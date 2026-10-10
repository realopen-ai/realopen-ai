"""Notebook organization and source boundaries; content stays in existing services."""

import uuid
from datetime import datetime, timezone
from fastapi import HTTPException
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select
from app.db.models import (
    Notebook,
    NotebookItem,
    Document,
    Artifact,
    StudyNote,
    FlashcardDeck,
    Quiz,
)

TARGETS = {
    "document": ("document_id", Document),
    "artifact": ("artifact_id", Artifact),
    "note": ("note_id", StudyNote),
    "deck": ("deck_id", FlashcardDeck),
    "quiz": ("quiz_id", Quiz),
}


class NotebookInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


async def get_notebook(db, notebook_id, *, lock=False):
    query = select(Notebook).where(Notebook.id == notebook_id)
    if lock:
        query = query.with_for_update()
    notebook = (await db.execute(query)).scalar_one_or_none()
    if notebook is None:
        raise HTTPException(404, "Notebook not found")
    return notebook


async def for_conversation(db, conversation_id):
    if not conversation_id:
        return None
    result = (
        await db.execute(
            select(Notebook).where(Notebook.conversation_id == uuid.UUID(str(conversation_id)))
        )
    ).scalar_one_or_none()
    return result if isinstance(result, Notebook) else None


async def selected_sources(db, conversation_id):
    notebook = await for_conversation(db, conversation_id)
    if notebook is None:
        return None
    return (
        (
            await db.execute(
                select(NotebookItem)
                .where(
                    NotebookItem.notebook_id == notebook.id,
                    NotebookItem.selected.is_(True),
                    (NotebookItem.document_id.isnot(None) | NotebookItem.artifact_id.isnot(None)),
                )
                .order_by(NotebookItem.created_at, NotebookItem.id)
            )
        )
        .scalars()
        .all()
    )


async def selected_document_ids(db, conversation_id):
    sources = await selected_sources(db, conversation_id)
    if sources is None:
        return None
    ids = {item.document_id for item in sources if item.document_id}
    artifacts = [item.artifact_id for item in sources if item.artifact_id]
    if artifacts:
        ids.update(
            (
                await db.execute(
                    select(Artifact.document_id).where(
                        Artifact.id.in_(artifacts), Artifact.document_id.isnot(None)
                    )
                )
            )
            .scalars()
            .all()
        )
    return sorted(ids, key=str)


async def source_catalog(db, conversation_id, *, offset=0, limit=30):
    sources = await selected_sources(db, conversation_id)
    if sources is None:
        return None
    items = []
    for link in sources:
        if link.document_id:
            doc = await db.get(Document, link.document_id)
            if doc:
                items.append(
                    {
                        "id": str(doc.id),
                        "title": doc.filename[:120],
                        "kind": "upload",
                        "version": 1,
                        "document_id": str(doc.id),
                        "editable": False,
                    }
                )
        elif link.artifact_id:
            artifact = await db.get(Artifact, link.artifact_id)
            if artifact:
                items.append(
                    {
                        "id": str(artifact.id),
                        "title": artifact.title[:120],
                        "kind": artifact.kind,
                        "version": artifact.current_version,
                        "document_id": str(artifact.document_id) if artifact.document_id else None,
                        "editable": False,
                    }
                )
    return {
        "items": items[offset : offset + limit],
        "offset": offset,
        "next_offset": offset + limit if len(items) > offset + limit else None,
    }


async def permits_artifact(db, conversation_id, identifier):
    sources = await selected_sources(db, conversation_id)
    if sources is None:
        return None
    allowed = {item.document_id or item.artifact_id for item in sources}
    artifact = await db.get(Artifact, identifier)
    if artifact and artifact.document_id in allowed:
        return True
    return identifier in allowed


async def attach(db, notebook, kind, target_id):
    field, model = TARGETS[kind]
    target = await db.get(model, target_id)
    if target is None:
        raise HTTPException(404, "Linked content not found")
    existing = (
        await db.execute(
            select(NotebookItem).where(
                NotebookItem.notebook_id == notebook.id, getattr(NotebookItem, field) == target_id
            )
        )
    ).scalar_one_or_none()
    if existing is None:
        db.add(NotebookItem(notebook_id=notebook.id, **{field: target_id}))
        notebook.updated_at = datetime.now(timezone.utc)
        await db.flush()


async def attach_generated(db, conversation_id, kind, target_id):
    notebook = await for_conversation(db, conversation_id)
    if notebook:
        # Serialize link mutations against notebook deletion.
        notebook = await get_notebook(db, notebook.id, lock=True)
        await attach(db, notebook, kind, target_id)


def metadata(notebook):
    return {
        "id": str(notebook.id),
        "title": notebook.title,
        "description": notebook.description,
        "conversation_id": str(notebook.conversation_id),
        "created_at": notebook.created_at.isoformat(),
        "updated_at": notebook.updated_at.isoformat(),
    }


async def detail(db, notebook):
    items = []
    links = (
        (
            await db.execute(
                select(NotebookItem)
                .where(NotebookItem.notebook_id == notebook.id)
                .order_by(NotebookItem.created_at, NotebookItem.id)
            )
        )
        .scalars()
        .all()
    )
    for link in links:
        for kind, (field, model) in TARGETS.items():
            target_id = getattr(link, field)
            if target_id:
                target = await db.get(model, target_id)
                if target is not None:
                    items.append(
                        {
                            "id": str(link.id),
                            "kind": kind,
                            "target_id": str(target_id),
                            "title": target.filename if kind == "document" else target.title,
                            "selected": link.selected,
                            "status": target.digestion_status if kind == "document" else None,
                        }
                    )
    return {**metadata(notebook), "items": items}
