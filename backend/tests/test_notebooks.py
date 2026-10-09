"""Notebook persistence, source isolation and shared-content lifecycle."""

import uuid
import json
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.api.notebooks import router
from app.db.session import get_db
from app.db.models import (
    Conversation,
    Document,
    Artifact,
    ArtifactVersion,
    StudyNote,
    FlashcardDeck,
    Notebook,
    NotebookItem,
)
from app.learn import notebooks
from app.learn.notes import NoteInput, create_note
from app.learn.schemas import DeckInput
from app.learn.service import create_deck
from app.services import artifacts, rag, conversations
from app.agent import service as agent_service
from test_flashcards import AsyncAdapter


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )

    @event.listens_for(engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        for table in ("messages", "sandboxes", "document_chunks"):
            connection.execute(text(f"CREATE TABLE {table} (id UUID PRIMARY KEY)"))
    for model in (
        Conversation,
        Document,
        StudyNote,
        FlashcardDeck,
        Artifact,
        ArtifactVersion,
        Notebook,
        NotebookItem,
    ):
        model.__table__.create(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def client(database):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def dependency():
        with Session(database, expire_on_commit=False) as session:
            try:
                yield AsyncAdapter(session)
                session.commit()
            except Exception:
                session.rollback()
                raise

    app.dependency_overrides[get_db] = dependency
    with TestClient(app) as client:
        yield client


def create(client, title="Physics"):
    response = client.post("/api/learn/notebooks", json={"title": title})
    assert response.status_code == 201, response.text
    return response.json()


def document(database):
    with Session(database) as session:
        conv = Conversation(title="Original source")
        session.add(conv)
        session.flush()
        doc = Document(
            id=uuid.uuid4(),
            filename="Physics.txt",
            original_filename="Physics.txt",
            mime_type="text/plain",
            file_size_bytes=100,
            file_path="documents/source.txt",
            scope="private",
            conversation_id=conv.id,
            digestion_status="ready",
        )
        session.add(doc)
        session.commit()
        return doc.id


def link(client, notebook, kind, target):
    response = client.post(
        f"/api/learn/notebooks/{notebook['id']}/items",
        json={"kind": kind, "target_id": str(target)},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_crud_shared_sources_and_preserving_deletion(client, database):
    first, second = create(client), create(client, "Chemistry")
    doc_id = document(database)
    first = link(client, first, "document", doc_id)
    second = link(client, second, "document", doc_id)
    assert len(link(client, first, "document", doc_id)["items"]) == 1
    renamed = client.put(
        f"/api/learn/notebooks/{first['id']}", json={"title": "Motion", "description": "Mechanics"}
    )
    assert renamed.status_code == 200 and renamed.json()["title"] == "Motion"
    assert client.delete(f"/api/learn/notebooks/{first['id']}").status_code == 204
    assert client.get(f"/api/learn/notebooks/{first['id']}").status_code == 404
    assert len(client.get(f"/api/learn/notebooks/{second['id']}").json()["items"]) == 1
    with Session(database) as session:
        assert session.get(Document, doc_id)
        conv = session.get(Conversation, uuid.UUID(first["conversation_id"]))
        assert conv.archived and not conv.is_notebook


def test_invalid_targets_cross_notebook_mutation_and_unlink(client, database):
    first, second = create(client), create(client)
    for payload in (
        {"kind": "unknown", "target_id": str(uuid.uuid4())},
        {"kind": "document", "target_id": "invented"},
    ):
        assert (
            client.post(f"/api/learn/notebooks/{first['id']}/items", json=payload).status_code
            == 422
        )
    assert (
        client.post(
            f"/api/learn/notebooks/{first['id']}/items",
            json={"kind": "document", "target_id": str(uuid.uuid4())},
        ).status_code
        == 404
    )
    doc_id = document(database)
    first = link(client, first, "document", doc_id)
    item_id = first["items"][0]["id"]
    assert (
        client.patch(
            f"/api/learn/notebooks/{second['id']}/items/{item_id}", json={"selected": False}
        ).status_code
        == 404
    )
    assert client.delete(f"/api/learn/notebooks/{first['id']}/items/{item_id}").status_code == 204
    with Session(database) as session:
        assert session.get(Document, doc_id)


@pytest.mark.asyncio
async def test_selection_filters_document_and_artifact_access(client, database):
    notebook = create(client)
    doc_id = document(database)
    notebook = link(client, notebook, "document", doc_id)
    conv = uuid.UUID(notebook["conversation_id"])
    with Session(database) as session:
        db = AsyncAdapter(session)
        assert await notebooks.selected_document_ids(db, conv) == [doc_id]
        assert await notebooks.permits_artifact(db, conv, doc_id)
        assert not await notebooks.permits_artifact(db, conv, uuid.uuid4())
        assert await notebooks.selected_document_ids(db, uuid.uuid4()) is None
    item_id = notebook["items"][0]["id"]
    assert (
        client.patch(
            f"/api/learn/notebooks/{notebook['id']}/items/{item_id}", json={"selected": False}
        ).status_code
        == 200
    )
    with Session(database) as session:
        db = AsyncAdapter(session)
        assert await notebooks.selected_document_ids(db, conv) == []
        assert (await artifacts.catalog(db, conv, scoped=True))["items"] == []
        with pytest.raises(HTTPException) as error:
            await artifacts.get_artifact(db, doc_id, conv, scoped=True)
        assert error.value.status_code == 403
        with pytest.raises(HTTPException):
            await create_note(
                db,
                NoteInput(
                    title="Not grounded", source_conversation_id=conv, source_document_id=doc_id
                ),
            )


@pytest.mark.asyncio
async def test_no_selected_sources_never_embeds_or_searches(monkeypatch):
    embedding = AsyncMock(side_effect=AssertionError("No sources must mean no retrieval"))
    monkeypatch.setattr(rag, "get_embedding", embedding)
    assert await rag.search_documents(None, "anything", document_ids=[]) == []
    embedding.assert_not_awaited()


@pytest.mark.asyncio
async def test_generated_materials_are_linked_and_keep_sources(client, database):
    notebook = create(client)
    doc_id = document(database)
    notebook = link(client, notebook, "document", doc_id)
    conv = uuid.UUID(notebook["conversation_id"])
    with Session(database, expire_on_commit=False) as session:
        db = AsyncAdapter(session)
        note = await create_note(
            db,
            NoteInput(
                title="Motion",
                content="Acceleration changes velocity.",
                source_conversation_id=conv,
                source_document_id=doc_id,
            ),
        )
        deck = await create_deck(
            db,
            DeckInput(
                title="Motion", cards=[], source_conversation_id=conv, source_document_id=doc_id
            ),
        )
        await notebooks.attach_generated(db, conv, "note", note.id)
        await notebooks.attach_generated(db, conv, "deck", deck.id)
        session.commit()
        note_id, deck_id = note.id, deck.id
    detail = client.get(f"/api/learn/notebooks/{notebook['id']}").json()
    assert {i["kind"] for i in detail["items"]} == {"document", "note", "deck"}
    assert client.delete(f"/api/learn/notebooks/{notebook['id']}").status_code == 204
    with Session(database) as session:
        assert session.get(StudyNote, note_id).source_document_id == doc_id
        assert session.get(FlashcardDeck, deck_id).source_conversation_id == conv


@pytest.mark.asyncio
async def test_notebook_chats_do_not_enter_regular_sidebar(client, database):
    notebook = create(client)
    with Session(database) as session:
        rows = await conversations.list_conversations(AsyncAdapter(session))
        assert uuid.UUID(notebook["conversation_id"]) not in {row.id for row in rows}


def test_source_changes_are_locked_during_generation(client, database, monkeypatch):
    notebook = create(client)
    doc_id = document(database)
    from types import SimpleNamespace

    monkeypatch.setattr("app.api.notebooks.get_stream", lambda _: SimpleNamespace(done=False))
    assert (
        client.post(
            f"/api/learn/notebooks/{notebook['id']}/items",
            json={"kind": "document", "target_id": str(doc_id)},
        ).status_code
        == 409
    )
    assert client.delete(f"/api/learn/notebooks/{notebook['id']}").status_code == 409


@pytest.mark.asyncio
async def test_notebook_conversation_cannot_be_deleted_directly(client, database):
    notebook = create(client)
    with Session(database) as session:
        with pytest.raises(HTTPException) as error:
            await conversations.delete_conversation(
                AsyncAdapter(session), uuid.UUID(notebook["conversation_id"])
            )
        assert error.value.status_code == 409
        assert session.get(Notebook, uuid.UUID(notebook["id"])) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "voice"])
async def test_notebook_agent_uses_selected_catalog_and_no_external_context(
    client, database, monkeypatch, mode
):
    notebook = create(client)
    allowed = document(database)
    rejected = document(database)
    with Session(database) as session:
        session.get(Document, allowed).filename = "Selected source.txt"
        session.get(Document, rejected).filename = "Unchecked source.txt"
        session.commit()
    link(client, notebook, "document", allowed)
    excluded = link(client, notebook, "document", rejected)
    client.patch(
        f"/api/learn/notebooks/{notebook['id']}/items/{excluded['items'][-1]['id']}",
        json={"selected": False},
    )

    @asynccontextmanager
    async def session_factory():
        with Session(database, expire_on_commit=False) as session:
            yield AsyncAdapter(session)

    requests = []

    async def provider(model, messages, tools=None, **kwargs):
        requests.append((messages, tools))
        yield {"content": "Only selected sources.", "done": True}

    memory = AsyncMock(side_effect=AssertionError("Notebook must not query memories"))
    past = AsyncMock(side_effect=AssertionError("Notebook must not query unrelated conversations"))
    monkeypatch.setattr(agent_service, "async_session_factory", session_factory)
    monkeypatch.setattr(agent_service.providers, "stream_chat", provider)
    monkeypatch.setattr(
        agent_service.model_prefs, "resolve_chat_request_model", AsyncMock(return_value="qwen3:4b")
    )
    monkeypatch.setattr(agent_service, "get_relevant_memories", memory)
    monkeypatch.setattr(agent_service, "build_cross_session_context", past)
    monkeypatch.setattr(
        agent_service.config_store,
        "get_all_tool_configs",
        lambda: {
            name: {"enabled": True}
            for name in [
                "rag_search",
                "read_artifact",
                "list_artifacts",
                "create_study_note",
                "create_flashcard_deck",
                "use_websearch",
            ]
        },
    )
    events = [
        event
        async for event in agent_service.run_agent_stream(
            [{"role": "user", "content": "Search the web and create notes about my source"}],
            conversation_id=notebook["conversation_id"],
            interaction_mode=mode,
        )
    ]
    assert events and requests
    prompt = json.dumps(requests[0][0])
    assert "Selected source.txt" in prompt
    assert "Unchecked source.txt" not in prompt
    assert "only its currently selected sources" in prompt
    names = {tool["function"]["name"] for tool in requests[0][1]}
    assert "rag_search" in names and "create_study_note" in names
    assert "use_websearch" not in names and "use_code_exec" not in names
    memory.assert_not_called()
    past.assert_not_called()
