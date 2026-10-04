"""API tests for app/api/memory.py (the /api/memory router).

Follows the established pattern from tests/test_documents_workspace_api.py:
the router is mounted on a bare FastAPI app with prefix="/api" and hit
through httpx's ASGITransport.

What is tested (endpoint contracts):
  - GET  /api/memory                → list + total, category/limit/offset
  - GET  /api/memory/categories     → category counts
  - GET  /api/memory/{id}           → 200 dict / 404
  - POST /api/memory                → 201-ish ok payload, duplicate
                                       short-circuit, empty-text 400,
                                       missing-field 422, ValueError 400
  - PUT  /api/memory/{id}           → 200 ok / 404
  - DELETE /api/memory/{id}         → 200 ok / 404
  - POST /api/memory/{id}/pin       → 200 pinned / 404
  - POST /api/memory/search         → results + total + query echo
  - POST /api/memory/audit          → ok/before/after/removed mapping,
                                       hard failure → 502

What is mocked:
  - app.api.memory.async_session_factory is patched to hand out a fake
    session (no real Postgres).
  - app.api.memory.MemoryManager is patched with a MagicMock whose async
    methods are AsyncMocks — the service layer never runs, so nothing
    reaches Ollama or the DB.
  - app.api.memory.audit_memories is patched for the audit endpoint.
"""

import sys
import uuid
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import memory as memory_api  # noqa: E402
from app.db.models import Memory  # noqa: E402

app = FastAPI()
app.include_router(memory_api.router, prefix="/api")


# ══════════════════════════════════════════════════════════════════════
# Test doubles
# ══════════════════════════════════════════════════════════════════════


class _FakeSessionCtx:
    def __init__(self):
        self.sessions = []

    async def __aenter__(self):
        session = MagicMock()
        session.commit = AsyncMock()
        session.flush = AsyncMock()
        self.sessions.append(session)
        return session

    async def __aexit__(self, *exc):
        return False


def make_memory(**kwargs) -> Memory:
    defaults = dict(
        id=uuid.uuid4(),
        text="User likes pizza",
        category="preference",
        source="user",
        pinned=False,
        uses=3,
        conversation_id=None,
        created_at=datetime(2024, 5, 1, 12, 0, 0),
        updated_at=datetime(2024, 5, 2, 12, 0, 0),
    )
    defaults.update(kwargs)
    return Memory(**defaults)


def make_manager(**overrides):
    mgr = MagicMock()
    mgr.get_all_memories = AsyncMock(return_value=[])
    mgr.get_categories_with_counts = AsyncMock(return_value=[])
    mgr.get_memory_by_id = AsyncMock(return_value=None)
    mgr.find_duplicates = AsyncMock(return_value=[])
    mgr.add_memory = AsyncMock(side_effect=lambda db, text, **kw: make_memory(
        text=text, **{k: v for k, v in kw.items() if k != "source"}
    ))
    mgr.update_memory = AsyncMock(return_value=None)
    mgr.delete_memory = AsyncMock(return_value=False)
    mgr.pin_memory = AsyncMock(return_value=None)
    mgr.search_memories = AsyncMock(return_value=[])
    for key, value in overrides.items():
        setattr(mgr, key, value)
    return mgr


@pytest.fixture
def api():
    """Yield (httpx_client, manager, session_ctx) with the service layer mocked."""
    ctx = _FakeSessionCtx()
    mgr = make_manager()
    with (
        patch.object(memory_api, "async_session_factory", lambda: ctx),
        patch.object(memory_api, "MemoryManager", MagicMock(return_value=mgr)),
    ):
        transport = httpx.ASGITransport(app=app)
        yield (
            httpx.AsyncClient(transport=transport, base_url="http://test"),
            mgr,
            ctx,
        )


# ══════════════════════════════════════════════════════════════════════
# GET /api/memory
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_memories_empty(api):
    http, mgr, _ = api
    r = await http.get("/api/memory")
    assert r.status_code == 200
    assert r.json() == {"memories": [], "total": 0}


@pytest.mark.asyncio
async def test_list_memories_maps_fields(api):
    http, mgr, _ = api
    mem = make_memory()
    mgr.get_all_memories = AsyncMock(return_value=[mem])
    r = await http.get("/api/memory")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    entry = body["memories"][0]
    assert entry["id"] == str(mem.id)
    assert entry["text"] == "User likes pizza"
    assert entry["category"] == "preference"
    assert entry["source"] == "user"
    assert entry["pinned"] is False
    assert entry["uses"] == 3
    assert entry["conversation_id"] is None
    # datetimes serialized as epoch millis
    assert entry["created_at"] == int(datetime(2024, 5, 1, 12).timestamp() * 1000)
    assert entry["updated_at"] == int(datetime(2024, 5, 2, 12).timestamp() * 1000)


@pytest.mark.asyncio
async def test_list_memories_serializes_conversation_id_and_missing_timestamps(api):
    http, mgr, _ = api
    conv_id = uuid.uuid4()
    mem = make_memory(conversation_id=conv_id, created_at=None, updated_at=None)
    mgr.get_all_memories = AsyncMock(return_value=[mem])
    r = await http.get("/api/memory")
    assert r.status_code == 200
    entry = r.json()["memories"][0]
    assert entry["conversation_id"] == str(conv_id)
    # missing datetimes serialize as 0 epoch millis
    assert entry["created_at"] == 0
    assert entry["updated_at"] == 0


@pytest.mark.asyncio
async def test_list_memories_passes_query_params(api):
    http, mgr, _ = api
    r = await http.get("/api/memory", params={"category": "goal", "limit": 5, "offset": 7})
    assert r.status_code == 200
    mgr.get_all_memories.assert_awaited_once()
    kwargs = mgr.get_all_memories.call_args.kwargs
    assert kwargs["category"] == "goal"
    assert kwargs["limit"] == 5
    assert kwargs["offset"] == 7


# ══════════════════════════════════════════════════════════════════════
# GET /api/memory/categories
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_categories(api):
    http, mgr, _ = api
    mgr.get_categories_with_counts = AsyncMock(
        return_value=[{"category": "fact", "count": 2}]
    )
    r = await http.get("/api/memory/categories")
    assert r.status_code == 200
    assert r.json() == {"categories": [{"category": "fact", "count": 2}]}


# ══════════════════════════════════════════════════════════════════════
# GET /api/memory/{id}
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_memory_found(api):
    http, mgr, _ = api
    mem = make_memory()
    mgr.get_memory_by_id = AsyncMock(return_value=mem)
    r = await http.get(f"/api/memory/{mem.id}")
    assert r.status_code == 200
    assert r.json()["memory"]["id"] == str(mem.id)


@pytest.mark.asyncio
async def test_get_memory_not_found_404(api):
    http, mgr, _ = api
    r = await http.get(f"/api/memory/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["detail"] == "Memory not found"


# ══════════════════════════════════════════════════════════════════════
# POST /api/memory
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_add_memory_success(api):
    http, mgr, ctx = api
    r = await http.post(
        "/api/memory", json={"text": "User enjoys hiking", "category": "preference"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["memory"]["text"] == "User enjoys hiking"
    # request defaults: source=user; service args forwarded
    call = mgr.add_memory.call_args
    assert call.args[1] == "User enjoys hiking"
    assert call.kwargs["category"] == "preference"
    assert call.kwargs["source"] == "user"
    # the transaction was committed
    assert ctx.sessions[0].commit.await_count == 1


@pytest.mark.asyncio
async def test_add_memory_duplicate_short_circuits(api):
    http, mgr, ctx = api
    dup = make_memory()
    mgr.find_duplicates = AsyncMock(return_value=[dup])
    r = await http.post("/api/memory", json={"text": "User likes pizza"})
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "ok": True,
        "message": "Memory already exists",
        "duplicate_of": str(dup.id),
    }
    mgr.add_memory.assert_not_awaited()
    assert ctx.sessions[0].commit.await_count == 0


@pytest.mark.asyncio
async def test_add_memory_empty_text_400(api):
    http, mgr, _ = api
    r = await http.post("/api/memory", json={"text": "   "})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"].lower()
    mgr.add_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_memory_missing_text_422(api):
    http, mgr, _ = api
    r = await http.post("/api/memory", json={"category": "fact"})
    assert r.status_code == 422  # pydantic validation
    mgr.add_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_add_memory_value_error_400(api):
    http, mgr, _ = api
    mgr.add_memory = AsyncMock(side_effect=ValueError("Memory text cannot be empty"))
    r = await http.post("/api/memory", json={"text": "x"})
    assert r.status_code == 400
    assert "empty" in r.json()["detail"]


# ══════════════════════════════════════════════════════════════════════
# PUT /api/memory/{id}
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_memory_success(api):
    http, mgr, ctx = api
    updated = make_memory(text="new text", category="goal")
    mgr.update_memory = AsyncMock(return_value=updated)
    r = await http.put(
        f"/api/memory/{updated.id}",
        json={"text": "new text", "category": "goal"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["memory"]["text"] == "new text"
    mgr.update_memory.assert_awaited_once()
    assert ctx.sessions[0].commit.await_count == 1


@pytest.mark.asyncio
async def test_update_memory_not_found_404(api):
    http, mgr, _ = api
    r = await http.put(
        f"/api/memory/{uuid.uuid4()}", json={"text": "new text"}
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "Memory not found"


# ══════════════════════════════════════════════════════════════════════
# DELETE /api/memory/{id}
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_delete_memory_success(api):
    http, mgr, ctx = api
    mem_id = uuid.uuid4()
    mgr.delete_memory = AsyncMock(return_value=True)
    r = await http.delete(f"/api/memory/{mem_id}")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "message": "Memory deleted successfully"}
    assert ctx.sessions[0].commit.await_count == 1


@pytest.mark.asyncio
async def test_delete_memory_not_found_404(api):
    http, mgr, _ = api
    r = await http.delete(f"/api/memory/{uuid.uuid4()}")
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# POST /api/memory/{id}/pin
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_pin_memory_success(api):
    http, mgr, ctx = api
    mem = make_memory(pinned=True)
    mgr.pin_memory = AsyncMock(return_value=mem)
    r = await http.post(f"/api/memory/{mem.id}/pin", json={"pinned": True})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "pinned": True}
    mgr.pin_memory.assert_awaited_once()
    assert ctx.sessions[0].commit.await_count == 1


@pytest.mark.asyncio
async def test_pin_memory_not_found_404(api):
    http, mgr, _ = api
    r = await http.post(
        f"/api/memory/{uuid.uuid4()}/pin", json={"pinned": True}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_pin_memory_missing_body_422(api):
    http, mgr, _ = api
    r = await http.post(f"/api/memory/{uuid.uuid4()}/pin")
    assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════
# POST /api/memory/search
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_search_memories(api):
    http, mgr, _ = api
    mem = make_memory()
    mgr.search_memories = AsyncMock(return_value=[mem])
    r = await http.post(
        "/api/memory/search", json={"query": "pizza", "category": "preference"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["query"] == "pizza"
    assert body["memories"][0]["id"] == str(mem.id)
    mgr.search_memories.assert_awaited_once()
    call = mgr.search_memories.call_args
    assert call.args[1] == "pizza"  # query is positional (db is args[0])
    assert call.kwargs["category"] == "preference"
    assert call.kwargs["limit"] == 20


@pytest.mark.asyncio
async def test_search_memories_empty(api):
    http, mgr, _ = api
    r = await http.post("/api/memory/search", json={"query": "nothing"})
    assert r.status_code == 200
    assert r.json()["total"] == 0


@pytest.mark.asyncio
async def test_search_memories_missing_query_422(api):
    http, mgr, _ = api
    r = await http.post("/api/memory/search", json={})
    assert r.status_code == 422


# ══════════════════════════════════════════════════════════════════════
# POST /api/memory/audit
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_audit_success(api):
    http, mgr, _ = api
    with patch.object(
        memory_api, "audit_memories", new=AsyncMock(return_value={"before": 5, "after": 3})
    ):
        r = await http.post("/api/memory/audit")
    assert r.status_code == 200
    assert r.json() == {
        "ok": True,
        "before": 5,
        "after": 3,
        "removed": 2,
        "already_tidy": False,
    }


@pytest.mark.asyncio
async def test_audit_already_tidy(api):
    http, mgr, _ = api
    result = {"before": 4, "after": 4, "already_tidy": True}
    with patch.object(
        memory_api, "audit_memories", new=AsyncMock(return_value=result)
    ):
        r = await http.post("/api/memory/audit")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["already_tidy"] is True
    assert body["removed"] == 0


@pytest.mark.asyncio
async def test_audit_llm_failed_is_soft_error(api):
    # error WITH before/after counts → HTTP 200 with ok=False
    http, mgr, _ = api
    result = {"before": 4, "after": 4, "error": "llm_failed"}
    with patch.object(
        memory_api, "audit_memories", new=AsyncMock(return_value=result)
    ):
        r = await http.post("/api/memory/audit")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["before"] == 4


@pytest.mark.asyncio
async def test_audit_hard_error_502(api):
    # error WITHOUT before/after counts → HTTP 502
    http, mgr, _ = api
    with patch.object(
        memory_api,
        "audit_memories",
        new=AsyncMock(return_value={"error": "no_model"}),
    ):
        r = await http.post("/api/memory/audit")
    assert r.status_code == 502
    assert "no_model" in r.json()["detail"]
