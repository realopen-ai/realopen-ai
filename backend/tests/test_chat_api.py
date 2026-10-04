"""
API-level tests for app/api/chat.py — the core chat + conversations API.

What is exercised (httpx.ASGITransport against the real chat router):
  - POST /chat                    non-streaming chat (model resolution, DB
                                  persistence, provider error mapping)
  - POST /chat/stream             SSE agent streaming: verbatim chunk
                                  pass-through, assistant snapshot persistence,
                                  memory extraction + auto-title + summary
                                  side effects, agent failure path, in-stream
                                  error events, malformed SSE chunks,
                                  cancellation → interrupted, debug logging,
                                  persistence failure survival
  - POST /chat/stream/multipart    image base64 encoding, private document
                                  RAG digestion with live progress, bad JSON
                                  fallback, digest failure, slow digestion
                                  (queue timeouts), unreadable upload
  - POST /chat/stream/{id}/stop    400 invalid UUID / 404 no stream / 200
  - GET  /chat/stream/{id}/events  400 invalid UUID / 404 not resumable / 200
  - GET/POST/PATCH/DELETE          conversation CRUD + POST /conversations/search
  - GET /models                    Ollama tags passthrough + ConnectError fallback

What is mocked — NOTHING reaches Ollama/LLM providers or Postgres:
  - app.api.chat.conv_service      → recording namespace (in-memory store)
  - app.api.chat.model_prefs       → resolve_chat_request_model stub
  - app.api.chat.providers         → chat_once / provider_of stubs
  - app.api.chat.run_agent_stream  → scripted SSE event generator
  - persist/update_message_standalone, maybe_run_memory_extraction,
    maybe_generate_and_save_title, maybe_summarize_conversation,
    mark_stream_active/idle, start/stop/get_stream → recorders
  - app.api.chat.httpx             → fake AsyncClient (GET /models only)
  - get_db dependency              → override returning a MagicMock session
  - async_session_factory          → in-memory context-manager factory
"""

import asyncio
import base64
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import FastAPI

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import chat as chat_module  # noqa: E402
from app.config import settings  # noqa: E402
from app.db.session import get_db  # noqa: E402

app = FastAPI()
app.include_router(chat_module.router, prefix="/api")

CONV_ID = uuid.uuid4()
MSG_ID = uuid.uuid4()
DOC_ID = uuid.uuid4()


# ══════════════════════════════════════════════════════════════════════
# Fakes
# ══════════════════════════════════════════════════════════════════════


class SessionCtx:
    """async_session_factory() replacement returning a fixed session."""

    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


def make_conv(title="Test Chat", model=None, **kw):
    now = datetime.utcnow()
    data = dict(
        id=CONV_ID,
        title=title,
        model=model,
        sandbox_id=None,
        created_at=now,
        updated_at=now,
        pinned=False,
        archived=False,
        pinned_at=None,
        archived_at=None,
        summary=None,
        summary_at=None,
    )
    data.update(kw)
    return SimpleNamespace(**data)


def make_msg(role="user", content="hi", **kw):
    data = dict(
        id=uuid.uuid4(),
        conversation_id=CONV_ID,
        role=role,
        content=content,
        model=None,
        tokens=None,
        has_image=False,
        has_document=False,
        image_count=0,
        document_count=0,
        blocks=None,
        generation_duration=None,
        deliverables=None,
        modality=None,
        completion_status="completed",
        created_at=datetime.utcnow(),
    )
    data.update(kw)
    return SimpleNamespace(**data)


class FakeActiveStream:
    """Stand-in for chat_streams.ActiveChatStream (replays drained events)."""

    def __init__(self, events, persisted_through=0, done=False):
        self.events = events
        self.persisted_through = persisted_through
        self.done = done

    def subscribe(self, after=0):
        async def _gen():
            for ev in self.events[after:]:
                yield ev

        return _gen()


def default_agent_events():
    """A full agent turn: thinking → text → tool call → rag sources → done."""
    return [
        {"event": "thinking_start"},
        {"event": "thinking", "thinking": "pondering"},
        {"event": "thinking_done", "thinkingDuration": 2},
        {"event": "message", "message": {"content": "Hello "}},
        {"event": "message", "message": {"content": "world"}},
        {
            "event": "tool_call",
            "tool_call": {
                "id": "tc1",
                "type": "websearch",
                "status": "running",
                "title": "Search",
            },
        },
        {
            "event": "tool_call",
            "tool_call": {"id": "tc1", "status": "completed", "result": "ok"},
        },
        {
            "event": "rag_sources",
            "tool_call_id": "tc1",
            "sources": [{"filename": "doc.pdf"}],
        },
        {"event": "message", "message": {"content": "the answer"}},
        {"event": "generation_done", "generationDuration": 5},
        {"event": "done"},
    ]


def sse_payloads(body: str):
    """Split an SSE body into raw `data: ...` payload strings."""
    out = []
    for frame in body.split("\n\n"):
        for line in frame.splitlines():
            if line.startswith("data: "):
                out.append(line[6:])
    return out


def sse_events(body: str):
    """JSON-decoded SSE event objects (non-JSON payloads skipped)."""
    events = []
    for payload in sse_payloads(body):
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return events


# ══════════════════════════════════════════════════════════════════════
# Fixture: patched environment
# ══════════════════════════════════════════════════════════════════════


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.fixture
def db_override():
    session = MagicMock(name="request-scoped-db")
    app.dependency_overrides[get_db] = lambda: SessionCtx(session)
    yield session
    app.dependency_overrides.clear()


@pytest.fixture
def env(monkeypatch, db_override):
    """Install recording fakes for every external of app.api.chat."""
    e = SimpleNamespace(
        # agent
        agent_events=default_agent_events(),
        agent_error=None,
        agent_calls=[],
        # persistence
        persists=[],
        updates=[],
        persist_result=MSG_ID,
        summarize_error=None,
        # memory / title / summary
        memory_calls=[],
        memory_result=(2, True, False),
        memory_error=None,
        title_calls=[],
        title_result="Auto Title",
        title_error=None,
        summarize_calls=[],
        summarize_result=True,
        summ_sessions=[],
        # stream lifecycle
        marks=[],
        start_stream_calls=[],
        stop_calls=[],
        stop_result=True,
        resume_stream=None,
        resume_after=None,
        # provider layer
        resolved_model="qwen3:4b",
        chat_once_calls=[],
        chat_once_error=None,
        chat_once_result={
            "message": {"role": "assistant", "content": "pong"},
            "model": "qwen3:4b",
            "done": True,
            "total_duration": 12345,
            "eval_count": 42,
        },
        # conversation service
        conv=make_conv(),
        second_conv=make_conv(title="Second"),
        list_kwargs=None,
        list_result=None,
        created=[],
        create_result=None,
        get_result=None,
        messages_result=[make_msg(role="assistant", content="prev answer")],
        add_message_calls=[],
        deleted=[],
        title_updates=[],
        flags_result=True,
        flags_calls=[],
        # search
        search_result=[{"content": "hit", "rank": 0.5}],
        search_error=None,
        search_calls=[],
        # GET /models
        models_payload={"models": [{"name": "qwen3:4b"}]},
        models_error=None,
        models_urls=[],
        # multipart digest
        digest_calls=[],
        digest_error=None,
        digest_delay=0.0,
    )
    e.list_result = [e.conv]

    # ── agent loop ──
    async def fake_agent(*, messages=None, model=None, images=None,
                         conversation_id=None, **kw):
        e.agent_calls.append(
            {
                "messages": messages,
                "model": model,
                "images": images,
                "conversation_id": conversation_id,
            }
        )
        if e.agent_error is not None:
            raise e.agent_error
        for ev in e.agent_events:
            if isinstance(ev, str):
                # pre-encoded raw SSE chunk (e.g. malformed JSON frames)
                yield ev
            else:
                yield "data: " + json.dumps(ev) + "\n\n"

    monkeypatch.setattr(chat_module, "run_agent_stream", fake_agent)

    # ── persistence (independent sessions) ──
    async def fake_persist(conv_id, role, content, model=None, **kwargs):
        call = {"conv_id": conv_id, "role": role, "content": content,
                "model": model}
        call.update(kwargs)
        e.persists.append(call)
        return e.persist_result

    async def fake_update(message_id, *, content=None, blocks=None,
                          generation_duration=None, deliverables=None,
                          completion_status=None, **kw):
        e.updates.append(
            {
                "message_id": message_id,
                "content": content,
                "blocks": blocks,
                "generation_duration": generation_duration,
                "deliverables": deliverables,
                "completion_status": completion_status,
            }
        )
        return True

    monkeypatch.setattr(chat_module, "persist_message_standalone", fake_persist)
    monkeypatch.setattr(chat_module, "update_message_standalone", fake_update)

    # ── memory extraction / titling / summarization ──
    async def fake_memory(conv_id, content, messages):
        e.memory_calls.append(
            {"conv_id": conv_id, "content": content, "messages": messages}
        )
        if e.memory_error is not None:
            raise e.memory_error
        return e.memory_result

    async def fake_title(conv_id, user_message):
        e.title_calls.append({"conv_id": conv_id, "user_message": user_message})
        if e.title_error is not None:
            raise e.title_error
        return e.title_result

    async def fake_summarize(db, conv_id, model, min_messages=None):
        e.summarize_calls.append({"conv_id": conv_id, "model": model})
        if e.summarize_error is not None:
            raise e.summarize_error
        return e.summarize_result

    monkeypatch.setattr(chat_module, "maybe_run_memory_extraction", fake_memory)
    monkeypatch.setattr(chat_module, "maybe_generate_and_save_title", fake_title)
    monkeypatch.setattr(chat_module, "maybe_summarize_conversation", fake_summarize)

    def fake_factory():
        session = AsyncMock(name="standalone-session")
        e.summ_sessions.append(session)
        return SessionCtx(session)

    monkeypatch.setattr(chat_module, "async_session_factory", fake_factory)

    # ── stream lifecycle markers + chat_streams control ──
    async def fake_active(cid):
        e.marks.append(("active", cid))

    async def fake_idle(cid):
        e.marks.append(("idle", cid))

    async def fake_start(conv_id, source):
        e.start_stream_calls.append(conv_id)
        events = [chunk async for chunk in source]
        return FakeActiveStream(events)

    async def fake_stop(cid):
        e.stop_calls.append(cid)
        return e.stop_result

    def fake_get(cid):
        return e.resume_stream

    monkeypatch.setattr(chat_module, "mark_stream_active", fake_active)
    monkeypatch.setattr(chat_module, "mark_stream_idle", fake_idle)
    monkeypatch.setattr(chat_module, "start_stream", fake_start)
    monkeypatch.setattr(chat_module, "stop_stream", fake_stop)
    monkeypatch.setattr(chat_module, "get_stream", fake_get)

    # ── model resolution + provider routing ──
    async def fake_resolve(raw_model):
        return e.resolved_model

    async def fake_chat_once(model, messages, tools=None, timeout=600.0, **kw):
        e.chat_once_calls.append(
            {"model": model, "messages": messages, "timeout": timeout}
        )
        if e.chat_once_error is not None:
            raise e.chat_once_error
        return dict(e.chat_once_result)

    monkeypatch.setattr(
        chat_module,
        "model_prefs",
        SimpleNamespace(resolve_chat_request_model=fake_resolve),
    )
    monkeypatch.setattr(
        chat_module,
        "providers",
        SimpleNamespace(
            chat_once=fake_chat_once,
            provider_of=lambda model: "ollama",
        ),
    )

    # ── conversation service (in-memory recorder) ──
    async def fake_list(db, limit=50, offset=0, archived=None):
        e.list_kwargs = {"limit": limit, "offset": offset, "archived": archived}
        return e.list_result

    async def fake_to_dict(conv):
        return {
            "id": str(conv.id),
            "title": conv.title,
            "model": conv.model,
            "pinned": bool(conv.pinned),
            "archived": bool(conv.archived),
        }

    async def fake_create(db, title="New Chat", model=None):
        e.created.append({"title": title, "model": model})
        e.create_result = make_conv(title=title, model=model)
        return e.create_result

    async def fake_get(db, conv_id):
        return e.get_result

    async def fake_messages(db, conv_id, limit=100):
        return e.messages_result

    async def fake_msg_dict(msg):
        return {"id": str(msg.id), "role": msg.role, "content": msg.content}

    async def fake_add_message(db, conv_id, role, content, model=None, **kw):
        e.add_message_calls.append(
            {"conv_id": conv_id, "role": role, "content": content, "model": model}
        )
        return make_msg(role=role, content=content)

    async def fake_delete(db, conv_id):
        e.deleted.append(conv_id)

    async def fake_update_title(db, conv_id, title):
        e.title_updates.append({"conv_id": conv_id, "title": title})

    async def fake_set_flags(db, conv_id, pinned=None, archived=None):
        e.flags_calls.append({"pinned": pinned, "archived": archived})
        return e.flags_result

    monkeypatch.setattr(
        chat_module,
        "conv_service",
        SimpleNamespace(
            list_conversations=fake_list,
            conversation_to_dict=fake_to_dict,
            create_conversation=fake_create,
            get_conversation=fake_get,
            get_messages=fake_messages,
            message_to_dict=fake_msg_dict,
            add_message=fake_add_message,
            delete_conversation=fake_delete,
            update_conversation_title=fake_update_title,
            set_conversation_flags=fake_set_flags,
        ),
    )

    # ── GET /models fake httpx (real exception classes preserved) ──
    class FakeModelsResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeModelsClient:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            e.models_urls.append(url)
            if e.models_error is not None:
                raise e.models_error
            return FakeModelsResponse(e.models_payload)

    monkeypatch.setattr(
        chat_module,
        "httpx",
        SimpleNamespace(
            AsyncClient=FakeModelsClient,
            ConnectError=httpx.ConnectError,
            HTTPStatusError=httpx.HTTPStatusError,
        ),
    )

    # ── multipart document digestion ──
    async def fake_digest(db, *, file_bytes, filename, scope,
                          conversation_id=None, progress=None, **kw):
        e.digest_calls.append(
            {
                "filename": filename,
                "scope": scope,
                "conversation_id": conversation_id,
                "size": len(file_bytes),
            }
        )
        if e.digest_delay:
            # simulate slow extraction so the progress-drain loop hits its
            # 0.1 s queue timeout while the digest task is still running
            await asyncio.sleep(e.digest_delay)
        if progress is not None:
            progress(
                SimpleNamespace(
                    stage="extracting_text", percent=10, details=filename,
                    document_id=None, total_chunks=0, total_images=0,
                )
            )
            progress(
                SimpleNamespace(
                    stage="done", percent=100, details="ready",
                    document_id=str(DOC_ID), total_chunks=3, total_images=1,
                )
            )
        if e.digest_error is not None:
            raise e.digest_error
        return SimpleNamespace(
            id=DOC_ID, filename=filename, total_chunks=3, total_images=1
        )

    monkeypatch.setattr(
        chat_module.rag_service, "digest_document", fake_digest
    )
    return e


# ══════════════════════════════════════════════════════════════════════
# POST /chat — non-streaming
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_chat_happy_path_with_conversation(client, env):
    r = await client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "ping"}],
            "model": "default",
            "conversation_id": str(CONV_ID),
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "qwen3:4b"
    assert body["message"]["role"] == "assistant"
    assert body["message"]["content"] == "pong"
    assert body["done"] is True
    assert body["total_duration"] == 12345
    assert body["eval_count"] == 42
    assert body["conversation_id"] == str(CONV_ID)

    # user + assistant messages persisted through the request-scoped service
    roles = [c["role"] for c in env.add_message_calls]
    assert roles == ["user", "assistant"]
    assert env.add_message_calls[0]["content"] == "ping"
    assert env.add_message_calls[1]["model"] == "qwen3:4b"

    # auto-title invoked with the last user message
    assert env.title_calls == [
        {"conv_id": CONV_ID, "user_message": "ping"}
    ]


@pytest.mark.asyncio
async def test_chat_without_conversation_skips_persistence(client, env):
    r = await client.post(
        "/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert r.status_code == 200
    assert r.json()["conversation_id"] is None
    assert env.add_message_calls == []
    assert env.title_calls == []


@pytest.mark.asyncio
async def test_chat_invalid_conversation_id_is_ignored(client, env):
    r = await client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "conversation_id": "not-a-uuid",
        },
    )
    assert r.status_code == 200
    assert r.json()["conversation_id"] is None
    assert env.add_message_calls == []


@pytest.mark.asyncio
async def test_chat_model_override_wins(client, env):
    await client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "model": "default",
            "model_override": "qwen3:8b",
        },
    )
    # chat_once receives the RESOLVED model, and only one provider call
    assert len(env.chat_once_calls) == 1
    assert env.chat_once_calls[0]["model"] == "qwen3:4b"


@pytest.mark.asyncio
async def test_chat_missing_messages_is_422(client, env):
    r = await client.post("/api/chat", json={})
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_chat_provider_connect_error_returns_friendly_message(client, env):
    env.chat_once_error = httpx.ConnectError("connection refused")
    r = await client.post(
        "/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["done"] is True
    assert "Cannot connect to AI engine" in body["message"]["content"]
    assert body["conversation_id"] is None


@pytest.mark.asyncio
async def test_chat_provider_http_status_error(client, env):
    request = httpx.Request("POST", "http://provider/api/chat")
    response = httpx.Response(503, request=request)
    env.chat_once_error = httpx.HTTPStatusError(
        "503", request=request, response=response
    )
    r = await client.post(
        "/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    body = r.json()
    assert body["message"]["content"] == "Error from AI engine: 503"


@pytest.mark.asyncio
async def test_chat_title_failure_is_non_fatal(client, env):
    env.title_error = RuntimeError("title LLM down")
    r = await client.post(
        "/api/chat",
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "conversation_id": str(CONV_ID),
        },
    )
    assert r.status_code == 200
    assert r.json()["message"]["content"] == "pong"


# ══════════════════════════════════════════════════════════════════════
# POST /chat/stream — SSE
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stream_without_conversation_passes_chunks_through(client, env):
    r = await client.post(
        "/api/chat/stream",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text
    # agent chunks forwarded verbatim
    assert "thinking_start" in body
    assert "the answer" in body
    assert body.rstrip().endswith("data: [DONE]")
    assert '"response_duration"' in body
    # no conversation → no persistence, no markers, no title
    assert env.agent_calls[0]["conversation_id"] is None
    assert env.persists == []
    assert ("active", str(CONV_ID)) not in env.marks
    assert env.title_calls == []
    assert env.start_stream_calls == []


@pytest.mark.asyncio
async def test_stream_with_conversation_full_lifecycle(client, env):
    r = await client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hi there"}],
            "conversation_id": str(CONV_ID),
        },
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    body = r.text

    # eager user persistence before the response started
    user_calls = [c for c in env.persists if c["role"] == "user"]
    assert len(user_calls) == 1
    assert user_calls[0]["content"] == "hi there"

    # agent invoked with resolved model + conversation id
    assert env.agent_calls[0]["model"] == "qwen3:4b"
    assert env.agent_calls[0]["conversation_id"] == str(CONV_ID)

    # assistant snapshot: first persisted (streaming), then updated (completed)
    assistant_persists = [c for c in env.persists if c["role"] == "assistant"]
    assert len(assistant_persists) == 1
    assert assistant_persists[0]["completion_status"] == "streaming"
    assert assistant_persists[0]["blocks"], "expected reconstructed blocks"
    assert any(b["type"] == "thinking" for b in assistant_persists[0]["blocks"])
    assert any(
        b["type"] == "tool_call" and b["tool_call"].get("sources")
        for b in assistant_persists[0]["blocks"]
    )
    assert env.updates[-1]["completion_status"] == "completed"
    assert env.updates[-1]["message_id"] == MSG_ID

    # memory extraction ran and reported through SSE
    assert len(env.memory_calls) == 1
    assert env.memory_calls[0]["content"].startswith("Hello")
    events = sse_events(body)
    memory_done = [ev for ev in events if ev.get("event") == "memory_extraction_done"]
    assert memory_done and memory_done[0]["count"] == 2

    # summarization session committed
    assert env.summarize_calls == [{"conv_id": CONV_ID, "model": "qwen3:4b"}]
    assert env.summ_sessions[0].commit.await_count == 1

    # auto-title emitted as SSE
    title_events = [ev for ev in events if ev.get("event") == "conversation_title"]
    assert title_events and title_events[0]["title"] == "Auto Title"

    # stream markers + buffered stream started
    assert ("active", str(CONV_ID)) in env.marks
    assert ("idle", str(CONV_ID)) in env.marks
    assert env.start_stream_calls == [str(CONV_ID)]

    # every chunk passed through verbatim + terminators
    for chunk_event in env.agent_events:
        assert json.dumps(chunk_event) in body
    assert body.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_stream_agent_error_yields_error_event(client, env):
    env.agent_error = RuntimeError("agent exploded")
    r = await client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "conversation_id": str(CONV_ID),
        },
    )
    assert r.status_code == 200
    body = r.text
    assert "agent exploded" in body
    events = sse_events(body)

    # snapshot persisted with completion_status="error" via a fresh insert
    assistant_persists = [c for c in env.persists if c["role"] == "assistant"]
    assert len(assistant_persists) == 1
    assert assistant_persists[0]["completion_status"] == "error"
    assert any(b["type"] == "error" for b in assistant_persists[0]["blocks"])

    # memory extraction skipped entirely on a failed turn
    assert env.memory_calls == []
    assert not any(ev.get("event") == "memory_extraction_start" for ev in events)

    # idle marker released, stream still terminates cleanly
    assert ("idle", str(CONV_ID)) in env.marks
    assert body.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_stream_memory_extraction_failure_reports_error(client, env):
    env.memory_error = RuntimeError("watermark read failed")
    r = await client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "conversation_id": str(CONV_ID),
        },
    )
    events = sse_events(r.text)
    failed = [ev for ev in events if ev.get("event") == "memory_extraction_done"]
    assert failed and failed[0] == {
        "event": "memory_extraction_done",
        "count": 0,
        "ran": False,
        "error": "watermark read failed",
    }
    # summarization still attempted afterwards (non-fatal)
    assert env.summarize_calls


@pytest.mark.asyncio
async def test_stream_invalid_conversation_id_is_ignored(client, env):
    r = await client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "conversation_id": "not-a-uuid",
        },
    )
    assert r.status_code == 200
    assert env.persists == []
    assert env.start_stream_calls == []
    assert env.agent_calls[0]["conversation_id"] is None
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_stream_debug_mode_logs_early_chunks(client, env, monkeypatch, capsys):
    monkeypatch.setattr(chat_module, "is_debug", lambda: True)
    r = await client.post(
        "/api/chat/stream", json={"messages": [{"role": "user", "content": "hi"}]}
    )
    assert r.status_code == 200
    # the debug branch logs the first ≤5 chunks + every _log() line to stdout
    out = capsys.readouterr().out
    assert "🔄 generate() started" in out
    assert "📦 yielding chunk #1:" in out
    assert "📦 yielding chunk #5:" in out
    assert "✅ generate() finished — total chunks=11" in out
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_stream_in_band_error_event_fails_the_turn(client, env):
    env.agent_events = default_agent_events() + [
        {"event": "error", "error": "tool crashed"}
    ]
    r = await client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "conversation_id": str(CONV_ID),
        },
    )
    events = sse_events(r.text)
    in_band = [
        ev for ev in events if ev.get("event") == "error" and ev.get("error")
    ]
    assert in_band and in_band[0]["error"] == "tool crashed"

    # the snapshot is finalized with completion_status="error" + error block
    assert env.updates[-1]["completion_status"] == "error"
    assert any(b["type"] == "error" for b in env.updates[-1]["blocks"])

    # a failed turn skips memory extraction entirely
    assert env.memory_calls == []
    assert not any(ev.get("event") == "memory_extraction_start" for ev in events)
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_stream_malformed_sse_chunk_is_forwarded_but_ignored(client, env):
    env.agent_events = [
        {"event": "message", "message": {"content": "ok"}},
        "data: {not json\n\n",
        "data: [DONE-interloper]\n\n",
    ]
    r = await client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "conversation_id": str(CONV_ID),
        },
    )
    assert r.status_code == 200
    # malformed frames pass through verbatim but never reach the block builder
    assert "{not json" in r.text
    assert "[DONE-interloper]" in r.text
    assistant = [c for c in env.persists if c["role"] == "assistant"]
    assert assistant[-1]["content"] == "ok"
    assert env.memory_calls  # turn still counted as completed
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_stream_cancellation_yields_interrupted_event(client, env):
    env.agent_error = asyncio.CancelledError()
    r = await client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "conversation_id": str(CONV_ID),
        },
    )
    events = sse_events(r.text)
    assert events[-1] == {"event": "interrupted"}

    # nothing was produced → no assistant snapshot, no memory, no summary
    assert not any(c["role"] == "assistant" for c in env.persists)
    assert env.updates == []
    assert env.memory_calls == []
    assert env.summarize_calls == []

    # the interrupted turn still releases the stream marker + ends cleanly
    assert ("idle", str(CONV_ID)) in env.marks
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_stream_summarization_failure_is_non_fatal(client, env):
    env.summarize_error = RuntimeError("summary LLM down")
    r = await client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "conversation_id": str(CONV_ID),
        },
    )
    assert r.status_code == 200
    assert env.summarize_calls  # attempted …
    assert env.memory_calls  # … after memory extraction ran
    events = sse_events(r.text)
    # the auto-title SSE event is still the last parsed event of the turn
    assert events[-1]["event"] == "conversation_title"
    assert events[-2]["event"] == "response_duration"
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_stream_title_failure_is_non_fatal(client, env):
    env.title_error = RuntimeError("title LLM down")
    r = await client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "conversation_id": str(CONV_ID),
        },
    )
    events = sse_events(r.text)
    assert not any(ev.get("event") == "conversation_title" for ev in events)
    # the rest of the post-turn pipeline still ran
    assert env.memory_calls
    assert env.summarize_calls
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_stream_persist_failure_is_survived(client, env):
    env.persist_result = None
    r = await client.post(
        "/api/chat/stream",
        json={
            "messages": [{"role": "user", "content": "hi"}],
            "conversation_id": str(CONV_ID),
        },
    )
    assert r.status_code == 200
    # user persist + snapshot at generation_done + final snapshot (all None
    # → the first snapshot id is never established, so the final one retries)
    roles = [c["role"] for c in env.persists]
    assert roles == ["user", "assistant", "assistant"]
    # update path never used because no message id ever existed
    assert env.updates == []
    assert r.text.rstrip().endswith("data: [DONE]")


# ══════════════════════════════════════════════════════════════════════
# POST /chat/stream/multipart
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_multipart_images_encoded_to_base64(client, env):
    image_bytes = b"PNGDATA-fake-image-bytes"
    r = await client.post(
        "/api/chat/stream/multipart",
        data={
            "messages": json.dumps([{"role": "user", "content": "see this"}]),
            "conversation_id": str(CONV_ID),
        },
        files={"images": ("photo.png", image_bytes, "image/png")},
    )
    assert r.status_code == 200
    expected_b64 = base64.b64encode(image_bytes).decode("utf-8")
    assert env.agent_calls[0]["images"] == [expected_b64]

    # eager user persist records image metadata
    user_calls = [c for c in env.persists if c["role"] == "user"]
    assert user_calls[0]["has_image"] is True
    assert user_calls[0]["image_count"] == 1
    assert user_calls[0]["has_document"] is False


@pytest.mark.asyncio
async def test_multipart_document_digestion_with_progress(client, env):
    r = await client.post(
        "/api/chat/stream/multipart",
        data={
            "messages": json.dumps([{"role": "user", "content": "summarize this"}]),
            "conversation_id": str(CONV_ID),
        },
        files={"documents": ("notes.pdf", b"%PDF-fake", "application/pdf")},
    )
    assert r.status_code == 200
    body = r.text
    events = sse_events(body)

    # digestion ran as private scope for this conversation
    assert env.digest_calls == [
        {
            "filename": "notes.pdf",
            "scope": "private",
            "conversation_id": CONV_ID,
            "size": len(b"%PDF-fake"),
        }
    ]

    # progress + done events streamed live
    progress = [ev for ev in events if ev.get("event") == "document_digest_progress"]
    assert [p["stage"] for p in progress] == ["extracting_text", "done"]
    done = [ev for ev in events if ev.get("event") == "document_digest_done"]
    assert done and done[0]["document_id"] == str(DOC_ID)
    assert done[0]["total_chunks"] == 3

    # a system hint about the fresh document was appended for the agent
    agent_messages = env.agent_calls[0]["messages"]
    assert agent_messages[-1]["role"] == "user"
    assert "rag_search" in agent_messages[-1]["content"]
    assert "notes.pdf" in agent_messages[-1]["content"]

    # user persist records document metadata
    user_calls = [c for c in env.persists if c["role"] == "user"]
    assert user_calls[0]["has_document"] is True
    assert user_calls[0]["document_count"] == 1


@pytest.mark.asyncio
async def test_multipart_digest_failure_yields_error_event(client, env):
    env.digest_error = ValueError("bad pdf")
    r = await client.post(
        "/api/chat/stream/multipart",
        data={
            "messages": json.dumps([{"role": "user", "content": "summarize"}]),
            "conversation_id": str(CONV_ID),
        },
        files={"documents": ("broken.pdf", b"%PDF-fake", "application/pdf")},
    )
    events = sse_events(r.text)
    errors = [ev for ev in events if ev.get("event") == "document_digest_error"]
    assert errors and errors[0]["filename"] == "broken.pdf"
    assert "bad pdf" in errors[0]["error"]
    # no digest_done and no hint injected
    assert not [ev for ev in events if ev.get("event") == "document_digest_done"]
    assert "rag_search" not in env.agent_calls[0]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_multipart_invalid_messages_json_falls_back(client, env):
    r = await client.post(
        "/api/chat/stream/multipart",
        data={"messages": '{"broken'},
    )
    assert r.status_code == 200
    assert env.agent_calls[0]["messages"] == [
        {"role": "user", "content": '{"broken'}
    ]


@pytest.mark.asyncio
async def test_multipart_invalid_conversation_id_is_ignored(client, env):
    r = await client.post(
        "/api/chat/stream/multipart",
        data={
            "messages": json.dumps([{"role": "user", "content": "hi"}]),
            "conversation_id": "not-a-uuid",
        },
    )
    assert r.status_code == 200
    assert env.persists == []
    assert env.agent_calls[0]["conversation_id"] is None
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_multipart_in_band_error_and_debug_logging(client, env, monkeypatch):
    monkeypatch.setattr(chat_module, "is_debug", lambda: True)
    env.agent_events = default_agent_events() + [
        {"event": "error", "error": "boom"}
    ]
    r = await client.post(
        "/api/chat/stream/multipart",
        data={
            "messages": json.dumps([{"role": "user", "content": "hi"}]),
            "conversation_id": str(CONV_ID),
        },
    )
    events = sse_events(r.text)
    in_band = [
        ev for ev in events if ev.get("event") == "error" and ev.get("error")
    ]
    assert in_band and in_band[0]["error"] == "boom"
    assert env.updates[-1]["completion_status"] == "error"
    assert any(b["type"] == "error" for b in env.updates[-1]["blocks"])

    # failed turn → memory extraction skipped (cancelled internally)
    assert env.memory_calls == []
    assert not any(ev.get("event") == "memory_extraction_start" for ev in events)
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_multipart_agent_exception_yields_error_event(client, env):
    env.agent_error = RuntimeError("multipart agent exploded")
    r = await client.post(
        "/api/chat/stream/multipart",
        data={
            "messages": json.dumps([{"role": "user", "content": "hi"}]),
            "conversation_id": str(CONV_ID),
        },
    )
    assert "multipart agent exploded" in r.text
    # no generation_done arrived → snapshot is a fresh insert, not an update
    assistant = [c for c in env.persists if c["role"] == "assistant"]
    assert assistant[-1]["completion_status"] == "error"
    assert any(b["type"] == "error" for b in assistant[-1]["blocks"])
    assert env.updates == []
    assert env.memory_calls == []
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_multipart_cancellation_yields_interrupted_event(client, env):
    env.agent_error = asyncio.CancelledError()
    r = await client.post(
        "/api/chat/stream/multipart",
        data={
            "messages": json.dumps([{"role": "user", "content": "hi"}]),
            "conversation_id": str(CONV_ID),
        },
    )
    events = sse_events(r.text)
    assert events[-1] == {"event": "interrupted"}
    assert not any(c["role"] == "assistant" for c in env.persists)
    assert env.memory_calls == []
    assert ("idle", str(CONV_ID)) in env.marks
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_multipart_memory_extraction_failure_reports_error(client, env):
    env.memory_error = RuntimeError("watermark read failed")
    r = await client.post(
        "/api/chat/stream/multipart",
        data={
            "messages": json.dumps([{"role": "user", "content": "hi"}]),
            "conversation_id": str(CONV_ID),
        },
    )
    failed = [
        ev for ev in sse_events(r.text) if ev.get("event") == "memory_extraction_done"
    ]
    assert failed and failed[0]["error"] == "watermark read failed"
    assert env.summarize_calls  # summarization still attempted (non-fatal)
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_multipart_summarization_failure_is_non_fatal(client, env):
    env.summarize_error = RuntimeError("summary LLM down")
    r = await client.post(
        "/api/chat/stream/multipart",
        data={
            "messages": json.dumps([{"role": "user", "content": "hi"}]),
            "conversation_id": str(CONV_ID),
        },
    )
    assert r.status_code == 200
    assert env.summarize_calls
    assert env.memory_calls
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_multipart_title_failure_is_non_fatal(client, env):
    env.title_error = RuntimeError("title LLM down")
    r = await client.post(
        "/api/chat/stream/multipart",
        data={
            "messages": json.dumps([{"role": "user", "content": "hi"}]),
            "conversation_id": str(CONV_ID),
        },
    )
    events = sse_events(r.text)
    assert not any(ev.get("event") == "conversation_title" for ev in events)
    assert env.memory_calls
    assert env.summarize_calls
    assert r.text.rstrip().endswith("data: [DONE]")


@pytest.mark.asyncio
async def test_multipart_slow_digest_survives_queue_timeouts(client, env):
    env.digest_delay = 0.35  # longer than the 0.1 s drain-loop timeout
    r = await client.post(
        "/api/chat/stream/multipart",
        data={
            "messages": json.dumps([{"role": "user", "content": "summarize this"}]),
            "conversation_id": str(CONV_ID),
        },
        files={"documents": ("notes.pdf", b"%PDF-slow", "application/pdf")},
    )
    assert r.status_code == 200
    events = sse_events(r.text)
    # both progress events still arrive after the timeouts
    progress = [ev for ev in events if ev.get("event") == "document_digest_progress"]
    assert [p["stage"] for p in progress] == ["extracting_text", "done"]
    done = [ev for ev in events if ev.get("event") == "document_digest_done"]
    assert done and done[0]["document_id"] == str(DOC_ID)
    assert env.digest_calls[0]["filename"] == "notes.pdf"


@pytest.mark.asyncio
async def test_multipart_malformed_sse_chunk_is_forwarded_but_ignored(client, env):
    env.agent_events = [
        {"event": "message", "message": {"content": "ok"}},
        "data: {not json\n\n",
    ]
    r = await client.post(
        "/api/chat/stream/multipart",
        data={
            "messages": json.dumps([{"role": "user", "content": "hi"}]),
            "conversation_id": str(CONV_ID),
        },
    )
    assert r.status_code == 200
    assert "{not json" in r.text
    assistant = [c for c in env.persists if c["role"] == "assistant"]
    assert assistant[-1]["content"] == "ok"
    assert env.memory_calls
    assert r.text.rstrip().endswith("data: [DONE]")


class ExplodingUpload:
    """UploadFile double whose read() always fails (disk full simulation)."""

    filename = "broken.pdf"

    async def read(self):
        raise OSError("disk full")


@pytest.mark.asyncio
async def test_multipart_unreadable_document_is_skipped(env):
    # direct coroutine call — an unreadable upload cannot be produced via httpx
    # (the client builds the multipart body up-front), so a failing read() has
    # to be injected on the server side.
    response = await chat_module.chat_stream_multipart(
        messages=json.dumps([{"role": "user", "content": "hi"}]),
        model=None,
        model_override=None,
        conversation_id=None,
        images=[],
        documents=[ExplodingUpload()],
    )
    body = "".join([chunk async for chunk in response.body_iterator])

    # the unreadable doc never reached the digest stage
    assert env.digest_calls == []
    assert "disk full" not in body
    # the agent turn still ran on the (empty) doc set
    assert env.agent_calls[0]["messages"] == [{"role": "user", "content": "hi"}]
    assert body.rstrip().endswith("data: [DONE]")


# ══════════════════════════════════════════════════════════════════════
# Stream control endpoints
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stop_stream_invalid_uuid_400(client, env):
    r = await client.post("/api/chat/stream/not-a-uuid/stop")
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid conversation ID"


@pytest.mark.asyncio
async def test_stop_stream_no_active_stream_404(client, env):
    env.stop_result = False
    r = await client.post(f"/api/chat/stream/{CONV_ID}/stop")
    assert r.status_code == 404
    assert r.json()["detail"] == "No active stream"
    assert env.stop_calls == [str(CONV_ID)]


@pytest.mark.asyncio
async def test_stop_stream_active_200(client, env):
    r = await client.post(f"/api/chat/stream/{CONV_ID}/stop")
    assert r.status_code == 200
    assert r.json() == {"stopping": True}


@pytest.mark.asyncio
async def test_resume_stream_invalid_uuid_400(client, env):
    r = await client.get("/api/chat/stream/xyz/events")
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid conversation ID"


@pytest.mark.asyncio
async def test_resume_stream_none_404(client, env):
    r = await client.get(f"/api/chat/stream/{CONV_ID}/events")
    assert r.status_code == 404
    assert r.json()["detail"] == "No resumable stream"


@pytest.mark.asyncio
async def test_resume_stream_finished_404(client, env):
    env.resume_stream = SimpleNamespace(done=True)
    r = await client.get(f"/api/chat/stream/{CONV_ID}/events")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_resume_stream_live_200(client, env):
    stream = FakeActiveStream(
        events=[
            "data: {\"event\": \"message\", \"message\": {\"content\": \"head\"}}\n\n",
            "data: {\"event\": \"message\", \"message\": {\"content\": \"mid\"}}\n\n",
            "data: {\"event\": \"message\", \"message\": {\"content\": \"tail\"}}\n\n",
            "data: [DONE]\n\n",
        ],
        persisted_through=2,
    )
    env.resume_stream = stream

    r = await client.get(f"/api/chat/stream/{CONV_ID}/events")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    # cursor starts at persisted_through → only the live tail is delivered
    assert "tail" in r.text
    assert "head" not in r.text
    assert "mid" not in r.text
    assert r.text.rstrip().endswith("data: [DONE]")

    r2 = await client.get(f"/api/chat/stream/{CONV_ID}/events?after=1")
    assert r2.status_code == 200
    assert "tail" in r2.text
    assert "head" not in r2.text
    assert "[DONE]" in r2.text


# ══════════════════════════════════════════════════════════════════════
# Conversation CRUD
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_conversations_forwards_query_params(client, env):
    r = await client.get(
        "/api/conversations", params={"limit": 7, "offset": 3, "archived": "true"}
    )
    assert r.status_code == 200
    assert env.list_kwargs == {"limit": 7, "offset": 3, "archived": True}
    body = r.json()
    assert body["conversations"][0]["id"] == str(CONV_ID)
    assert body["conversations"][0]["title"] == "Test Chat"


@pytest.mark.asyncio
async def test_list_conversations_empty(client, env):
    env.list_result = []
    r = await client.get("/api/conversations")
    assert r.status_code == 200
    assert r.json() == {"conversations": []}


@pytest.mark.asyncio
async def test_create_conversation_with_params(client, env):
    r = await client.post(
        "/api/conversations", params={"title": "My Chat", "model": "qwen3:4b"}
    )
    assert r.status_code == 200
    assert env.created == [{"title": "My Chat", "model": "qwen3:4b"}]
    assert r.json()["title"] == "My Chat"


@pytest.mark.asyncio
async def test_create_conversation_defaults(client, env):
    r = await client.post("/api/conversations")
    assert r.status_code == 200
    assert env.created == [{"title": "New Chat", "model": None}]


@pytest.mark.asyncio
async def test_get_conversation_with_messages(client, env):
    env.get_result = env.conv
    r = await client.get(f"/api/conversations/{CONV_ID}")
    assert r.status_code == 200
    body = r.json()
    assert body["conversation"]["id"] == str(CONV_ID)
    assert body["messages"][0]["content"] == "prev answer"


@pytest.mark.asyncio
async def test_get_conversation_unknown_404(client, env):
    r = await client.get(f"/api/conversations/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["detail"] == "Conversation not found"


@pytest.mark.asyncio
async def test_delete_conversation(client, env):
    r = await client.delete(f"/api/conversations/{CONV_ID}")
    assert r.status_code == 200
    assert r.json() == {"status": "deleted"}
    assert env.deleted == [CONV_ID]


@pytest.mark.asyncio
async def test_patch_conversation_flags_404_when_not_found(client, env):
    env.flags_result = False
    r = await client.patch(f"/api/conversations/{CONV_ID}", params={"pinned": "true"})
    assert r.status_code == 404
    assert r.json()["detail"] == "Conversation not found"


@pytest.mark.asyncio
async def test_patch_conversation_title_and_pin(client, env):
    env.get_result = env.conv
    r = await client.patch(
        f"/api/conversations/{CONV_ID}",
        params={"title": "Renamed", "pinned": "true", "archived": "false"},
    )
    assert r.status_code == 200
    assert env.title_updates == [{"conv_id": CONV_ID, "title": "Renamed"}]
    assert env.flags_calls == [{"pinned": True, "archived": False}]
    body = r.json()
    assert body["status"] == "updated"
    assert body["conversation"]["title"] == "Test Chat"


@pytest.mark.asyncio
async def test_search_past_conversations(client, env):
    # the endpoint imports search_past_messages lazily from the module
    import app.services.session_search as session_search

    recorded = {}

    async def fake_search(db, query, limit=10, exclude_conversation_id=None):
        recorded["db"] = db
        recorded["query"] = query
        recorded["limit"] = limit
        return env.search_result

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_search, "search_past_messages", fake_search)
    try:
        r = await client.post(
            "/api/conversations/search",
            params={"query": "budget", "limit": 3},
        )
    finally:
        monkeypatch.undo()
    assert r.status_code == 200
    assert r.json() == {
        "results": env.search_result,
        "total": 1,
        "query": "budget",
    }
    assert recorded["query"] == "budget"
    assert recorded["limit"] == 3


@pytest.mark.asyncio
async def test_search_past_conversations_error_500(client, env):
    import app.services.session_search as session_search

    async def failing_search(db, query, limit=10, exclude_conversation_id=None):
        raise RuntimeError("search backend down")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(session_search, "search_past_messages", failing_search)
    try:
        r = await client.post("/api/conversations/search", params={"query": "x"})
    finally:
        monkeypatch.undo()
    assert r.status_code == 500
    assert "search backend down" in r.json()["detail"]


# ══════════════════════════════════════════════════════════════════════
# GET /models
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_models_passes_ollama_payload_through(client, env):
    r = await client.get("/api/models")
    assert r.status_code == 200
    assert r.json() == {"models": [{"name": "qwen3:4b"}]}
    assert env.models_urls == [f"{settings.OLLAMA_BASE_URL}/api/tags"]


@pytest.mark.asyncio
async def test_list_models_connect_error_returns_empty(client, env):
    env.models_error = httpx.ConnectError("down")
    r = await client.get("/api/models")
    assert r.status_code == 200
    assert r.json() == {"models": [], "error": "Ollama not reachable"}


# ══════════════════════════════════════════════════════════════════════
# Helper
# ══════════════════════════════════════════════════════════════════════


def test_parse_uuid_rejects_garbage():
    with pytest.raises(ValueError):
        chat_module._parse_uuid("garbage")
    assert chat_module._parse_uuid(str(CONV_ID)) == CONV_ID


# ══════════════════════════════════════════════════════════════════════
# _log helper
# ══════════════════════════════════════════════════════════════════════


def test_log_helper_is_silent_when_debug_off(monkeypatch, capsys):
    monkeypatch.setattr(chat_module, "is_debug", lambda: False)
    chat_module._log("quiet %s", "msg")
    assert capsys.readouterr().out == ""


def test_log_helper_formats_and_prints_when_debug_on(monkeypatch, capsys):
    monkeypatch.setattr(chat_module, "is_debug", lambda: True)
    chat_module._log("hello %s", "world")
    assert "[chat] hello world" in capsys.readouterr().out


def test_log_helper_falls_back_on_format_mismatch(monkeypatch, capsys):
    # NOTE: logger.debug(msg, *args) with mismatched placeholders would raise
    # inside the logging machinery, so the module logger is neutralised to
    # isolate the print() fallback behaviour under test.
    monkeypatch.setattr(chat_module, "is_debug", lambda: True)
    monkeypatch.setattr(chat_module, "logger", MagicMock())
    chat_module._log("bad %s %s", "onlyone")  # 2 placeholders, 1 arg
    out = capsys.readouterr().out
    assert "[chat] bad %s %s ('onlyone',)" in out
    chat_module.logger.debug.assert_called_once_with("bad %s %s", "onlyone")
