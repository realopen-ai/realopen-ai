"""
API-level tests for app/api/deps.py — the optional system-dependencies router.

Endpoints covered (httpx + ASGITransport against the real router):
  - GET    /api/deps                  catalog passthrough
  - GET    /api/deps/{name}/status    installed/version shape, unknown → 404
  - POST   /api/deps/{name}/install   SSE happy path, unknown → 404,
                                      installer crash → error event
  - DELETE /api/deps/{name}/uninstall SSE happy path, unknown → 404

What is mocked — no apt-get/pip subprocess is ever spawned:
  - app.api.deps.get_catalog / get_dependency / is_installed / get_version
  - app.api.deps.install_dependency / uninstall_dependency (async generators)

get_dependency returns REAL Dependency dataclass instances so the endpoints
handle attribute access exactly as in production.
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import deps as deps_api  # noqa: E402
from app.services.deps_manager import Dependency  # noqa: E402

app = FastAPI()
app.include_router(deps_api.router, prefix="/api")


def make_dep(name="libreoffice", **kw):
    data = dict(
        name=name,
        display_name="LibreOffice",
        description="Office suite for document preview",
        category="Office",
        kind="system-direct",
        binary_name="soffice",
        pip_name=None,
        import_name=None,
        pip_extra_args=[],
        install_size="450 MB",
        enables=["document preview"],
    )
    data.update(kw)
    return Dependency(**data)


def sse_events(body: str):
    """JSON-decode the `data: ...` frames of an SSE body."""
    events = []
    for frame in body.strip().split("\n\n"):
        if frame.startswith("data: "):
            events.append(json.loads(frame[6:]))
    return events


@pytest.fixture
def env(monkeypatch):
    e = SimpleNamespace(
        catalog=[
            {"name": "libreoffice", "installed": True},
            {"name": "voice-runtime", "installed": False},
        ],
        deps={"libreoffice": make_dep()},
        installed=True,
        version="7.4.7",
        install_events=[
            {"stage": "updating", "output": "Hit:1 http://deb.debian.org"},
            {"stage": "downloading", "output": "Get:1 libobasis"},
            {"stage": "done", "exit_code": 0, "version": "7.4.7"},
        ],
        install_error=None,
        uninstall_events=[
            {"stage": "uninstalling", "output": "Removing libreoffice"},
            {"stage": "done", "exit_code": 0, "output": "removed"},
        ],
        uninstall_error=None,
        install_calls=[],
        uninstall_calls=[],
    )

    monkeypatch.setattr(deps_api, "get_catalog", lambda: e.catalog)
    monkeypatch.setattr(
        deps_api, "get_dependency", lambda name: e.deps.get(name)
    )
    monkeypatch.setattr(deps_api, "is_installed", lambda dep: e.installed)
    monkeypatch.setattr(deps_api, "get_version", lambda dep: e.version)

    async def fake_install(dep):
        e.install_calls.append(dep.name)
        for i, ev in enumerate(e.install_events):
            yield ev
            if i == 0 and e.install_error is not None:
                # crash mid-stream after the first progress event
                raise e.install_error

    async def fake_uninstall(dep):
        e.uninstall_calls.append(dep.name)
        for ev in e.uninstall_events:
            yield ev
            if e.uninstall_error is not None:
                raise e.uninstall_error

    monkeypatch.setattr(deps_api, "install_dependency", fake_install)
    monkeypatch.setattr(deps_api, "uninstall_dependency", fake_uninstall)
    return e


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url="http://test")


# ══════════════════════════════════════════════════════════════════════
# GET /deps
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_dependencies_returns_catalog(client, env):
    r = await client.get("/api/deps")
    assert r.status_code == 200
    assert r.json() == {"dependencies": env.catalog}


@pytest.mark.asyncio
async def test_list_dependencies_empty_catalog(client, env):
    env.catalog = []
    r = await client.get("/api/deps")
    assert r.status_code == 200
    assert r.json() == {"dependencies": []}


# ══════════════════════════════════════════════════════════════════════
# GET /deps/{name}/status
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_status_installed_with_version(client, env):
    r = await client.get("/api/deps/libreoffice/status")
    assert r.status_code == 200
    assert r.json() == {
        "name": "libreoffice",
        "installed": True,
        "version": "7.4.7",
    }


@pytest.mark.asyncio
async def test_status_not_installed_has_no_version(client, env):
    env.installed = False
    r = await client.get("/api/deps/libreoffice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["installed"] is False
    assert body["version"] is None


@pytest.mark.asyncio
async def test_status_unknown_dependency_404(client, env):
    r = await client.get("/api/deps/doesnotexist/status")
    assert r.status_code == 404
    assert "Unknown dependency" in r.json()["detail"]
    assert "doesnotexist" in r.json()["detail"]


# ══════════════════════════════════════════════════════════════════════
# POST /deps/{name}/install
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_streams_progress_events(client, env):
    r = await client.post("/api/deps/libreoffice/install")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    assert r.headers["cache-control"] == "no-cache"
    events = sse_events(r.text)
    assert events == env.install_events
    assert env.install_calls == ["libreoffice"]


@pytest.mark.asyncio
async def test_install_unknown_dependency_404(client, env):
    r = await client.post("/api/deps/ghostscript/install")
    assert r.status_code == 404
    assert "Unknown dependency" in r.json()["detail"]
    assert env.install_calls == []


@pytest.mark.asyncio
async def test_install_crash_yields_error_event(client, env):
    env.install_events = [{"stage": "updating", "output": "starting"}]
    env.install_error = RuntimeError("apt lock held")
    r = await client.post("/api/deps/libreoffice/install")
    assert r.status_code == 200  # SSE stream still 200; error is an event
    events = sse_events(r.text)
    assert events[0] == {"stage": "updating", "output": "starting"}
    assert events[1]["stage"] == "error"
    assert "apt lock held" in events[1]["error"]


# ══════════════════════════════════════════════════════════════════════
# DELETE /deps/{name}/uninstall
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_uninstall_streams_progress_events(client, env):
    r = await client.delete("/api/deps/libreoffice/uninstall")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r.text)
    assert events == env.uninstall_events
    assert env.uninstall_calls == ["libreoffice"]


@pytest.mark.asyncio
async def test_uninstall_unknown_dependency_404(client, env):
    r = await client.delete("/api/deps/ghostscript/uninstall")
    assert r.status_code == 404
    assert env.uninstall_calls == []


@pytest.mark.asyncio
async def test_uninstall_crash_yields_error_event(client, env):
    env.uninstall_error = RuntimeError("dpkg interrupted")
    r = await client.delete("/api/deps/libreoffice/uninstall")
    assert r.status_code == 200
    events = sse_events(r.text)
    assert events[0] == {"stage": "uninstalling", "output": "Removing libreoffice"}
    assert events[1]["stage"] == "error"
    assert "dpkg interrupted" in events[1]["error"]
