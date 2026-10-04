"""
Tests for DebugLoggingMiddleware (app/core/middleware.py).

Scope:
- Pure ASGI middleware: request/response logging when is_debug() is True,
  silence when False, pass-through for non-HTTP scopes.
- Request-body logging for POST/PUT/PATCH with replay of the body so the
  downstream app can still read it (the core ASGI contract of this
  middleware).
- Response status capture, elapsed-time logging, /api/health suppression.
- Exception path: downstream exceptions are logged then re-raised.
- Body-read failure path ("<could not read body>").
- Unit tests for _truncate and _make_body_replay.

Mocking: `app.core.middleware.is_debug` is monkeypatched (it is imported
into the module's namespace at import time). The app under test is a tiny
FastAPI app driven in-process via httpx.ASGITransport — no network. The
500-path test uses starlette's TestClient with raise_server_exceptions=False
so the re-raised exception does not escape the client.
"""

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.types import Message

from app.core.middleware import (
    MAX_BODY_LOG,
    DebugLoggingMiddleware,
    _make_body_replay,
    _truncate,
)


def _build_app() -> FastAPI:
    """Tiny app exercising the middleware over real FastAPI routing."""
    app = FastAPI()
    app.add_middleware(DebugLoggingMiddleware)

    @app.get("/hello")
    async def hello():
        return {"ok": True}

    @app.post("/echo")
    async def echo(request: Request):
        # Reads the body through the middleware's replayed receive — proves
        # the downstream app can still consume the body after logging.
        raw = await request.body()
        return {"length": len(raw), "text": raw.decode("utf-8", errors="replace")}

    @app.get("/api/health")
    async def fake_health():
        return {"status": "healthy"}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


# ── is_debug() = False: complete silence, pass-through ────────────────


@pytest.mark.asyncio
async def test_no_logging_when_debug_disabled(monkeypatch, capsys):
    monkeypatch.setattr("app.core.middleware.is_debug", lambda: False)
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/hello")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert capsys.readouterr().out == ""


@pytest.mark.asyncio
async def test_disabled_middleware_passes_original_receive_through(monkeypatch):
    """When debug is off the downstream app must get the ORIGINAL receive."""
    monkeypatch.setattr("app.core.middleware.is_debug", lambda: False)

    seen = {}

    async def downstream(scope, receive, send):
        seen["scope"] = scope
        seen["receive"] = receive
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    scope = {"type": "http", "method": "GET", "path": "/x", "query_string": b""}
    middleware = DebugLoggingMiddleware(downstream)
    await middleware(scope, receive, send)

    assert seen["scope"] is scope
    assert seen["receive"] is receive


# ── is_debug() = True: request/response logging ───────────────────────


@pytest.mark.asyncio
async def test_logs_request_and_response_lines_when_debug_enabled(
    monkeypatch, capsys
):
    monkeypatch.setattr("app.core.middleware.is_debug", lambda: True)
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/hello", params={"q": "1"})
    assert resp.status_code == 200

    out = capsys.readouterr().out
    assert "REQUEST  GET /hello" in out
    assert "query=q=1" in out
    assert "RESPONSE GET /hello" in out
    assert "status=200" in out
    assert "ms)" in out  # elapsed wall-clock time is reported
    # GET requests log an empty body (nothing was read from the stream)
    assert "query=q=1  body=\n" in out


@pytest.mark.asyncio
async def test_query_none_placeholder_when_no_query_params(monkeypatch, capsys):
    monkeypatch.setattr("app.core.middleware.is_debug", lambda: True)
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/hello")
    assert resp.status_code == 200
    assert "query=(none)" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_api_health_requests_are_not_logged(monkeypatch, capsys):
    monkeypatch.setattr("app.core.middleware.is_debug", lambda: True)
    app = _build_app()
    async with _client(app) as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert capsys.readouterr().out == ""


# ── Body logging + replay ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_body_logged_and_replayed_to_downstream(monkeypatch, capsys):
    """The middleware logs the body AND the downstream app can still read it."""
    monkeypatch.setattr("app.core.middleware.is_debug", lambda: True)
    app = _build_app()
    async with _client(app) as client:
        resp = await client.post("/echo", content=b"hello middleware")

    assert resp.status_code == 200
    assert resp.json() == {"length": 16, "text": "hello middleware"}

    out = capsys.readouterr().out
    assert "REQUEST  POST /echo" in out
    assert "body=hello middleware" in out
    assert "RESPONSE POST /echo" in out
    assert "status=200" in out


@pytest.mark.asyncio
async def test_post_empty_body(monkeypatch, capsys):
    monkeypatch.setattr("app.core.middleware.is_debug", lambda: True)
    app = _build_app()
    async with _client(app) as client:
        resp = await client.post("/echo", content=b"")

    assert resp.status_code == 200
    assert resp.json() == {"length": 0, "text": ""}
    out = capsys.readouterr().out
    assert "REQUEST  POST /echo" in out
    assert "body=\n" in out or out.rstrip().endswith("body=")  # logged as empty


@pytest.mark.asyncio
async def test_post_body_truncated_to_max_body_log(monkeypatch, capsys):
    """Bodies are truncated to MAX_BODY_LOG (2000) characters in the log line."""
    monkeypatch.setattr("app.core.middleware.is_debug", lambda: True)
    app = _build_app()
    payload = "a" * (MAX_BODY_LOG + 1000)
    async with _client(app) as client:
        resp = await client.post("/echo", content=payload.encode())

    assert resp.status_code == 200
    assert resp.json()["length"] == MAX_BODY_LOG + 1000  # downstream gets it all

    out = capsys.readouterr().out
    request_line = next(ln for ln in out.splitlines() if "REQUEST" in ln)
    body_part = request_line.split("body=", 1)[1]
    assert body_part == "a" * MAX_BODY_LOG  # logged body is exactly 2000 chars


@pytest.mark.asyncio
async def test_body_read_failure_logs_placeholder(monkeypatch, capsys):
    """When request.body() raises, the log shows <could not read body>."""
    monkeypatch.setattr("app.core.middleware.is_debug", lambda: True)

    async def broken_receive():
        raise RuntimeError("receive exploded")

    sent: list[Message] = []

    async def downstream(scope, receive, send):
        # Downstream does NOT read the body here (the original receive raises).
        await send({"type": "http.response.start", "status": 201, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "path": "/broken",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
    }
    middleware = DebugLoggingMiddleware(downstream)
    await middleware(scope, broken_receive, send)

    out = capsys.readouterr().out
    assert "REQUEST  POST /broken" in out
    assert "body=<could not read body>" in out
    assert "RESPONSE POST /broken" in out
    assert "status=201" in out
    assert sent[0]["status"] == 201


# ── Exception path ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_downstream_exception_logged_and_reraised(monkeypatch, capsys):
    monkeypatch.setattr("app.core.middleware.is_debug", lambda: True)

    async def downstream(scope, receive, send):
        raise RuntimeError("boom")

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        pass

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "path": "/boom",
        "query_string": b"",
        "headers": [],
        "scheme": "http",
        "server": ("testserver", 80),
    }
    middleware = DebugLoggingMiddleware(downstream)
    with pytest.raises(RuntimeError, match="boom"):
        await middleware(scope, receive, send)

    out = capsys.readouterr().out
    assert "EXCEPTION  GET /boom" in out
    assert "error=boom" in out
    assert "ms)" in out
    # No RESPONSE line is printed for a failed request
    assert "RESPONSE" not in out


def test_exception_path_returns_500_via_testclient(monkeypatch, capsys):
    """End-to-end: a failing route logs the exception; the client sees a 500."""
    monkeypatch.setattr("app.core.middleware.is_debug", lambda: True)
    app = _build_app()
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/boom")

    assert resp.status_code == 500
    out = capsys.readouterr().out
    assert "EXCEPTION  GET /boom" in out
    assert "error=boom" in out


# ── Non-HTTP scopes ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_http_scope_passes_through_untouched(monkeypatch, capsys):
    """Lifespan/websocket scopes bypass all logging logic."""
    monkeypatch.setattr("app.core.middleware.is_debug", lambda: True)

    seen = {}

    async def downstream(scope, receive, send):
        seen["scope"] = scope
        seen["receive"] = receive

    async def receive():
        raise AssertionError("should not be called")

    async def send(message):
        raise AssertionError("should not be called")

    scope = {"type": "lifespan"}
    middleware = DebugLoggingMiddleware(downstream)
    await middleware(scope, receive, send)

    assert seen["scope"] is scope
    assert seen["receive"] is receive
    assert capsys.readouterr().out == ""


# ── _make_body_replay ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_body_replay_yields_body_once_then_delegates():
    original_calls = []

    async def original_receive():
        original_calls.append("called")
        return {"type": "http.disconnect"}

    replay = _make_body_replay(b"payload", original_receive)

    first = await replay()
    assert first == {"type": "http.request", "body": b"payload", "more_body": False}
    assert original_calls == []  # cached body served without touching original

    second = await replay()
    assert second == {"type": "http.disconnect"}
    assert original_calls == ["called"]  # delegated to the original receive

    third = await replay()
    assert third == {"type": "http.disconnect"}
    assert original_calls == ["called", "called"]


@pytest.mark.asyncio
async def test_body_replay_empty_body():
    async def original_receive():
        return {"type": "http.disconnect"}

    replay = _make_body_replay(b"", original_receive)
    assert await replay() == {"type": "http.request", "body": b"", "more_body": False}


# ── _truncate ──────────────────────────────────────────────────────────


def test_truncate_short_text_unchanged():
    assert _truncate("hello", 10) == "hello"
    assert _truncate("", 10) == ""


def test_truncate_exact_limit_unchanged():
    text = "x" * 50
    assert _truncate(text, 50) == text


def test_truncate_long_text_gets_marker():
    text = "y" * 120
    result = _truncate(text, 100)
    assert result == "y" * 100 + "... (120 chars total)"


def test_truncate_zero_limit():
    assert _truncate("abc", 0) == "... (3 chars total)"
