"""Real SQL persistence tests, without Ollama or external services."""

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from pydantic import ValidationError

from app.api.flashcards import router
from app.db.session import get_db
from app.db.models import FlashcardDeck, Flashcard, FlashcardProgress, FlashcardReview
from app.learn.schemas import CardInput, DeckInput
from app.learn.scheduler import schedule_review
from app.learn import service


class AsyncAdapter:
    def __init__(self, session):
        self.session = session

    def add(self, value):
        self.session.add(value)

    async def flush(self):
        self.session.flush()

    async def execute(self, query):
        return self.session.execute(query)

    async def get(self, model, key):
        return self.session.get(model, key)

    async def delete(self, value):
        self.session.delete(value)

    async def commit(self):
        self.session.commit()


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE conversations (id UUID PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE documents (id UUID PRIMARY KEY)"))
    for model in (FlashcardDeck, Flashcard, FlashcardProgress, FlashcardReview):
        model.__table__.create(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def client(database):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def db_override():
        with Session(database, expire_on_commit=False) as session:
            try:
                yield AsyncAdapter(session)
                session.commit()
            except Exception:
                session.rollback()
                raise

    app.dependency_overrides[get_db] = db_override
    with TestClient(app) as client:
        yield client


def create(client):
    response = client.post(
        "/api/learn/flashcards",
        json={
            "title": "Python",
            "cards": [{"front": "What does a dict store?", "back": "Key-value pairs"}],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize(
    "front,back",
    [
        ("", "answer"),
        (" ", "answer"),
        ("question", ""),
        (" same ", "SAME"),
        ("x" * 2001, "answer"),
        ("question", "x" * 4001),
    ],
)
def test_rejects_bad_cards(front, back):
    with pytest.raises(ValidationError):
        CardInput(front=front, back=back)


def test_duplicate_fronts_rejected():
    with pytest.raises(ValidationError):
        DeckInput(
            title="test",
            cards=[CardInput(front="Why?", back="One"), CardInput(front=" WHY? ", back="Two")],
        )


def test_crud_and_cascading_history(client, database):
    deck = create(client)
    deck_id = deck["id"]
    card = deck["cards"][0]
    assert deck["due_count"] == 1
    assert client.get("/api/learn/flashcards").json()[0]["card_count"] == 1
    edited = client.put(
        f"/api/learn/flashcards/{deck_id}", json={"title": "Updated", "description": "Notes"}
    )
    assert edited.json()["title"] == "Updated"
    duplicate = client.post(
        f"/api/learn/flashcards/{deck_id}/cards",
        json={"front": "WHAT does a dict store?", "back": "Other"},
    )
    assert duplicate.status_code == 422
    added = client.post(
        f"/api/learn/flashcards/{deck_id}/cards",
        json={"front": "What is a list?", "back": "An ordered sequence"},
    )
    assert added.status_code == 201
    assert added.json()["card_count"] == 2
    changed = client.put(
        f"/api/learn/flashcards/{deck_id}/cards/{card['id']}",
        json={"front": "Dictionary?", "back": "Mapping", "source_reference": "Chapter 4"},
    )
    assert changed.json()["cards"][0]["source_reference"] == "Chapter 4"
    review = client.post(
        f"/api/learn/flashcards/{deck_id}/cards/{card['id']}/reviews",
        json={"rating": "good", "review_id": str(uuid.uuid4()), "expected_reviews": 0},
    )
    assert review.status_code == 200
    assert client.delete(f"/api/learn/flashcards/{deck_id}").status_code == 204
    assert client.get(f"/api/learn/flashcards/{deck_id}").status_code == 404
    with Session(database) as db:
        for model in (Flashcard, FlashcardProgress, FlashcardReview):
            assert not db.execute(select(model)).all()


def test_reviews_are_idempotent_and_reject_stale_updates(client):
    deck = create(client)
    card = deck["cards"][0]
    path = f"/api/learn/flashcards/{deck['id']}/cards/{card['id']}/reviews"
    body = {"rating": "easy", "review_id": str(uuid.uuid4()), "expected_reviews": 0}
    first = client.post(path, json=body)
    assert first.status_code == 200
    assert client.post(path, json=body).json() == first.json()
    assert client.post(path, json={**body, "rating": "hard"}).status_code == 409
    assert client.post(path, json={**body, "review_id": str(uuid.uuid4())}).status_code == 409
    assert client.post(path, json={**body, "rating": "invalid"}).status_code == 422
    detail = client.get(f"/api/learn/flashcards/{deck['id']}").json()
    assert detail["due_count"] == 0
    assert detail["cards"][0]["reviews"] == 1


def test_card_cannot_be_edited_or_reviewed_through_another_deck(client):
    first = create(client)
    second = create(client)
    card = first["cards"][0]
    base = f"/api/learn/flashcards/{second['id']}/cards/{card['id']}"
    assert client.put(base, json={"front": "No", "back": "Not yours"}).status_code == 404
    assert client.delete(base).status_code == 404
    assert (
        client.post(
            base + "/reviews",
            json={"rating": "good", "review_id": str(uuid.uuid4()), "expected_reviews": 0},
        ).status_code
        == 404
    )


def test_card_delete_cascades_progress(client, database):
    deck = create(client)
    assert (
        client.delete(
            f"/api/learn/flashcards/{deck['id']}/cards/{deck['cards'][0]['id']}"
        ).status_code
        == 204
    )
    assert client.get(f"/api/learn/flashcards/{deck['id']}").json()["card_count"] == 0
    with Session(database) as db:
        assert not db.execute(select(FlashcardProgress)).all()


@pytest.mark.parametrize(
    "rating,minutes,days", [("again", 10, 0), ("hard", 0, 1), ("good", 0, 1), ("easy", 0, 1)]
)
def test_initial_schedule(rating, minutes, days):
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    result = schedule_review(rating=rating, now=now)
    assert result.due_at == now + timedelta(minutes=minutes, days=days)
    assert result.ease >= 1.3


def test_successive_intervals_and_failure_reset():
    now = datetime.now(timezone.utc)
    second = schedule_review(rating="good", now=now, repetitions=1, interval_days=1)
    assert second.interval_days == 6
    assert (
        schedule_review(rating="easy", now=now, repetitions=2, interval_days=6).interval_days == 15
    )
    reset = schedule_review(rating="again", now=now, repetitions=10, ease=1.3, lapses=2)
    assert reset.repetitions == 0 and reset.lapses == 3 and reset.ease == 1.3
    with pytest.raises(ValueError):
        schedule_review(rating="unknown", now=now)


@pytest.mark.asyncio
async def test_private_source_scope_checked():
    class DB:
        async def get(self, model, key):
            return SimpleNamespace(scope="private", conversation_id=uuid.uuid4())

    with pytest.raises(HTTPException) as error:
        await service.check_sources(DB(), DeckInput(title="deck", source_document_id=uuid.uuid4()))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_agent_returns_small_persistent_artifact(database, monkeypatch):
    from app.agent.tools import flashcards

    @asynccontextmanager
    async def factory():
        with Session(database, expire_on_commit=False) as session:
            yield AsyncAdapter(session)

    monkeypatch.setattr(flashcards, "async_session_factory", factory)
    result = await flashcards.CreateFlashcardDeckTool().execute(
        title="Python", cards=[{"front": "Dictionary?", "back": "Mapping"}]
    )
    assert result.success
    assert "Mapping" not in result.output
    assert result.tool_call.gen_results[0]["type"] == "flashcard_deck"
    with Session(database) as db:
        assert len(db.execute(select(Flashcard)).all()) == 1
    bad = await flashcards.CreateFlashcardDeckTool().execute(title="Bad", cards=[])
    assert not bad.success
