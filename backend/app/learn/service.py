"""Shared persistence for REST and agent-created decks. Transactions belong to callers."""

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import select, func, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.exc import IntegrityError

from app.db.models import (
    Conversation,
    Document,
    DocumentChunk,
    Flashcard,
    FlashcardDeck,
    FlashcardProgress,
    FlashcardReview,
)
from app.learn.schemas import CardInput, DeckInput, ReviewInput, normalized, similar_question
from app.learn.scheduler import schedule_review


def now():
    return datetime.now(timezone.utc)


def utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


async def deck_or_404(db: AsyncSession, deck_id: uuid.UUID, *, lock=False):
    query = select(FlashcardDeck).where(FlashcardDeck.id == deck_id)
    if lock:
        query = query.with_for_update()
    deck = (await db.execute(query)).scalar_one_or_none()
    if deck is None:
        raise HTTPException(404, "Deck not found")
    return deck


async def check_sources(db, body: DeckInput):
    if (
        body.source_conversation_id
        and await db.get(Conversation, body.source_conversation_id) is None
    ):
        raise HTTPException(404, "Source conversation not found")
    if body.source_document_id:
        document = await db.get(Document, body.source_document_id)
        if document is None:
            raise HTTPException(404, "Source document not found")
        if document.scope == "private" and document.conversation_id != body.source_conversation_id:
            raise HTTPException(403, "Document belongs to another conversation")


async def create_deck(db: AsyncSession, body: DeckInput):
    await check_sources(db, body)
    deck = FlashcardDeck(
        title=body.title,
        description=body.description,
        source_conversation_id=body.source_conversation_id,
        source_document_id=body.source_document_id,
    )
    db.add(deck)
    await db.flush()
    for position, card in enumerate(body.cards):
        await add_card(db, deck, card, position=position)
    await db.flush()
    return deck


async def add_card(db, deck, body: CardInput, *, position=None, exclude_id=None):
    if body.source_chunk_id:
        chunk = await db.get(DocumentChunk, body.source_chunk_id)
        if chunk is None or chunk.document_id != deck.source_document_id:
            raise HTTPException(422, "Source chunk must belong to the deck document")
        if body.source_page is not None and chunk.page_number != body.source_page:
            raise HTTPException(422, "Source page does not match the retrieved chunk")
        if body.source_page is None:
            body = body.model_copy(update={"source_page": chunk.page_number})
    elif body.source_page and not deck.source_document_id:
        raise HTTPException(422, "A source page requires a document source")
    existing = (
        (await db.execute(select(Flashcard).where(Flashcard.deck_id == deck.id))).scalars().all()
    )
    edited = next((card for card in existing if card.id == exclude_id), None)
    if exclude_id is not None and edited is None:
        raise HTTPException(404, "Card not found")
    question_changed = edited is None or normalized(edited.front) != normalized(body.front)
    if question_changed and any(
        card.id != exclude_id and similar_question(card.front, body.front) for card in existing
    ):
        raise HTTPException(422, "Duplicate card question")
    if exclude_id is None and len(existing) >= 100:
        raise HTTPException(422, "A deck can contain at most 100 cards")
    if exclude_id is not None:
        card = edited
        for field, value in body.model_dump().items():
            setattr(card, field, value)
        card.updated_at = now()
    else:
        card = Flashcard(
            deck_id=deck.id,
            **body.model_dump(),
            position=position
            if position is not None
            else max((c.position for c in existing), default=-1) + 1,
        )
        db.add(card)
        await db.flush()
        db.add(FlashcardProgress(card_id=card.id, due_at=now()))
    deck.updated_at = now()
    await db.flush()
    return card


def deck_dict(deck, card_count=0, due_count=0, next_review_at=None, last_studied_at=None):
    return {
        "id": str(deck.id),
        "title": deck.title,
        "description": deck.description,
        "source_conversation_id": str(deck.source_conversation_id)
        if deck.source_conversation_id
        else None,
        "source_document_id": str(deck.source_document_id) if deck.source_document_id else None,
        "created_at": deck.created_at,
        "updated_at": deck.updated_at,
        "card_count": card_count,
        "due_count": due_count,
        "next_review_at": next_review_at,
        "last_studied_at": last_studied_at,
    }


def card_dict(card, progress):
    return {
        "id": str(card.id),
        "deck_id": str(card.deck_id),
        "front": card.front,
        "back": card.back,
        "position": card.position,
        "source_reference": card.source_reference,
        "source_page": card.source_page,
        "source_chunk_id": str(card.source_chunk_id) if card.source_chunk_id else None,
        "due_at": progress.due_at,
        "reviews": progress.reviews,
        "interval_days": progress.interval_days,
    }


async def list_decks(db):
    counts = select(
        Flashcard.deck_id.label("deck_id"),
        func.count().label("card_count"),
        func.sum(case((FlashcardProgress.due_at <= now(), 1), else_=0)).label("due_count"),
        func.min(FlashcardProgress.due_at).label("next_review_at"),
        func.max(FlashcardProgress.last_reviewed_at).label("last_studied_at"),
    )
    counts = (
        counts.join(FlashcardProgress, FlashcardProgress.card_id == Flashcard.id)
        .group_by(Flashcard.deck_id)
        .subquery()
    )
    rows = (
        await db.execute(
            select(
                FlashcardDeck,
                counts.c.card_count,
                counts.c.due_count,
                counts.c.next_review_at,
                counts.c.last_studied_at,
            )
            .outerjoin(counts, counts.c.deck_id == FlashcardDeck.id)
            .order_by(FlashcardDeck.updated_at.desc())
        )
    ).all()
    return [
        deck_dict(deck, count or 0, due or 0, next_at, last_at)
        for deck, count, due, next_at, last_at in rows
    ]


async def due_session(db):
    """Oldest due first; UUID tie-breaker makes ordering stable across reloads."""
    rows = (
        await db.execute(
            select(Flashcard, FlashcardProgress, FlashcardDeck)
            .join(FlashcardProgress, FlashcardProgress.card_id == Flashcard.id)
            .join(FlashcardDeck, FlashcardDeck.id == Flashcard.deck_id)
            .where(FlashcardProgress.due_at <= now())
            .order_by(FlashcardProgress.due_at, Flashcard.id)
        )
    ).all()
    return {
        "cards": [
            {
                **card_dict(card, progress),
                "deck_title": deck.title,
                "source_document_id": str(deck.source_document_id)
                if deck.source_document_id
                else None,
                "source_conversation_id": str(deck.source_conversation_id)
                if deck.source_conversation_id
                else None,
            }
            for card, progress, deck in rows
        ]
    }


async def deck_detail(db, deck_id):
    deck = await deck_or_404(db, deck_id)
    rows = (
        await db.execute(
            select(Flashcard, FlashcardProgress)
            .join(FlashcardProgress, FlashcardProgress.card_id == Flashcard.id)
            .where(Flashcard.deck_id == deck_id)
            .order_by(Flashcard.position, Flashcard.created_at)
        )
    ).all()
    current = now()
    # SQLite unit tests strip timezone information; persisted PostgreSQL dates are aware.
    due = sum(
        (p.due_at.replace(tzinfo=timezone.utc) if p.due_at.tzinfo is None else p.due_at) <= current
        for _, p in rows
    )
    return {
        **deck_dict(deck, len(rows), due, min((p.due_at for _, p in rows), default=None)),
        "cards": [card_dict(card, progress) for card, progress in rows],
    }


async def review_card(db, deck_id, card_id, body: ReviewInput):
    card = (
        await db.execute(
            select(Flashcard).where(Flashcard.id == card_id, Flashcard.deck_id == deck_id)
        )
    ).scalar_one_or_none()
    if card is None:
        raise HTTPException(404, "Card not found")
    progress = (
        await db.execute(
            select(FlashcardProgress).where(FlashcardProgress.card_id == card_id).with_for_update()
        )
    ).scalar_one()
    previous = await db.get(FlashcardReview, body.review_id)
    if previous:
        if previous.card_id != card_id or previous.rating != body.rating:
            raise HTTPException(409, "Review key already used")
        return {
            "review_id": str(previous.id),
            "due_at": utc(previous.due_at),
            "interval_days": previous.interval_days,
            "reviews": progress.reviews,
        }
    if body.expected_reviews != progress.reviews:
        raise HTTPException(409, "Card was reviewed elsewhere. Reload the study session.")
    reviewed_at = now()
    result = schedule_review(
        rating=body.rating,
        now=reviewed_at,
        interval_days=progress.interval_days,
        ease=progress.ease,
        repetitions=progress.repetitions,
        lapses=progress.lapses,
    )
    for field in ("due_at", "interval_days", "ease", "repetitions", "lapses"):
        setattr(progress, field, getattr(result, field))
    progress.reviews += 1
    progress.last_reviewed_at = reviewed_at
    db.add(
        FlashcardReview(
            id=body.review_id,
            card_id=card_id,
            rating=body.rating,
            reviewed_at=reviewed_at,
            due_at=result.due_at,
            interval_days=result.interval_days,
        )
    )
    try:
        await db.flush()
    except IntegrityError as exc:
        raise HTTPException(409, "Review key already used") from exc
    return {
        "review_id": str(body.review_id),
        "due_at": result.due_at,
        "interval_days": result.interval_days,
        "reviews": progress.reviews,
    }
