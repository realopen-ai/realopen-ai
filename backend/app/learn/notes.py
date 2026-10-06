"""Lightweight notes; provenance validation and transactions reuse Learn patterns."""

import uuid
from typing import Literal
from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select, or_
from app.db.models import StudyNote, DocumentChunk
from app.learn.service import check_sources


class NoteContent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(default="", max_length=40000)
    pinned: bool = False

    @field_validator("title", mode="before")
    @classmethod
    def strip_title(cls, value):
        return value.strip() if isinstance(value, str) else value


class NoteInput(NoteContent):
    source_conversation_id: uuid.UUID | None = None
    source_document_id: uuid.UUID | None = None
    source_page: int | None = Field(default=None, ge=1)
    source_chunk_id: uuid.UUID | None = None


class NoteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["shorter", "clearer", "expand", "bullets", "flashcards"]
    selection: str | None = Field(default=None, min_length=1, max_length=12000)
    count: int = Field(default=10, ge=1, le=30)


async def get_note(db, note_id, lock=False):
    query = select(StudyNote).where(StudyNote.id == note_id)
    if lock:
        query = query.with_for_update()
    note = (await db.execute(query)).scalar_one_or_none()
    if note is None:
        raise HTTPException(404, "Note not found")
    return note


def note_dict(note, summary=False):
    fields = (
        "id",
        "title",
        "pinned",
        "source_conversation_id",
        "source_document_id",
        "source_page",
        "source_chunk_id",
        "created_at",
        "updated_at",
    )
    result = {field: getattr(note, field) for field in fields}
    result["excerpt" if summary else "content"] = note.content[:200] if summary else note.content
    return result


async def create_note(db, body):
    await check_sources(db, body)
    if body.source_chunk_id:
        chunk = await db.get(DocumentChunk, body.source_chunk_id)
        if chunk is None or chunk.document_id != body.source_document_id:
            raise HTTPException(422, "Source chunk must belong to the note document")
        if body.source_page is not None and body.source_page != chunk.page_number:
            raise HTTPException(422, "Source page does not match the chunk")
        body = body.model_copy(update={"source_page": chunk.page_number})
    elif body.source_page and not body.source_document_id:
        raise HTTPException(422, "A source page requires a document")
    note = StudyNote(**body.model_dump())
    db.add(note)
    await db.flush()
    return note


async def list_notes(db, search=""):
    query = select(StudyNote)
    if search.strip():
        # Bound, escaped LIKE values: user input never becomes SQL.
        query = query.where(
            or_(
                StudyNote.title.icontains(search.strip(), autoescape=True),
                StudyNote.content.icontains(search.strip(), autoescape=True),
            )
        )
    notes = (
        (
            await db.execute(
                query.order_by(
                    StudyNote.pinned.desc(), StudyNote.updated_at.desc(), StudyNote.id
                ).limit(200)
            )
        )
        .scalars()
        .all()
    )
    return [note_dict(note, summary=True) for note in notes]
