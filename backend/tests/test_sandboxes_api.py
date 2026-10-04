"""Tests for the sandbox management API (app/api/sandboxes.py).

Scope — every REST endpoint on the router plus the terminal proxy helper:
  - GET/POST   /sandboxes              — list + create (provision mocked)
  - GET        /sandboxes/{id}         — status refresh via sandbox_host
  - POST       /sandboxes/{id}/start|stop|restart — lifecycle actions
  - DELETE     /sandboxes/{id}         — host delete + DB delete
  - PUT/DELETE /sandboxes/link/...     — conversation link/unlink
  - GET        /sandboxes/conversation/{id}
  - POST       /sandboxes/{id}/exec    — command exec + usage accounting
  - GET        /sandboxes/{id}/commands — command history (reversed)
  - POST       /sandboxes/{id}/exec/{cid}/cancel
  - GET/PUT    /sandboxes/{id}/file(s) — file listing / read / write (+quota)
  - POST       /sandboxes/{id}/upload  — multipart upload (size + quota limits)
  - GET        /sandboxes/{id}/download — base64 → binary download
  - GET        /sandboxes/{id}/preview/... and /previews — preview proxy/discovery
  - GET        /sandboxes/{id}/tasks + cancel
  - terminal_proxy() — WebSocket bridge to the sandbox host terminal

Mocks:
  - app.services.sandbox_host.call / preview_get / payload — never a real
    HTTP call to the sandbox runtime; no Docker containers are started.
  - provision_sandbox — no real provisioning.
  - get_db dependency override — an in-memory FakeDB (no Postgres).
  - websockets.connect — a fake host socket for the terminal proxy.
"""

import asyncio
import base64
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.websockets import WebSocketDisconnect

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import sandboxes  # noqa: E402
from app.db import models  # noqa: E402
from app.db.session import get_db  # noqa: E402

app = FastAPI()
app.include_router(sandboxes.router, prefix="/api")


# ── fakes ─────────────────────────────────────────────────────────────


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    """Minimal AsyncSession stand-in backed by in-memory dicts."""

    def __init__(self):
        self.sandboxes = {}
        self.conversations = {}
        self.tasks = {}
        self.execute_results = []
        self.deleted = []
        self.flush_count = 0

    async def get(self, model, obj_id):
        store = {
            models.Sandbox: self.sandboxes,
            models.Conversation: self.conversations,
            models.SandboxTask: self.tasks,
        }.get(model)
        if store is None:
            return None
        return store.get(obj_id)

    async def execute(self, stmt):
        if self.execute_results:
            item = self.execute_results.pop(0)
            return item(stmt) if callable(item) else item
        return FakeResult([])

    async def delete(self, item):
        self.deleted.append(item)

    async def flush(self):
        self.flush_count += 1


def _make_sandbox(**kwargs) -> models.Sandbox:
    defaults = dict(
        id=uuid.uuid4(),
        name="dev",
        status="stopped",
        desired_running=False,
        volume_name=f"vol-{uuid.uuid4().hex[:8]}",
        container_name=f"ctr-{uuid.uuid4().hex[:8]}",
        image="realopenai-sandbox:latest",
        cpu_limit=2.0,
        memory_limit_mb=2048,
        workspace_quota_bytes=2 * 1024**3,
        usage_bytes=0,
        idle_timeout_seconds=1800,
        last_active_at=None,
        error=None,
        created_at=datetime(2024, 1, 1, 12, 0, 0),
        updated_at=datetime(2024, 1, 1, 12, 0, 0),
    )
    defaults.update(kwargs)
    return models.Sandbox(**defaults)


def _make_conversation(**kwargs) -> models.Conversation:
    defaults = dict(id=uuid.uuid4(), title="conv", created_at=datetime(2024, 1, 1))
    defaults.update(kwargs)
    return models.Conversation(**defaults)


def _make_command(sandbox_id, **kwargs) -> models.SandboxCommand:
    defaults = dict(
        id=uuid.uuid4(),
        sandbox_id=sandbox_id,
        task_id=None,
        conversation_id=None,
        source="user",
        tool_name="terminal",
        sequence=1,
        command="ls",
        cwd="/workspace",
        stdout="ok",
        stderr="",
        exit_code=0,
        output_truncated=False,
        started_at=datetime(2024, 1, 1, 12, 0, 0),
        completed_at=datetime(2024, 1, 1, 12, 0, 1),
        duration_ms=1000,
    )
    defaults.update(kwargs)
    return models.SandboxCommand(**defaults)


@pytest_asyncio.fixture
async def env():
    db = FakeDB()
    test_app = FastAPI()
    test_app.include_router(sandboxes.router, prefix="/api")

    async def override_db():
        yield db

    test_app.dependency_overrides[get_db] = override_db
    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        yield client, db


# ── list / create ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_sandboxes_empty(env):
    client, db = env
    db.execute_results.append(FakeResult([]))
    r = await client.get("/api/sandboxes")
    assert r.status_code == 200
    assert r.json() == {"sandboxes": []}


@pytest.mark.asyncio
async def test_list_sandboxes_serializes_rows(env):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb
    db.execute_results.append(FakeResult([sb]))
    r = await client.get("/api/sandboxes")
    body = r.json()["sandboxes"]
    assert len(body) == 1
    assert body[0]["id"] == str(sb.id)
    assert body[0]["name"] == "dev"
    assert body[0]["created_at"] == "2024-01-01T12:00:00"
    assert body[0]["last_active_at"] is None


@pytest.mark.asyncio
async def test_create_sandbox_success(env, monkeypatch):
    client, db = env
    created = _make_sandbox(name="workspace-1", status="running")

    async def fake_provision(db_, **kwargs):
        assert kwargs["name"] == "workspace-1"
        assert kwargs["cpu_limit"] == 4.0
        return created

    monkeypatch.setattr(sandboxes, "provision_sandbox", fake_provision)
    r = await client.post(
        "/api/sandboxes",
        json={"name": "workspace-1", "cpu_limit": 4.0},
    )
    assert r.status_code == 200
    assert r.json()["name"] == "workspace-1"
    assert r.json()["status"] == "running"


@pytest.mark.asyncio
async def test_create_sandbox_with_conversation(env, monkeypatch):
    client, db = env
    conv = _make_conversation()
    db.conversations[conv.id] = conv
    seen = {}

    async def fake_provision(db_, **kwargs):
        seen["conversation"] = kwargs["conversation"]
        return _make_sandbox()

    monkeypatch.setattr(sandboxes, "provision_sandbox", fake_provision)
    r = await client.post(
        "/api/sandboxes", json={"name": "linked", "conversation_id": str(conv.id)}
    )
    assert r.status_code == 200
    assert seen["conversation"] is conv


@pytest.mark.asyncio
async def test_create_sandbox_unknown_conversation_404(env):
    client, db = env
    r = await client.post(
        "/api/sandboxes",
        json={"name": "x", "conversation_id": str(uuid.uuid4())},
    )
    assert r.status_code == 404
    assert r.json()["detail"] == "Conversation not found"


@pytest.mark.asyncio
async def test_create_sandbox_host_failure_503(env, monkeypatch):
    client, db = env

    async def fake_provision(db_, **kwargs):
        raise RuntimeError("docker daemon down")

    monkeypatch.setattr(sandboxes, "provision_sandbox", fake_provision)
    r = await client.post("/api/sandboxes", json={"name": "x"})
    assert r.status_code == 503
    assert "docker daemon down" in r.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"name": ""},  # too short
        {"name": "x", "cpu_limit": 0.1},  # below ge=0.25
        {"name": "x", "memory_limit_mb": 100},  # below ge=256
        {"name": "x", "workspace_quota_bytes": 1024},  # below ge=64MB
        {"name": "x", "idle_timeout_seconds": 10},  # below ge=60
    ],
)
async def test_create_sandbox_validation_422(env, payload):
    client, db = env
    r = await client.post("/api/sandboxes", json=payload)
    assert r.status_code == 422


# ── get / lifecycle / delete ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_sandbox_refreshes_status_from_host(env, monkeypatch):
    client, db = env
    sb = _make_sandbox(error="old error")
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        assert action == "status"
        return {"status": "running"}

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.get(f"/api/sandboxes/{sb.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "running"
    assert body["error"] is None


@pytest.mark.asyncio
async def test_get_sandbox_host_error_sets_error_state(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        raise RuntimeError("host timeout")

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.get(f"/api/sandboxes/{sb.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "error"
    assert "host timeout" in body["error"]


@pytest.mark.asyncio
async def test_get_sandbox_unknown_404(env):
    client, db = env
    r = await client.get(f"/api/sandboxes/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action,host_status,desired",
    [
        ("start", "running", True),
        ("stop", "stopped", False),
        ("restart", "running", True),
    ],
)
async def test_lifecycle_actions(env, monkeypatch, action, host_status, desired):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb
    calls = []

    async def fake_call(host_action, item, extra=None, timeout=180.0):
        calls.append(host_action)
        return {"status": host_status}

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.post(f"/api/sandboxes/{sb.id}/{action}")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == host_status
    assert body["desired_running"] is desired
    assert body["error"] is None
    assert calls == [action]
    assert db.flush_count == 1
    assert sb.last_active_at is not None


@pytest.mark.asyncio
async def test_lifecycle_host_failure_503(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    async def fake_call(host_action, item, extra=None, timeout=180.0):
        raise RuntimeError("cannot start")

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.post(f"/api/sandboxes/{sb.id}/start")
    assert r.status_code == 503
    assert sb.status == "error"
    assert "cannot start" in sb.error


@pytest.mark.asyncio
async def test_lifecycle_unknown_sandbox_404(env, monkeypatch):
    client, db = env
    monkeypatch.setattr(
        sandboxes.sandbox_host, "call", AsyncMock(return_value={"status": "running"})
    )
    r = await client.post(f"/api/sandboxes/{uuid.uuid4()}/start")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_sandbox_removes_host_and_row(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb
    calls = []

    async def fake_call(host_action, item, extra=None, timeout=180.0):
        calls.append(host_action)
        return {"deleted": True}

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.delete(f"/api/sandboxes/{sb.id}")
    assert r.status_code == 200
    assert r.json() == {"deleted": True}
    assert calls == ["delete"]
    assert db.deleted == [sb]


@pytest.mark.asyncio
async def test_delete_sandbox_host_failure_503(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    async def fake_call(host_action, item, extra=None, timeout=180.0):
        raise RuntimeError("volume in use")

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.delete(f"/api/sandboxes/{sb.id}")
    assert r.status_code == 503
    assert db.deleted == []  # DB row kept when host delete fails


@pytest.mark.asyncio
async def test_delete_sandbox_unknown_404(env, monkeypatch):
    client, db = env
    monkeypatch.setattr(
        sandboxes.sandbox_host, "call", AsyncMock(return_value={})
    )
    r = await client.delete(f"/api/sandboxes/{uuid.uuid4()}")
    assert r.status_code == 404


# ── conversation linking ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_conversation(env):
    client, db = env
    sb = _make_sandbox()
    conv = _make_conversation()
    db.sandboxes[sb.id] = sb
    db.conversations[conv.id] = conv

    r = await client.put(f"/api/sandboxes/{sb.id}/link/{conv.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["linked"] is True
    assert conv.sandbox_id == sb.id


@pytest.mark.asyncio
async def test_link_conversation_unknown_sandbox_404(env):
    client, db = env
    conv = _make_conversation()
    db.conversations[conv.id] = conv
    r = await client.put(f"/api/sandboxes/{uuid.uuid4()}/link/{conv.id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_link_conversation_unknown_conversation_404(env):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb
    r = await client.put(f"/api/sandboxes/{sb.id}/link/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_unlink_conversation(env):
    client, db = env
    sb = _make_sandbox()
    conv = _make_conversation(sandbox_id=sb.id)
    db.conversations[conv.id] = conv
    r = await client.delete(f"/api/sandboxes/link/{conv.id}")
    assert r.status_code == 200
    assert r.json() == {"linked": False}
    assert conv.sandbox_id is None


@pytest.mark.asyncio
async def test_unlink_unknown_conversation_404(env):
    client, db = env
    r = await client.delete(f"/api/sandboxes/link/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_conversation_sandbox_none_when_unlinked(env):
    client, db = env
    conv = _make_conversation()
    db.conversations[conv.id] = conv
    r = await client.get(f"/api/sandboxes/conversation/{conv.id}")
    assert r.status_code == 200
    assert r.json() == {"sandbox": None}


@pytest.mark.asyncio
async def test_conversation_sandbox_linked(env):
    client, db = env
    sb = _make_sandbox(name="shared")
    conv = _make_conversation(sandbox_id=sb.id)
    db.sandboxes[sb.id] = sb
    db.conversations[conv.id] = conv
    r = await client.get(f"/api/sandboxes/conversation/{conv.id}")
    assert r.status_code == 200
    assert r.json()["sandbox"]["id"] == str(sb.id)


@pytest.mark.asyncio
async def test_conversation_sandbox_missing_row_404(env):
    client, db = env
    sb = _make_sandbox()
    conv = _make_conversation(sandbox_id=sb.id)  # sandbox row never inserted
    db.conversations[conv.id] = conv
    r = await client.get(f"/api/sandboxes/conversation/{conv.id}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_conversation_sandbox_unknown_conversation_404(env):
    client, db = env
    r = await client.get(f"/api/sandboxes/conversation/{uuid.uuid4()}")
    assert r.status_code == 404


# ── exec / command history ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_exec_command_success(env, monkeypatch):
    client, db = env
    sb = _make_sandbox(usage_bytes=100)
    db.sandboxes[sb.id] = sb
    calls = []

    async def fake_call(action, item, extra=None, timeout=180.0):
        calls.append((action, extra, timeout))
        if action == "exec":
            return {"exit_code": 0, "stdout": "hi", "stderr": ""}
        if action == "usage":
            return {"usage_bytes": 500}
        raise AssertionError(f"unexpected action {action}")

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.post(
        f"/api/sandboxes/{sb.id}/exec",
        json={"command": "echo hi", "timeout": 60},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["stdout"] == "hi"
    assert body["usage_bytes"] == 500
    assert body["quota_exceeded"] is False

    exec_action, extra, timeout = calls[0]
    assert exec_action == "exec"
    assert extra["command"] == "echo hi"
    assert extra["command_id"]  # auto-generated uuid string
    assert timeout == 80  # timeout + 20s headroom


@pytest.mark.asyncio
async def test_exec_command_quota_exceeded_flag(env, monkeypatch):
    client, db = env
    sb = _make_sandbox(workspace_quota_bytes=1000)
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        if action == "exec":
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        return {"usage_bytes": 5000}

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.post(
        f"/api/sandboxes/{sb.id}/exec",
        json={"command": "dd if=/dev/zero", "command_id": "cmd-42"},
    )
    body = r.json()
    assert body["quota_exceeded"] is True
    assert body["usage_bytes"] == 5000


@pytest.mark.asyncio
async def test_exec_command_unknown_sandbox_404(env, monkeypatch):
    client, db = env
    monkeypatch.setattr(
        sandboxes.sandbox_host, "call", AsyncMock(return_value={})
    )
    r = await client.post(
        f"/api/sandboxes/{uuid.uuid4()}/exec", json={"command": "ls"}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"command": ""},  # too short
        {"command": "x", "timeout": 0},  # below ge=1
        {"command": "x", "timeout": 9999},  # above le=1800
        {"command": "x", "command_id": "y" * 81},  # too long
    ],
)
async def test_exec_command_validation_422(env, payload):
    client, db = env
    r = await client.post(f"/api/sandboxes/{uuid.uuid4()}/exec", json=payload)
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_command_history_returns_oldest_first(env):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb
    # Host returns newest-first (ordered by started_at desc in the query)
    newest = _make_command(sb.id, command="third", sequence=3)
    oldest = _make_command(sb.id, command="first", sequence=1)
    db.execute_results.append(FakeResult([newest, oldest]))

    r = await client.get(f"/api/sandboxes/{sb.id}/commands")
    assert r.status_code == 200
    commands = r.json()["commands"]
    assert [c["command"] for c in commands] == ["first", "third"]
    entry = commands[0]
    assert entry["cwd"] == "/workspace"
    assert entry["exit_code"] == 0
    assert entry["started_at"] == "2024-01-01T12:00:00"
    assert entry["task_id"] is None


@pytest.mark.asyncio
async def test_command_history_unknown_sandbox_404(env):
    client, db = env
    r = await client.get(f"/api/sandboxes/{uuid.uuid4()}/commands")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_command_history_limit_validation(env):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb
    r = await client.get(f"/api/sandboxes/{sb.id}/commands?limit=0")
    assert r.status_code == 422
    r = await client.get(f"/api/sandboxes/{sb.id}/commands?limit=5000")
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_cancel_command_forwards_to_host(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb
    calls = []

    async def fake_call(action, item, extra=None, timeout=180.0):
        calls.append((action, extra))
        return {"cancelled": True}

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.post(f"/api/sandboxes/{sb.id}/exec/cmd-7/cancel")
    assert r.status_code == 200
    assert r.json() == {"cancelled": True}
    assert calls[0][0] == "exec/cancel"
    assert calls[0][1]["command_id"] == "cmd-7"


# ── files ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_files_listing(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        assert action == "files"
        assert extra == {"path": "/workspace/src"}
        return {"entries": [{"name": "main.py", "type": "file"}]}

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.get(f"/api/sandboxes/{sb.id}/files?path=/workspace/src")
    assert r.status_code == 200
    assert r.json()["entries"][0]["name"] == "main.py"


@pytest.mark.asyncio
async def test_read_file(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        assert action == "files/read"
        return {"content": "hello"}

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.get(f"/api/sandboxes/{sb.id}/file?path=/workspace/a.txt")
    assert r.status_code == 200
    assert r.json() == {"content": "hello"}


@pytest.mark.asyncio
async def test_write_file_success(env, monkeypatch):
    client, db = env
    sb = _make_sandbox(usage_bytes=100)
    db.sandboxes[sb.id] = sb
    calls = []

    async def fake_call(action, item, extra=None, timeout=180.0):
        calls.append((action, extra))
        if action == "usage":
            return {"usage_bytes": 100}
        if action == "files/stat":
            return {"size": 0}
        if action == "files/write":
            return {"written": True}
        raise AssertionError(action)

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.put(
        f"/api/sandboxes/{sb.id}/file",
        json={"path": "/workspace/a.txt", "content": "x" * 50},
    )
    assert r.status_code == 200
    assert r.json() == {"written": True}
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_write_file_quota_exceeded_413(env, monkeypatch):
    client, db = env
    sb = _make_sandbox(workspace_quota_bytes=200, usage_bytes=100)
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        if action == "usage":
            return {"usage_bytes": 100}
        if action == "files/stat":
            return {"size": 0}
        raise AssertionError(action)

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.put(
        f"/api/sandboxes/{sb.id}/file",
        json={"path": "/workspace/big.txt", "content": "x" * 500},
    )
    assert r.status_code == 413
    assert "quota" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_write_file_replaces_existing_size(env, monkeypatch):
    client, db = env
    # usage 5000, replacing a 4500-byte file with 600 bytes → fits in 1KB quota
    sb = _make_sandbox(workspace_quota_bytes=1024, usage_bytes=5000)
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        if action == "usage":
            return {"usage_bytes": 5000}
        if action == "files/stat":
            return {"size": 4500}
        if action == "files/write":
            return {"written": True}
        raise AssertionError(action)

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.put(
        f"/api/sandboxes/{sb.id}/file",
        json={"path": "/workspace/a.txt", "content": "y" * 400},
    )
    assert r.status_code == 200


# ── upload / download ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_file_success(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb
    writes = []

    async def fake_call(action, item, extra=None, timeout=180.0):
        if action == "usage":
            return {"usage_bytes": 0}
        if action == "files/stat":
            return {"size": 0}
        if action == "files/write":
            writes.append(extra)
            return {"written": True}
        raise AssertionError(action)

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.post(
        f"/api/sandboxes/{sb.id}/upload?path=/workspace/docs",
        files={"upload": ("report.pdf", b"%PDF-1.4 fake", "application/pdf")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == "/workspace/docs/report.pdf"
    # content is forwarded base64-encoded
    assert writes[0]["content_base64"] == base64.b64encode(b"%PDF-1.4 fake").decode()
    assert writes[0]["path"] == "/workspace/docs/report.pdf"


@pytest.mark.asyncio
async def test_upload_file_sanitizes_filename_slashes(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        if action == "usage":
            return {"usage_bytes": 0}
        if action == "files/stat":
            return {"size": 0}
        if action == "files/write":
            return {"written": True}
        raise AssertionError(action)

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.post(
        f"/api/sandboxes/{sb.id}/upload",
        files={"upload": ("../../etc/passwd", b"x", "text/plain")},
    )
    assert r.status_code == 200
    assert r.json()["path"] == "/workspace/.._.._etc_passwd"


@pytest.mark.asyncio
async def test_upload_file_rejects_oversize(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb
    big = b"\x00" * (25 * 1024**2 + 1)

    async def fail_call(action, item, extra=None, timeout=180.0):
        raise AssertionError("host must not be called for oversize upload")

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fail_call)
    r = await client.post(
        f"/api/sandboxes/{sb.id}/upload",
        files={"upload": ("huge.bin", big, "application/octet-stream")},
    )
    assert r.status_code == 413
    assert "25 MB" in r.json()["detail"]


@pytest.mark.asyncio
async def test_upload_file_quota_exceeded_413(env, monkeypatch):
    client, db = env
    sb = _make_sandbox(workspace_quota_bytes=10)
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        if action == "usage":
            return {"usage_bytes": 0}
        if action == "files/stat":
            return {"size": 0}
        raise AssertionError(action)

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.post(
        f"/api/sandboxes/{sb.id}/upload",
        files={"upload": ("a.txt", b"way more than ten bytes", "text/plain")},
    )
    assert r.status_code == 413
    assert "quota" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_download_file_decodes_base64(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb
    payload = b"binary\x00payload"

    async def fake_call(action, item, extra=None, timeout=180.0):
        assert action == "files/read"
        return {"content_base64": base64.b64encode(payload).decode()}

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.get(f"/api/sandboxes/{sb.id}/download?path=/workspace/out.bin")
    assert r.status_code == 200
    assert r.content == payload
    assert r.headers["content-type"] == "application/octet-stream"
    assert 'filename="out.bin"' in r.headers["content-disposition"]


# ── preview proxy + discovery ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_preview_proxies_allowed_headers_only(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    fake_response = SimpleNamespace(
        headers={"Content-Type": "text/html", "X-Drop": "yes", "Etag": "abc"},
        content=b"<html></html>",
        status_code=200,
    )

    async def fake_preview_get(item, port, path, query=""):
        assert port == 6767
        assert path == "index.html"
        return fake_response

    monkeypatch.setattr(sandboxes.sandbox_host, "preview_get", fake_preview_get)
    r = await client.get(f"/api/sandboxes/{sb.id}/preview/6767/index.html?a=b")
    assert r.status_code == 200
    assert r.content == b"<html></html>"
    assert r.headers["content-type"] == "text/html"
    assert "x-drop" not in r.headers


@pytest.mark.asyncio
async def test_discover_previews_status_error_returns_empty(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        raise RuntimeError("host down")

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.get(f"/api/sandboxes/{sb.id}/previews")
    assert r.status_code == 200
    assert r.json() == {"available": [], "preferred": None}


@pytest.mark.asyncio
async def test_discover_previews_not_running_returns_empty(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        return {"running": False}

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    r = await client.get(f"/api/sandboxes/{sb.id}/previews")
    assert r.json() == {"available": [], "preferred": None}


@pytest.mark.asyncio
async def test_discover_previews_prefers_6767(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        return {"running": True}

    async def fake_preview_get(item, port, path, query=""):
        if port == 6767:
            return SimpleNamespace(status_code=200, headers={}, content=b"")
        raise RuntimeError("6969 closed")

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    monkeypatch.setattr(sandboxes.sandbox_host, "preview_get", fake_preview_get)
    r = await client.get(f"/api/sandboxes/{sb.id}/previews")
    body = r.json()
    assert [p["port"] for p in body["available"]] == [6767]
    assert body["preferred"]["port"] == 6767
    assert body["preferred"]["url"] == f"/api/sandboxes/{sb.id}/preview/6767/"


@pytest.mark.asyncio
async def test_discover_previews_falls_back_to_first(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        return {"running": True}

    async def fake_preview_get(item, port, path, query=""):
        if port == 6969:
            return SimpleNamespace(status_code=404, headers={}, content=b"")
        raise RuntimeError("6767 down")

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    monkeypatch.setattr(sandboxes.sandbox_host, "preview_get", fake_preview_get)
    r = await client.get(f"/api/sandboxes/{sb.id}/previews")
    body = r.json()
    assert [p["port"] for p in body["available"]] == [6969]  # 404 < 500 → healthy
    assert body["preferred"]["port"] == 6969


@pytest.mark.asyncio
async def test_discover_previews_server_error_excluded(env, monkeypatch):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    async def fake_call(action, item, extra=None, timeout=180.0):
        return {"running": True}

    async def fake_preview_get(item, port, path, query=""):
        return SimpleNamespace(status_code=502, headers={}, content=b"")

    monkeypatch.setattr(sandboxes.sandbox_host, "call", fake_call)
    monkeypatch.setattr(sandboxes.sandbox_host, "preview_get", fake_preview_get)
    r = await client.get(f"/api/sandboxes/{sb.id}/previews")
    assert r.json() == {"available": [], "preferred": None}


# ── tasks ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_tasks_listing(env):
    client, db = env
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb
    task = models.SandboxTask(
        id=uuid.uuid4(),
        sandbox_id=sb.id,
        status="completed",
        request="build the thing",
        summary="done",
        worklog=[{"step": 1}],
        files_changed=["a.py"],
        tests_run=["pytest"],
        created_at=datetime(2024, 1, 1, 12, 0, 0),
    )
    db.execute_results.append(FakeResult([task]))
    r = await client.get(f"/api/sandboxes/{sb.id}/tasks")
    assert r.status_code == 200
    tasks = r.json()["tasks"]
    assert tasks[0]["status"] == "completed"
    assert tasks[0]["request"] == "build the thing"
    assert tasks[0]["worklog"] == [{"step": 1}]
    assert tasks[0]["created_at"] == "2024-01-01T12:00:00"


@pytest.mark.asyncio
async def test_cancel_task_success(env):
    client, db = env
    sb = _make_sandbox()
    task = models.SandboxTask(
        id=uuid.uuid4(), sandbox_id=sb.id, request="r", created_at=datetime(2024, 1, 1)
    )
    db.tasks[task.id] = task
    r = await client.post(f"/api/sandboxes/{sb.id}/tasks/{task.id}/cancel")
    assert r.status_code == 200
    assert r.json() == {"cancellation_requested": True}
    assert task.cancellation_requested is True


@pytest.mark.asyncio
async def test_cancel_task_wrong_sandbox_404(env):
    client, db = env
    sb = _make_sandbox()
    other = _make_sandbox()
    task = models.SandboxTask(
        id=uuid.uuid4(), sandbox_id=other.id, request="r", created_at=datetime(2024, 1, 1)
    )
    db.tasks[task.id] = task
    r = await client.post(f"/api/sandboxes/{sb.id}/tasks/{task.id}/cancel")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_cancel_task_unknown_404(env):
    client, db = env
    sb = _make_sandbox()
    r = await client.post(f"/api/sandboxes/{sb.id}/tasks/{uuid.uuid4()}/cancel")
    assert r.status_code == 404


# ── terminal proxy ────────────────────────────────────────────────────


class FakeWebSocket:
    def __init__(self, initial_config, events=None):
        self.initial_config = initial_config
        self.events = list(events or [])
        self.accepted = False
        self.close_code = None
        self.sent = []
        self._initial_consumed = False

    async def accept(self):
        self.accepted = True

    async def close(self, code=None):
        self.close_code = code

    async def receive_text(self):
        return self.initial_config

    async def receive(self):
        if self.events:
            event = self.events.pop(0)
            if isinstance(event, Exception):
                raise event
            return event
        raise WebSocketDisconnect(code=1000)

    async def send_bytes(self, data):
        self.sent.append(("bytes", data))

    async def send_text(self, text):
        self.sent.append(("text", text))


class FakeHostSocket:
    """Stand-in for the websockets.connect() context manager target."""

    def __init__(self, incoming=None):
        self.incoming = list(incoming or [])
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, message):
        self.sent.append(message)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.incoming:
            return self.incoming.pop(0)
        raise StopAsyncIteration


@pytest.mark.asyncio
async def test_terminal_proxy_invalid_uuid_closes_4404():
    ws = FakeWebSocket("{}")
    await sandboxes.terminal_proxy(ws, "not-a-uuid", FakeDB())
    assert ws.close_code == 4404
    assert ws.accepted is False


@pytest.mark.asyncio
async def test_terminal_proxy_unknown_sandbox_closes_4404():
    ws = FakeWebSocket("{}")
    await sandboxes.terminal_proxy(ws, str(uuid.uuid4()), FakeDB())
    assert ws.close_code == 4404
    assert ws.accepted is False


@pytest.mark.asyncio
async def test_terminal_proxy_bridges_messages(monkeypatch):
    db = FakeDB()
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    host = FakeHostSocket(incoming=["hello from host", b"\x01\x02"])

    def fake_connect(url, max_size=None):
        assert url.endswith(f"/v1/sandboxes/{sb.id}/terminal")
        return host

    monkeypatch.setattr(sandboxes.websockets, "connect", fake_connect)

    ws = FakeWebSocket(
        initial_config=json.dumps({"cols": 80, "rows": 24}),
        events=[
            {"type": "websocket.receive", "text": "ls -la"},
            {"type": "websocket.receive", "bytes": b"\x00ping"},
        ],
    )
    await sandboxes.terminal_proxy(ws, str(sb.id), db)

    assert ws.accepted is True
    assert ws.close_code is None
    # The host received the payload+config merge and client keystrokes
    first = json.loads(host.sent[0])
    assert first["sandbox_id"] == str(sb.id)
    assert first["container_name"] == sb.container_name
    assert first["cols"] == 80
    assert host.sent[1] == "ls -la"
    assert host.sent[2] == b"\x00ping"
    # The client received the host messages (text + binary passthrough)
    assert ("text", "hello from host") in ws.sent
    assert ("bytes", b"\x01\x02") in ws.sent


@pytest.mark.asyncio
async def test_terminal_proxy_cancels_pending_client_relay_when_host_closes(monkeypatch):
    """When the host socket closes first, the still-blocked client→host relay
    task must be cancelled (no leaked task left polling the websocket)."""
    db = FakeDB()
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    # Host immediately ends its stream → host_to_client finishes at once.
    host = FakeHostSocket(incoming=[])
    monkeypatch.setattr(
        sandboxes.websockets, "connect", lambda url, max_size=None: host
    )

    class _BlockingWebSocket(FakeWebSocket):
        """A client that never sends another event after the config."""

        def __init__(self):
            super().__init__(initial_config="{}")
            self._gate = asyncio.Event()

        async def receive(self):
            await self._gate.wait()  # never set — must be cancelled
            return {"type": "websocket.receive", "text": "never"}

    ws = _BlockingWebSocket()
    before = {id(t) for t in asyncio.all_tasks()}
    await asyncio.wait_for(sandboxes.terminal_proxy(ws, str(sb.id), db), timeout=5)
    # Let the cancellation propagate through the relay task.
    await asyncio.sleep(0.05)

    assert ws.accepted is True
    assert ws.close_code is None
    # The merged payload+config reached the host, and nothing came back.
    assert len(host.sent) == 1
    assert json.loads(host.sent[0])["container_name"] == sb.container_name
    assert ws.sent == []
    # The blocked client relay task was cancelled — no live task leaked.
    stray = [
        t
        for t in asyncio.all_tasks()
        if id(t) not in before and t is not asyncio.current_task()
    ]
    assert stray == []


@pytest.mark.asyncio
async def test_terminal_proxy_host_connect_failure_is_swallowed(monkeypatch):
    db = FakeDB()
    sb = _make_sandbox()
    db.sandboxes[sb.id] = sb

    def fake_connect(url, max_size=None):
        raise OSError("host terminal unavailable")

    monkeypatch.setattr(sandboxes.websockets, "connect", fake_connect)
    ws = FakeWebSocket("{}")
    await sandboxes.terminal_proxy(ws, str(sb.id), db)
    # Accepted, then the exception is swallowed by the outer handler
    assert ws.accepted is True
    assert ws.sent == []
