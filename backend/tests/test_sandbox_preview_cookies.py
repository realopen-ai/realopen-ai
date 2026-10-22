"""Run the real direct-preview proxy without initializing native voice engines."""

import ast
import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient


def test_direct_preview_preserves_multiple_session_cookies_and_request_headers():
    source = Path(__file__).resolve().parents[2] / "scripts/host-runtime-server.py"
    function = next(
        node
        for node in ast.parse(source.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "_direct_preview_app"
    )
    received = []

    def upstream(request):
        received.append(request)
        return httpx.Response(
            200,
            json={"ok": True},
            headers=[
                ("Set-Cookie", "session=opaque; HttpOnly; Path=/api; SameSite=Strict"),
                ("Set-Cookie", "other=value; Path=/api"),
            ],
        )

    transport = httpx.MockTransport(upstream)
    namespace = {
        "FastAPI": FastAPI,
        "HTTPException": HTTPException,
        "Request": Request,
        "StreamingResponse": StreamingResponse,
        "asyncio": asyncio,
        "httpx": SimpleNamespace(
            AsyncClient=lambda **kwargs: httpx.AsyncClient(
                transport=transport, **kwargs
            )
        ),
        "direct_preview_specs": {6767: object()},
        "_sandboxes": lambda: SimpleNamespace(
            preview_target=lambda spec, port: "http://sandbox.test"
        ),
    }
    exec(
        compile(ast.Module(body=[function], type_ignores=[]), str(source), "exec"),
        namespace,
    )
    with TestClient(namespace["_direct_preview_app"](6767)) as client:
        result = client.post(
            "/api/auth/login",
            headers={"X-App-Request": "1"},
            json={"username": "tester"},
        )
        assert len(result.headers.get_list("set-cookie")) == 2
        assert "httponly" in result.headers.get_list("set-cookie")[0].lower()
        client.get("/api/contacts", headers={"X-CSRF-Token": "token"})
    assert received[0].url.host == "sandbox.test"
    assert received[0].headers["x-app-request"] == "1"
    assert received[1].headers["x-csrf-token"] == "token"
    assert received[1].headers["cookie"] == "session=opaque; other=value"
