import uuid
from fastapi import APIRouter, Depends, Query, Response, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.session import get_db
from app.learn import notes, service
from app.learn.notes import NoteContent, NoteInput, NoteAction
from app.learn.schemas import DeckInput
from app.services.artifact_refs import ArtifactReference

router = APIRouter(prefix="/learn/notes", tags=["learn"])


@router.get("")
async def list_notes(
    q: str = Query(default="", max_length=200), db: AsyncSession = Depends(get_db)
):
    return await notes.list_notes(db, q)


@router.post("", status_code=201)
async def create_note(body: NoteInput, db: AsyncSession = Depends(get_db)):
    return notes.note_dict(await notes.create_note(db, body))


@router.get("/{note_id}")
async def get_note(note_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return notes.note_dict(await notes.get_note(db, note_id))


@router.put("/{note_id}")
async def update_note(note_id: uuid.UUID, body: NoteContent, db: AsyncSession = Depends(get_db)):
    note = await notes.get_note(db, note_id, lock=True)
    for field, value in body.model_dump().items():
        setattr(note, field, value)
    note.updated_at = service.now()
    await db.flush()
    return notes.note_dict(note)


@router.delete("/{note_id}", status_code=204)
async def delete_note(note_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    await db.delete(await notes.get_note(db, note_id, lock=True))
    return Response(status_code=204)


@router.post("/{note_id}/propose")
async def propose(note_id: uuid.UUID, body: NoteAction, db: AsyncSession = Depends(get_db)):
    from app.learn.note_actions import propose_note

    return await propose_note(await notes.get_note(db, note_id), body)


@router.post("/{note_id}/flashcards", status_code=201)
async def create_flashcards(
    note_id: uuid.UUID, body: DeckInput, db: AsyncSession = Depends(get_db)
):
    note = await notes.get_note(db, note_id)
    if not body.cards:
        raise HTTPException(422, "Provide at least one card")
    body = body.model_copy(
        update={
            "source_conversation_id": note.source_conversation_id,
            "source_document_id": note.source_document_id,
            "source_artifact": ArtifactReference.model_validate(note.source_artifact)
            if note.source_artifact
            else None,
        }
    )
    # Provenance comes from the note, not model-generated metadata.
    body.cards = [
        card.model_copy(
            update={
                "source_page": note.source_page if note.source_document_id else None,
                "source_chunk_id": note.source_chunk_id if note.source_document_id else None,
                "source_reference": None,
            }
        )
        for card in body.cards
    ]
    deck = await service.create_deck(db, body)
    deck.source_note_id = note.id
    await db.flush()
    return await service.deck_detail(db, deck.id)
