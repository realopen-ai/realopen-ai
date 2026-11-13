"""Source inventory shared by the composer and command execution."""

import uuid

from sqlalchemy import select

from app.db.models import Artifact, Document
from app.learn.notebooks import source_catalog
from app.services.artifacts import metadata


COMMANDS = frozenset({"/find", "/summarize", "/note", "/flashcards", "/quiz"})
MAX_SELECTION = 20


def selection_blocks(ids, sources):
    ids = list(dict.fromkeys(map(str, ids)))
    titles = {source["id"]: source["title"] for source in sources}
    return [
        {
            "type": "selected_sources",
            "ids": ids,
            "titles": {ident: titles[ident] for ident in ids if ident in titles},
        }
    ]


async def saved_selection_blocks(conversation_id, ids, session_factory):
    async with session_factory() as db:
        return selection_blocks(ids, await list_sources(db, conversation_id))


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


def restrict_citations(arguments, document_ids, artifact_ids):
    """Drop model-supplied references outside the explicitly selected files."""

    def restrict(item):
        if str(item.get("source_document_id")) not in document_ids:
            item.pop("source_document_id", None)
            item.pop("source_page", None)
            item.pop("source_chunk_id", None)
        reference = item.get("source_artifact")
        if (
            isinstance(reference, dict)
            and str(reference.get("artifact_id")) not in artifact_ids
        ):
            item.pop("source_artifact", None)

    restrict(arguments)
    for question in arguments.get("questions") or []:
        if isinstance(question, dict):
            restrict(question)
            question.pop("source_urls", None)
    for card in arguments.get("cards") or []:
        if not isinstance(card, dict):
            continue
        reference = card.get("source_artifact")
        if (
            isinstance(reference, dict)
            and str(reference.get("artifact_id")) not in artifact_ids
        ):
            card.pop("source_artifact", None)
        if not arguments.get("source_document_id"):
            card.pop("source_page", None)
            card.pop("source_chunk_id", None)
