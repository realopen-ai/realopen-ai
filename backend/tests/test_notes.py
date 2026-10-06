"""Learn Notes API and focused AI behavior without an actual model/runtime."""

import json
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
import pytest
from sqlalchemy.orm import Session
from app.db.models import StudyNote
from app.learn import note_actions, notes
from app.learn.notes import NoteInput
from fastapi import HTTPException
import test_flashcards as flashcard_tests

client = flashcard_tests.client
database = flashcard_tests.database
AsyncAdapter = flashcard_tests.AsyncAdapter


def create(client, **values):
    response = client.post(
        "/api/learn/notes",
        json={
            "title": "Docker networking",
            "content": "## Bridge\nContainers use bridge networks.",
            **values,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_notes_crud_search_pin_and_empty_note(client):
    note = create(client)
    empty = create(client, title="Blank note", content="")
    path = f"/api/learn/notes/{note['id']}"
    response = client.put(
        path,
        json={
            "title": "Docker revised",
            "content": "## DNS\nDocker resolves container names.",
            "pinned": True,
        },
    )
    assert response.status_code == 200
    assert response.json()["updated_at"] >= note["updated_at"]
    listed = client.get("/api/learn/notes").json()
    assert listed[0]["id"] == note["id"] and listed[0]["pinned"]
    assert "content" not in listed[0] and "DNS" in listed[0]["excerpt"]
    assert len(client.get("/api/learn/notes?q=container").json()) == 1
    assert client.get("/api/learn/notes?q=%25").json() == []
    assert client.get("/api/learn/notes?q=' OR 1=1 --").json() == []
    assert client.get(f"/api/learn/notes/{empty['id']}").json()["content"] == ""
    assert client.delete(path).status_code == 204
    assert client.get(path).status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"title": " "},
        {"title": "x" * 201},
        {"title": "Title", "content": "x" * 40001},
        {"title": "Title", "source_page": 0},
        {"title": "Title", "unknown": "field"},
    ],
)
def test_note_validation(client, body):
    assert client.post("/api/learn/notes", json=body).status_code == 422


def test_note_provenance_validation(client):
    assert (
        client.post("/api/learn/notes", json={"title": "Note", "source_page": 4}).status_code == 422
    )


@pytest.mark.asyncio
async def test_note_chunk_grounding(monkeypatch):
    doc, chunk, conv = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    class DB:
        async def get(self, model, key):
            from app.db.models import Conversation, Document

            if model is Conversation:
                return SimpleNamespace(id=conv)
            if model is Document:
                return SimpleNamespace(id=doc, scope="private", conversation_id=conv)
            return SimpleNamespace(document_id=doc, page_number=4)

        def add(self, value):
            self.value = value

        async def flush(self):
            pass

    db = DB()
    note = await notes.create_note(
        db,
        NoteInput(
            title="Chapter",
            source_conversation_id=conv,
            source_document_id=doc,
            source_chunk_id=chunk,
        ),
    )
    assert note.source_page == 4 and note.source_chunk_id == chunk
    with pytest.raises(HTTPException) as error:
        await notes.create_note(
            db,
            NoteInput(
                title="Chapter",
                source_conversation_id=conv,
                source_document_id=doc,
                source_chunk_id=chunk,
                source_page=5,
            ),
        )
    assert error.value.status_code == 422
    with pytest.raises(HTTPException) as error:
        await notes.create_note(db, NoteInput(title="Other", source_document_id=doc))
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_missing_note_sources():
    class DB:
        async def get(self, model, key):
            return None

    for field in ("source_conversation_id", "source_document_id", "source_chunk_id"):
        with pytest.raises(HTTPException) as error:
            await notes.create_note(DB(), NoteInput(title="Note", **{field: uuid.uuid4()}))
        assert error.value.status_code in (404, 422)


def test_note_markdown_preserves_whitespace(client):
    content = "    indented code\n\nLast paragraph\n"
    note = create(client, content=content)
    assert note["content"] == content


def test_note_flashcards_share_service_and_preserve_source(client, database):
    note = create(client)
    response = client.post(
        f"/api/learn/notes/{note['id']}/flashcards",
        json={
            "title": "Docker cards",
            "cards": [
                {"front": "What connects containers on one host?", "back": "A bridge network."}
            ],
        },
    )
    assert response.status_code == 201, response.text
    deck = response.json()
    assert deck["source_note_id"] == note["id"]
    assert deck["card_count"] == 1 and deck["due_count"] == 1
    assert (
        client.post(
            f"/api/learn/notes/{note['id']}/flashcards", json={"title": "Bad", "cards": []}
        ).status_code
        == 422
    )
    client.delete(f"/api/learn/notes/{note['id']}")
    assert client.get(f"/api/learn/flashcards/{deck['id']}").json()["source_note_id"] is None


@pytest.mark.parametrize("action", ["shorter", "clearer", "expand", "bullets", "flashcards"])
def test_note_proposals_are_focused_and_never_auto_save(client, monkeypatch, action):
    note = create(client)
    captured = []

    async def resolve(task):
        return "local-model"

    async def chat(model, messages, **kwargs):
        captured.append((messages, kwargs))
        return {
            "message": {
                "content": json.dumps(
                    {"cards": [{"front": "What connects containers?", "back": "Bridge networks."}]}
                    if action == "flashcards"
                    else {"content": "## Concise\nBridge networks connect containers."}
                )
            }
        }

    monkeypatch.setattr(note_actions.model_prefs, "resolve_task_model", resolve)
    monkeypatch.setattr(note_actions.providers, "chat_once", chat)
    result = client.post(
        f"/api/learn/notes/{note['id']}/propose",
        json={"action": action, "selection": "Containers use bridge networks.", "count": 1},
    )
    assert result.status_code == 200, result.text
    assert len(captured) == 1 and len(captured[0][0]) == 2
    assert json.loads(captured[0][0][1]["content"])["content"] == "Containers use bridge networks."
    assert captured[0][1]["think"] is False
    assert client.get(f"/api/learn/notes/{note['id']}").json()["content"] == note["content"]


def test_proposal_retry_failure_and_bounds(client, monkeypatch):
    note = create(client)
    calls = []

    async def resolve(task):
        return "local-model"

    async def chat(*args, **kwargs):
        calls.append(1)
        return {"message": {"content": "not json"}}

    monkeypatch.setattr(note_actions.model_prefs, "resolve_task_model", resolve)
    monkeypatch.setattr(note_actions.providers, "chat_once", chat)
    path = f"/api/learn/notes/{note['id']}/propose"
    assert client.post(path, json={"action": "shorter"}).status_code == 422
    assert len(calls) == 2
    assert (
        client.post(path, json={"action": "shorter", "selection": "Not in note"}).status_code == 422
    )
    long = create(client, content="x" * 12001)
    assert (
        client.post(
            f"/api/learn/notes/{long['id']}/propose", json={"action": "shorter"}
        ).status_code
        == 422
    )
    assert len(calls) == 2

    async def fail(*args, **kwargs):
        raise RuntimeError("offline")

    monkeypatch.setattr(note_actions.providers, "chat_once", fail)
    assert client.post(path, json={"action": "shorter"}).status_code == 502


@pytest.mark.asyncio
async def test_agent_note_creation_uses_trusted_context_and_compact_artifact(database, monkeypatch):
    from app.agent.tools import notes as tool_module

    @asynccontextmanager
    async def factory():
        with Session(database, expire_on_commit=False) as db:
            yield AsyncAdapter(db)

    monkeypatch.setattr(tool_module, "async_session_factory", factory)
    tool = tool_module.CreateStudyNoteTool()
    assert set(tool.get_required_params()) == {"title", "content"}
    assert "conversation_id" not in tool.get_parameters()
    result = await tool.execute(title="Networking", content="## Bridge\nConnected containers.")
    assert result.success
    artifact = json.loads(result.output)
    assert artifact["type"] == "study_note" and "content" not in artifact
    with Session(database) as db:
        assert db.get(StudyNote, uuid.UUID(artifact["note_id"])).content.startswith("## Bridge")

    result = await tool.execute(
        title="Conversation notes",
        content="## Concepts\nSummary.",
        source_page=1,
        source_chunk_id="AGI_note_2026_001",
    )
    assert result.success
    artifact = json.loads(result.output)
    assert "warning" in artifact
    with Session(database) as db:
        saved = db.get(StudyNote, uuid.UUID(artifact["note_id"]))
        assert saved.source_page is None and saved.source_chunk_id is None

    for document_id in ("", "   ", "invented", "rag_search_context"):
        result = await tool.execute(
            title="Conversation summary",
            content="Summary.",
            source_document_id=document_id,
            source_chunk_id="chunk_agi_overview_2026",
            source_page=1,
        )
        assert result.success, result.output
        artifact = json.loads(result.output)
        with Session(database) as db:
            saved = db.get(StudyNote, uuid.UUID(artifact["note_id"]))
            assert saved.source_document_id is None
            assert saved.source_chunk_id is None and saved.source_page is None
