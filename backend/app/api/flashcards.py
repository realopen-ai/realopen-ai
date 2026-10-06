import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_db
from app.db.models import Flashcard
from app.learn import service
from app.learn.schemas import CardInput, DeckInput, DeckMetadata, ReviewInput

router = APIRouter(prefix="/learn/flashcards", tags=["learn"])


@router.get("")
async def list_decks(db: AsyncSession = Depends(get_db)):
    return await service.list_decks(db)


@router.post("", status_code=201)
async def create_deck(body: DeckInput, db: AsyncSession = Depends(get_db)):
    deck = await service.create_deck(db, body)
    return await service.deck_detail(db, deck.id)


@router.get("/{deck_id}")
async def get_deck(deck_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await service.deck_detail(db, deck_id)


@router.put("/{deck_id}")
async def edit_deck(deck_id: uuid.UUID, body: DeckMetadata, db: AsyncSession = Depends(get_db)):
    deck = await service.deck_or_404(db, deck_id, lock=True)
    deck.title, deck.description = body.title, body.description
    deck.updated_at = service.now()
    await db.flush()
    return await service.deck_detail(db, deck_id)


@router.delete("/{deck_id}", status_code=204)
async def delete_deck(deck_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    deck = await service.deck_or_404(db, deck_id, lock=True)
    await db.delete(deck)
    return Response(status_code=204)


@router.post("/{deck_id}/cards", status_code=201)
async def add_card(deck_id: uuid.UUID, body: CardInput, db: AsyncSession = Depends(get_db)):
    deck = await service.deck_or_404(db, deck_id, lock=True)
    await service.add_card(db, deck, body)
    return await service.deck_detail(db, deck_id)


@router.put("/{deck_id}/cards/{card_id}")
async def edit_card(
    deck_id: uuid.UUID, card_id: uuid.UUID, body: CardInput, db: AsyncSession = Depends(get_db)
):
    deck = await service.deck_or_404(db, deck_id, lock=True)
    await service.add_card(db, deck, body, exclude_id=card_id)
    return await service.deck_detail(db, deck_id)


@router.delete("/{deck_id}/cards/{card_id}", status_code=204)
async def delete_card(deck_id: uuid.UUID, card_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    deck = await service.deck_or_404(db, deck_id, lock=True)
    card = (
        await db.execute(
            select(Flashcard).where(Flashcard.deck_id == deck_id, Flashcard.id == card_id)
        )
    ).scalar_one_or_none()
    if card is None:
        raise HTTPException(404, "Card not found")
    await db.delete(card)
    deck.updated_at = service.now()
    return Response(status_code=204)


@router.post("/{deck_id}/cards/{card_id}/reviews")
async def review_card(
    deck_id: uuid.UUID, card_id: uuid.UUID, body: ReviewInput, db: AsyncSession = Depends(get_db)
):
    return await service.review_card(db, deck_id, card_id, body)
