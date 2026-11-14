"""Source inventory shared by the composer and command execution."""

import re
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db.models import Artifact, Document
from app.learn.notebooks import source_catalog
from app.services.artifacts import get_artifact, get_version, metadata, select_sections


COMMANDS = frozenset({"/find", "/summarize", "/note", "/flashcards", "/quiz"})
MAX_SELECTION = 20


class PinnedArtifact(BaseModel):
    artifact_id: uuid.UUID
    version: int = Field(ge=1)
    section_id: str | None = Field(default=None, min_length=1, max_length=128)


def selection_blocks(ids, sources, pinned=None):
    ids = list(dict.fromkeys(map(str, ids)))
    titles = {source["id"]: source["title"] for source in sources}
    return [
        {
            "type": "selected_sources",
            "ids": ids,
            "titles": {ident: titles[ident] for ident in ids if ident in titles},
            **({"pinned_artifact": pinned.model_dump(mode="json")} if pinned else {}),
        }
    ]


async def saved_selection_blocks(conversation_id, ids, session_factory, pinned=None):
    async with session_factory() as db:
        if pinned:
            try:
                sources = [await pinned_source(db, conversation_id, pinned)]
            except HTTPException:
                # Execution reports an unavailable source without exposing its title.
                sources = []
        else:
            sources = await list_sources(db, conversation_id)
        return selection_blocks(ids, sources, pinned)


async def pinned_source(db, conversation_id, pinned):
    artifact = await get_artifact(db, pinned.artifact_id, conversation_id, scoped=True)
    version = await get_version(db, artifact, pinned.version)
    sections = select_sections(version, pinned.section_id)
    source = metadata(artifact)
    title = f"{artifact.title} · v{version.number}"
    if pinned.section_id:
        title += f" · {sections[0]['title']}"
    return {**source, "title": title, **pinned.model_dump(mode="json")}


async def list_sources(db, conversation_id, temporary_chat=None):
    if temporary_chat is not None:
        return [
            {
                "id": str(item.id),
                "title": item.filename,
                "document_id": str(item.id),
            }
            for item in temporary_chat.attachments
            if not item.image
        ] + [
            {
                "id": str(ident),
                "title": temporary_chat.reports[str(ident)]["filename"],
                "document_id": None,
            }
            for ident in temporary_chat.versions
        ]
    if not conversation_id:
        return []
    conversation_id = uuid.UUID(str(conversation_id))
    notebook = await source_catalog(db, conversation_id, limit=None)
    if notebook is not None:
        return notebook["items"]

    documents = await db.scalars(
        select(Document)
        .where(
            Document.conversation_id == conversation_id,
            Document.digestion_status == "ready",
        )
        .order_by(Document.created_at.desc(), Document.id)
    )
    artifacts = await db.scalars(
        select(Artifact)
        .where(
            Artifact.conversation_id == conversation_id, Artifact.document_id.is_(None)
        )
        .order_by(Artifact.created_at.desc(), Artifact.id)
    )
    return [
        {"id": str(doc.id), "title": doc.filename, "document_id": str(doc.id)}
        for doc in documents
    ] + [metadata(artifact) for artifact in artifacts]


async def find_artifact_text(
    db, conversation_id, temporary_chat, ids, query, limit, *, pinned=None
):
    """Return exact matches with references to the searched artifact version."""
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    matches = []
    for ident in ids:
        if temporary_chat is not None:
            _, title, version = temporary_chat.artifact_version(ident, None)
        else:
            artifact = await get_artifact(db, ident, conversation_id, scoped=True)
            title = artifact.title
            version = await get_version(
                db, artifact, pinned.version if pinned else None
            )
        sections = select_sections(version, pinned.section_id if pinned else None)
        for section in sections:
            content = section["content"]
            match = pattern.search(content)
            if match is None:
                continue
            start = max(0, match.start() - 250)
            end = min(len(content), max(start + 750, match.end() + 250))
            excerpt = ("…" if start else "") + content[start:end]
            if end < len(content):
                excerpt += "…"
            matches.append(
                {
                    "document_id": None,
                    "document_filename": title,
                    "chunk_id": f"{ident}:{section['id']}",
                    "text": excerpt,
                    "snippet": excerpt,
                    "chunk_type": "text",
                    "page_number": section.get("page"),
                    "line_start": None,
                    "line_end": None,
                    "score": 1.0,
                    "vector_sim": 0.0,
                    "bm25_score": 0.0,
                    "artifact": {
                        "artifact_id": str(ident),
                        "version": version.number,
                        "section_id": section["id"],
                        **{
                            key: section[key]
                            for key in ("page", "slide", "sheet")
                            if section.get(key) is not None
                        },
                    },
                    "section_title": section["title"],
                }
            )
            if len(matches) == limit:
                return matches
    return matches


def restrict_citations(arguments, document_ids, artifact_ids, evidence, pinned=None):
    """Drop model-supplied references outside the explicitly selected files."""
    # A single retrieved section can supply provenance without guessing which
    # of several sections supports a particular note, card, or question.
    sections = {
        (
            record["metadata"].get("artifact_id"),
            record["metadata"].get("version"),
            record["request"].get("section_id"),
        )
        for record in evidence
        if record["tool"] == "read_artifact"
        and record["metadata"].get("artifact_id") in artifact_ids
        and record["metadata"].get("version")
        and record["request"].get("section_id")
        and record.get("evidence_kind") != "outline_only"
    }
    for record in evidence:
        metadata = record["metadata"]
        ident, version = metadata.get("artifact_id"), metadata.get("version")
        if record["tool"] == "summarize_artifact" and ident in artifact_ids and version:
            sections.update(
                (ident, version, reference["id"])
                for reference in metadata.get("source_references", [])
                if isinstance(reference, dict) and isinstance(reference.get("id"), str)
            )
    default_reference = None
    if len(sections) == 1 and len(artifact_ids) == 1 and not document_ids:
        ident, version, section = next(iter(sections))
        default_reference = {
            "artifact_id": ident,
            "version": version,
            "section_id": section,
        }

    def restrict_artifact(item):
        reference = item.get("source_artifact")
        if isinstance(reference, dict) and (
            str(reference.get("artifact_id")) not in artifact_ids
            or pinned
            and (
                reference.get("version") != pinned.version
                or pinned.section_id
                and reference.get("section_id") != pinned.section_id
            )
        ):
            item.pop("source_artifact", None)

    def restrict(item):
        if str(item.get("source_document_id")) not in document_ids:
            item.pop("source_document_id", None)
            item.pop("source_page", None)
            item.pop("source_chunk_id", None)
        restrict_artifact(item)

    restrict(arguments)
    if default_reference and "questions" not in arguments:
        arguments["source_artifact"] = (
            arguments.get("source_artifact") or default_reference
        )
    for question in arguments.get("questions") or []:
        if isinstance(question, dict):
            restrict(question)
            if default_reference and not question.get("source_artifact"):
                question["source_artifact"] = default_reference
            question.pop("source_urls", None)
    for card in arguments.get("cards") or []:
        if not isinstance(card, dict):
            continue
        restrict_artifact(card)
        if not arguments.get("source_document_id"):
            card.pop("source_page", None)
            card.pop("source_chunk_id", None)
