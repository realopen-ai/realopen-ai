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
        connection.execute(text("CREATE TABLE document_chunks (id UUID PRIMARY KEY)"))
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


@pytest.mark.parametrize(
    "raw",
    [
        '{"cards":[{"front":"Translate you are into Spanish.","back":"Tú estás"}]}',
        '{"front":"Translate you are into Spanish.","back":"Tú estás"}',
        '[{"front":"Translate you are into Spanish.","back":"Tú estás"}]',
        '```json\n{"card":{"front":"Translate you are into Spanish.","back":"Tú estás"}}\n```',
        '{"title":"Ignored", "cards":[{"id":"ignored","front":"Translate you are into Spanish.","back":"Tú estás","source_page":999}]}',
    ],
)
def test_card_proposal_accepts_common_json_shapes(raw):
    from app.learn.editing import parse_proposal

    cards = parse_proposal(raw, 1)
    assert cards[0].back == "Tú estás"
    assert cards[0].source_page is None


@pytest.mark.parametrize(
    "raw,count",
    [
        ('{"cards":[]}', 1),
        ('{"cards":[{"front":"","back":"Answer"}]}', 1),
        ('{"cards":[{"front":"Same","back":"SAME"}]}', 1),
        ('{"cards":[{"front":"Question?","back":"Answer"}]}', 2),
        (
            '{"cards":[{"front":"Question?","back":"Answer"},{"front":"Question?","back":"Other"}]}',
            2,
        ),
        ("not JSON", 1),
    ],
)
def test_invalid_proposals_still_rejected(raw, count):
    from app.learn.editing import parse_proposal

    with pytest.raises(ValueError):
        parse_proposal(raw, count)


def test_card_rewrite_uses_schema_and_retries_once(client, monkeypatch):
    from app.learn import editing

    calls = []

    async def resolve(_):
        return "test-local-model"

    async def chat(model, messages, **kwargs):
        calls.append((messages, kwargs))
        return {
            "message": {
                "content": "not JSON"
                if len(calls) == 1
                else '{"front":"Translate you are into Spanish.","back":"Tú estás"}'
            }
        }

    monkeypatch.setattr(editing.model_prefs, "resolve_task_model", resolve)
    monkeypatch.setattr(editing.providers, "chat_once", chat)
    response = client.post(
        "/api/learn/flashcards/rewrite",
        json={
            "card": {
                "front": "How do you say 'I am' in Spanish (tú form)?",
                "back": "Tú estás",
                "source_reference": None,
                "source_page": None,
                "source_chunk_id": None,
            },
            "action": "recall",
        },
    )
    assert response.status_code == 200, response.text
    assert len(calls) == 2
    assert calls[0][1]["format"]["properties"]["cards"]["maxItems"] == 1
    assert len(calls[1][0]) == 2
    assert client.get("/api/learn/flashcards").json() == []


def test_correct_card_proposes_factual_changes_without_overwriting(client, monkeypatch):
    from app.learn import editing
    import json

    deck = client.post(
        "/api/learn/flashcards",
        json={
            "title": "Spanish",
            "cards": [{"front": "How do you say 'I am' in Spanish (tú form)?", "back": "Tú estás"}],
        },
    ).json()
    messages_sent = []

    async def resolve(_):
        return "test-local-model"

    async def chat(model, messages, **kwargs):
        messages_sent.extend(messages)
        assert kwargs["format"]["properties"]["cards"]["maxItems"] == 1
        return {
            "message": {
                "content": json.dumps(
                    {
                        "cards": [
                            {
                                "front": "How do you say 'you are' in Spanish using estar (tú form)?",
                                "back": "Tú estás",
                            }
                        ]
                    }
                )
            }
        }

    monkeypatch.setattr(editing.model_prefs, "resolve_task_model", resolve)
    monkeypatch.setattr(editing.providers, "chat_once", chat)
    response = client.post(
        "/api/learn/flashcards/rewrite",
        json={
            "action": "correct",
            "card": {
                "front": deck["cards"][0]["front"],
                "back": deck["cards"][0]["back"],
                "source_reference": "Lesson 1",
            },
        },
    )
    assert response.status_code == 200, response.text
    assert "you are" in response.json()["cards"][0]["front"]
    assert response.json()["cards"][0]["source_reference"] == "Lesson 1"
    assert "Do not assume the existing answer is correct" in messages_sent[0]["content"]
    assert "Preserve its language and factual meaning" not in messages_sent[0]["content"]
    assert len(messages_sent) == 2
    original = client.get(f"/api/learn/flashcards/{deck['id']}").json()
    assert original["cards"][0]["front"] == deck["cards"][0]["front"]


def test_near_duplicates_are_rejected_without_conflating_numbers():
    from app.learn.schemas import similar_question

    assert similar_question(
        "What does a **Python dictionary** store?", "What does a Python dictionary store"
    )
    assert similar_question(
        "What does a Python dictionary store?", "What does a Python dictionary stores?"
    )
    assert not similar_question("What does chapter 1 explain?", "What does chapter 2 explain?")
    with pytest.raises(ValidationError):
        DeckInput(
            title="test",
            cards=[
                CardInput(front="What does a Python dictionary store?", back="Pairs"),
                CardInput(front="What does a Python dictionary stores?", back="Mapping"),
            ],
        )


def test_daily_queue_order_and_last_studied(client, database):
    first, second = create(client), create(client)
    earlier = datetime.now(timezone.utc) - timedelta(days=2)
    with Session(database) as db:
        db.get(FlashcardProgress, uuid.UUID(second["cards"][0]["id"])).due_at = earlier
        db.commit()
    cards = client.get("/api/learn/flashcards/review").json()["cards"]
    assert [card["deck_id"] for card in cards] == [second["id"], first["id"]]
    assert cards[0]["deck_title"] == "Python"
    assert client.get("/api/learn/flashcards").json()[0]["last_studied_at"] is None
    response = client.post(
        f"/api/learn/flashcards/{second['id']}/cards/{cards[0]['id']}/reviews",
        json={
            "rating": "good",
            "review_id": str(uuid.uuid4()),
            "expected_reviews": 0,
        },
    )
    assert response.status_code == 200
    assert len(client.get("/api/learn/flashcards/review").json()["cards"]) == 1
    decks = client.get("/api/learn/flashcards").json()
    assert next(deck for deck in decks if deck["id"] == second["id"])["last_studied_at"]


def test_split_is_atomic_and_preserves_original_review_state(client):
    deck = create(client)
    path = f"/api/learn/flashcards/{deck['id']}/cards/{deck['cards'][0]['id']}/replace"
    response = client.post(
        path,
        json={
            "cards": [
                {"front": "What is a mapping?", "back": "A key-value collection"},
                {"front": "What is a key?", "back": "A lookup identifier"},
            ]
        },
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["cards"]) == 2
    assert response.json()["cards"][0]["id"] == deck["cards"][0]["id"]
    bad = client.post(
        path,
        json={
            "cards": [
                {"front": "Would this edit persist?", "back": "No"},
                {"front": "What is a key?", "back": "Duplicate"},
            ]
        },
    )
    assert bad.status_code == 422
    current = client.get(f"/api/learn/flashcards/{deck['id']}").json()
    assert current["cards"][0]["front"] == "What is a mapping?"


def test_proposal_sends_only_card_and_never_persists(client, monkeypatch):
    from app.learn import editing

    calls = []

    async def resolve(_):
        return "test-model"

    async def chat(model, messages, **kwargs):
        calls.append((model, messages, kwargs))
        return {"message": {"content": '{"cards":[{"front":"Mapping?","back":"Key-value pairs"}]}'}}

    monkeypatch.setattr(editing.model_prefs, "resolve_task_model", resolve)
    monkeypatch.setattr(editing.providers, "chat_once", chat)
    response = client.post(
        "/api/learn/flashcards/rewrite",
        json={
            "action": "shorter",
            "card": {
                "front": "What does a dictionary store?",
                "back": "Key-value pairs",
                "source_reference": "Chapter 4",
            },
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["cards"][0]["source_reference"] == "Chapter 4"
    assert len(calls[0][1]) == 2
    assert "Chapter 4" not in calls[0][1][1]["content"]
    assert client.get("/api/learn/flashcards").json() == []

    async def invalid(*args, **kwargs):
        return {"message": {"content": "not JSON"}}

    monkeypatch.setattr(editing.providers, "chat_once", invalid)
    assert (
        client.post(
            "/api/learn/flashcards/rewrite",
            json={"action": "shorter", "card": {"front": "Q?", "back": "A"}},
        ).status_code
        == 422
    )


@pytest.mark.asyncio
async def test_chunk_grounding_checks_document_and_derives_page():
    from app.db.models import DocumentChunk

    document_id, chunk_id = uuid.uuid4(), uuid.uuid4()
    deck = SimpleNamespace(id=uuid.uuid4(), source_document_id=document_id)

    class FakeDB:
        async def get(self, model, key):
            assert model is DocumentChunk
            return SimpleNamespace(document_id=document_id, page_number=4)

        async def execute(self, _):
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

        def add(self, card):
            if isinstance(card, Flashcard):
                card.id = uuid.uuid4()
                self.card = card

        async def flush(self):
            pass

    db = FakeDB()
    await service.add_card(
        db, deck, CardInput(front="A question?", back="An answer", source_chunk_id=chunk_id)
    )
    assert db.card.source_page == 4
    with pytest.raises(HTTPException) as error:
        await service.add_card(
            db,
            deck,
            CardInput(front="Other?", back="Answer", source_chunk_id=chunk_id, source_page=99),
        )
    assert error.value.status_code == 422
    deck.source_document_id = uuid.uuid4()
    with pytest.raises(HTTPException):
        await service.add_card(
            db, deck, CardInput(front="Other?", back="Answer", source_chunk_id=chunk_id)
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
