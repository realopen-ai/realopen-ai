"""Tests for the providers + model-preferences layer (Settings ▸ AI tab).

Covers:
1. pretty_model_name prettifier
2. provider lookup (is_groq_model / provider_of)
3. Groq key validation / persistence / masking (mocked httpx)
4. Model preference resolution + validation (task slots)
5. The /api/providers and /api/models endpoints (FastAPI TestClient on an
   isolated app with just the new routers)
6. The provider-routed chat adapters: chat_once + stream_chat normalize
   BOTH wire formats (Ollama JSON-lines and Groq SSE) into one shape.
"""

import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.services import model_prefs
from app.services import providers

# ─── Shared fixtures ────────────────────────────────────────────────


@pytest.fixture
def tmp_state(tmp_path, monkeypatch):
    """Redirect the persistent state dir to a temp dir."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(providers, "_state_dir", lambda: state)
    # Reset the tags cache + lock so tests never see each other's data
    monkeypatch.setattr(
        providers, "_tags_cache", {"ts": 0.0, "models": [], "reachable": False}
    )
    monkeypatch.setattr(providers, "_tags_lock", None)
    return state


TAGS_RESPONSE = {
    "models": [
        {
            "name": "qwen3:4b",
            "size": 2_500_000_000,
            "modified_at": "2025-01-01T00:00:00Z",
            "details": {"family": "qwen3", "parameter_size": "4B"},
        },
        {
            "name": "qwen3.5:4b-mlx",
            "size": 2_500_000_000,
            "modified_at": "2025-01-01T00:00:00Z",
            "details": {"family": "qwen3", "parameter_size": "4B"},
        },
    ]
}


def _install_transport(monkeypatch, handler):
    """Route every httpx call inside providers.py through a MockTransport."""
    transport = httpx.MockTransport(handler)

    def _client_factory(*args, **kwargs):
        kwargs.pop("transport", None)
        return httpx.AsyncClient(*args, transport=transport, **kwargs)

    fake_httpx = SimpleNamespace(AsyncClient=_client_factory)
    monkeypatch.setattr(providers, "httpx", fake_httpx)
    return transport


@pytest.fixture
def ollama_up(monkeypatch):
    """Ollama reachable with two installed models; Groq key invalid (401)."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/tags"):
            return httpx.Response(200, json=TAGS_RESPONSE)
        if url.endswith("/api/version"):
            return httpx.Response(200, json={"version": "0.6"})
        if url.startswith("https://api.groq.com/openai/v1/models"):
            return httpx.Response(401, json={"error": {"message": "Invalid key"}})
        return httpx.Response(404, json={})

    _install_transport(monkeypatch, handler)


@pytest.fixture
def groq_ok(monkeypatch):
    """Groq accepts the key AND Ollama is up with installed models."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/api/tags"):
            return httpx.Response(200, json=TAGS_RESPONSE)
        if url.startswith("https://api.groq.com/openai/v1/models"):
            return httpx.Response(200, json={"data": [{"id": "openai/gpt-oss-120b"}]})
        return httpx.Response(404, json={})

    _install_transport(monkeypatch, handler)


# ─── 1. Prettifier ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,pretty",
    [
        ("qwen3.5:4b-mlx", "Qwen 3.5 4B MLX"),
        ("x/flux2-klein:4b", "X / Flux 2 Klein 4B"),
        ("openai/gpt-oss-120b", "GPT OSS 120B"),
        ("qwen3:32b", "Qwen 3 32B"),
        ("moondream:1.8b", "Moondream 1.8B"),
        ("llava:13b", "LLaVA 13B"),
        ("sdxl:q4_k_m", "SDXL Q4_K_M"),
        ("qwen/qwen3.6-27b", "Qwen 3.6 27B"),
    ],
)
def test_pretty_model_name(raw, pretty):
    assert providers.pretty_model_name(raw) == pretty


# ─── 2. Provider lookup ─────────────────────────────────────────────


def test_is_groq_model():
    assert providers.is_groq_model("openai/gpt-oss-120b")
    assert providers.is_groq_model("qwen/qwen3.6-27b")
    assert not providers.is_groq_model("qwen3:4b")
    assert providers.provider_of("qwen/qwen3.6-27b") == "groq"
    assert providers.provider_of("qwen3:4b") == "ollama"


# ─── 3. Groq key validation + persistence ───────────────────────────


@pytest.mark.asyncio
async def test_groq_key_validation_ok(groq_ok, tmp_state):
    ok, detail = await providers.test_groq_key("gsk_validkey123456789")
    assert ok, detail


@pytest.mark.asyncio
async def test_groq_key_validation_rejected(ollama_up, tmp_state):
    ok, detail = await providers.test_groq_key("gsk_wrongkey000000000")
    assert not ok
    assert "401" in detail or "Invalid" in detail


@pytest.mark.asyncio
async def test_groq_key_validation_empty():
    ok, _ = await providers.test_groq_key("")
    assert not ok


def test_groq_key_persistence(tmp_state):
    assert providers.get_groq_key() is None
    providers.set_groq_key("gsk_persisted00000000")
    assert providers.get_groq_key() == "gsk_persisted00000000"
    assert providers._mask_key("gsk_persisted00000000").startswith("gsk_")
    assert "•" in providers._mask_key("gsk_persisted00000000")
    assert providers.clear_groq_key() is True
    assert providers.get_groq_key() is None
    assert providers.clear_groq_key() is False  # already gone


@pytest.mark.asyncio
async def test_groq_status_masked(ollama_up, tmp_state):
    providers.set_groq_key("gsk_rejected12345678")
    status = await providers.groq_status()
    # Key present but Groq rejects it → not connected, key still masked
    assert status["has_key"] is True
    assert status["connected"] is False
    assert "gsk_" in status["key_masked"]


@pytest.mark.asyncio
async def test_groq_status_connected(groq_ok, tmp_state):
    providers.set_groq_key("gsk_accepted123456789")
    status = await providers.groq_status()
    assert status["connected"] is True
    assert status["model_count"] == 4
    assert status["kind"] == "cloud"


@pytest.mark.asyncio
async def test_ollama_status(ollama_up, tmp_state):
    status = await providers.ollama_status()
    assert status["connected"] is True
    assert status["model_count"] == 2
    assert status["kind"] == "local"


# ─── 4. Model preferences ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_task_resolution_default(ollama_up, tmp_state):
    """No prefs → profile defaults (cpu_small: chat qwen3:4b)."""
    assert await model_prefs.resolve_task_model("chat") == "qwen3:4b"
    assert await model_prefs.resolve_task_model("vision") == "moondream:1.8b"


@pytest.mark.asyncio
async def test_set_task_model_local(ollama_up, tmp_state):
    row = await model_prefs.set_task_model("chat", "qwen3.5:4b-mlx")
    assert row["model"] == "qwen3.5:4b-mlx"
    assert row["is_default"] is False
    assert row["provider"] == "ollama"
    assert await model_prefs.resolve_task_model("chat") == "qwen3.5:4b-mlx"
    # Persistence round-trip
    assert model_prefs.get_prefs()["chat"] == "qwen3.5:4b-mlx"


@pytest.mark.asyncio
async def test_set_task_model_clear(ollama_up, tmp_state):
    await model_prefs.set_task_model("chat", "qwen3.5:4b-mlx")
    row = await model_prefs.set_task_model("chat", None)
    assert row["model"] == "qwen3:4b"  # back to profile default
    assert row["is_default"] is True
    assert "chat" not in model_prefs.get_prefs()


@pytest.mark.asyncio
async def test_set_task_model_groq_requires_key(ollama_up, tmp_state):
    with pytest.raises(ValueError, match="[Cc]onnect the Groq"):
        await model_prefs.set_task_model("chat", "openai/gpt-oss-120b")


@pytest.mark.asyncio
async def test_set_task_model_groq_with_key(groq_ok, tmp_state):
    providers.set_groq_key("gsk_accepted123456789")
    row = await model_prefs.set_task_model("chat", "openai/gpt-oss-120b")
    assert row["model"] == "openai/gpt-oss-120b"
    assert row["provider"] == "groq"
    assert await model_prefs.resolve_task_model("chat") == "openai/gpt-oss-120b"


@pytest.mark.asyncio
async def test_vision_slot_is_local_only(groq_ok, tmp_state):
    providers.set_groq_key("gsk_accepted123456789")
    with pytest.raises(ValueError, match="locally"):
        await model_prefs.set_task_model("vision", "openai/gpt-oss-20b")
    with pytest.raises(ValueError, match="locally"):
        await model_prefs.set_task_model("image", "qwen/qwen3.6-27b")


@pytest.mark.asyncio
async def test_set_task_model_unknown_model(ollama_up, tmp_state):
    # Ollama reachable and the model is neither installed nor a profile
    # model → rejected with a helpful message
    with pytest.raises(ValueError, match="not installed"):
        await model_prefs.set_task_model("chat", "llama3:999b")


@pytest.mark.asyncio
async def test_set_task_model_unknown_task(ollama_up, tmp_state):
    with pytest.raises(ValueError):
        await model_prefs.set_task_model("nonexistent", "qwen3:4b")


@pytest.mark.asyncio
async def test_stale_pref_ignored(ollama_up, tmp_state):
    """A pref pointing at a model that was uninstalled falls back silently."""
    model_prefs._save_prefs({"chat": "llama3:999b"})
    assert await model_prefs.resolve_task_model("chat") == "qwen3:4b"


@pytest.mark.asyncio
async def test_profile_default_not_pulled_still_valid(ollama_up, tmp_state):
    """A profile default that isn't pulled yet is a legal selection."""
    # moondream:1.8b is the cpu_small vision default but not in TAGS_RESPONSE
    row = await model_prefs.set_task_model("chat", "moondream:1.8b")
    assert row["model"] == "moondream:1.8b"


@pytest.mark.asyncio
async def test_resolve_chat_request_model(ollama_up, tmp_state):
    # Role alias → chat slot (pref wins)
    await model_prefs.set_task_model("chat", "qwen3.5:4b-mlx")
    assert await model_prefs.resolve_chat_request_model("default") == "qwen3.5:4b-mlx"
    assert await model_prefs.resolve_chat_request_model("chat") == "qwen3.5:4b-mlx"
    # Explicit model id passes through untouched
    assert await model_prefs.resolve_chat_request_model("qwen3:4b") == "qwen3:4b"


@pytest.mark.asyncio
async def test_task_overview(ollama_up, tmp_state):
    overview = await model_prefs.task_overview()
    tasks = {row["task"]: row for row in overview}
    assert set(tasks) == {
        "chat",
        "vision",
        "document_reasoning",
        "report",
        "excel",
        "image",
    }
    assert tasks["chat"]["label"] == "Chat / Agent"
    assert tasks["report"]["label"] == "Report generation"
    assert tasks["vision"]["local_only"] is True
    assert tasks["chat"]["local_only"] is False


# ─── 5. API endpoints ───────────────────────────────────────────────


def _make_client():
    app = FastAPI()
    from app.api.providers import router as providers_router
    from app.api.models import router as models_router

    app.include_router(providers_router, prefix="/api")
    app.include_router(models_router, prefix="/api")
    return TestClient(app)


def test_get_providers(ollama_up, tmp_state):
    client = _make_client()
    res = client.get("/api/providers")
    assert res.status_code == 200
    providers_list = res.json()["providers"]
    by_id = {p["id"]: p for p in providers_list}
    assert by_id["ollama"]["connected"] is True
    assert by_id["ollama"]["model_count"] == 2
    assert by_id["groq"]["connected"] is False


def test_connect_groq_invalid_key(ollama_up, tmp_state):
    client = _make_client()
    res = client.post("/api/providers/groq", json={"api_key": "gsk_wrongkey00000"})
    assert res.status_code == 400
    assert providers.get_groq_key() is None  # nothing saved on failure


def test_connect_groq_valid_key(groq_ok, tmp_state):
    client = _make_client()
    res = client.post("/api/providers/groq", json={"api_key": "gsk_accepted123456789"})
    assert res.status_code == 200
    body = res.json()
    assert body["connected"] is True
    assert body["model_count"] == 4
    assert "gsk_" in body["key_masked"]
    # Key persisted only after successful validation
    assert providers.get_groq_key() == "gsk_accepted123456789"


def test_connect_groq_rejects_garbage(groq_ok, tmp_state):
    client = _make_client()
    res = client.post("/api/providers/groq", json={"api_key": "short"})
    assert res.status_code == 400
    assert providers.get_groq_key() is None


def test_disconnect_groq(groq_ok, tmp_state):
    providers.set_groq_key("gsk_accepted123456789")
    client = _make_client()
    res = client.delete("/api/providers/groq")
    assert res.status_code == 200
    assert res.json()["connected"] is False
    assert providers.get_groq_key() is None


def test_models_available(groq_ok, tmp_state):
    providers.set_groq_key("gsk_accepted123456789")
    client = _make_client()
    res = client.get("/api/models/available")
    assert res.status_code == 200
    body = res.json()
    ids = {m["id"]: m for m in body["models"]}
    assert "qwen3:4b" in ids  # installed
    assert ids["qwen3:4b"]["installed"] is True
    assert ids["qwen3:4b"]["provider"] == "ollama"
    assert "openai/gpt-oss-120b" in ids  # groq cloud
    assert ids["openai/gpt-oss-120b"]["provider"] == "groq"
    assert "moondream:1.8b" in ids  # profile default, not pulled
    assert ids["moondream:1.8b"]["installed"] is False
    assert body["groq_connected"] is True
    tasks = {t["task"]: t for t in body["tasks"]}
    assert tasks["chat"]["model"] == "qwen3:4b"
    assert tasks["chat"]["is_default"] is True


def test_put_model_preference(ollama_up, tmp_state):
    client = _make_client()
    res = client.put(
        "/api/models/preferences", json={"task": "chat", "model": "qwen3.5:4b-mlx"}
    )
    assert res.status_code == 200
    assert res.json()["model"] == "qwen3.5:4b-mlx"


def test_put_model_preference_reset(ollama_up, tmp_state):
    client = _make_client()
    assert (
        client.put(
            "/api/models/preferences", json={"task": "chat", "model": "qwen3.5:4b-mlx"}
        ).status_code
        == 200
    )
    res = client.put("/api/models/preferences", json={"task": "chat", "model": None})
    assert res.status_code == 200
    assert res.json()["model"] == "qwen3:4b"
    assert res.json()["is_default"] is True


def test_put_model_preference_errors(ollama_up, tmp_state):
    client = _make_client()
    res = client.put(
        "/api/models/preferences", json={"task": "nope", "model": "qwen3:4b"}
    )
    assert res.status_code == 400
    res = client.put(
        "/api/models/preferences",
        json={"task": "chat", "model": "openai/gpt-oss-120b"},
    )
    assert res.status_code == 400  # groq not connected


# ─── 6. Provider-routed chat adapters ───────────────────────────────


OLLAMA_STREAM_LINES = [
    json.dumps({"message": {"thinking": "let me"}, "done": False}),
    json.dumps({"message": {"thinking": " think"}, "done": False}),
    json.dumps({"message": {"content": "Hello"}, "done": False}),
    json.dumps({"message": {"content": " world"}, "done": False}),
    json.dumps(
        {
            "message": {
                "tool_calls": [
                    {"function": {"name": "use_websearch", "arguments": '{"q": "x"}'}}
                ]
            },
            "done": False,
        }
    ),
    json.dumps({"message": {}, "done": True}),
]

GROQ_STREAM_SSE = [
    'data: {"choices":[{"delta":{"reasoning":"hmm"}}]}',
    'data: {"choices":[{"delta":{"content":"Hi"}}]}',
    'data: {"choices":[{"delta":{"content":" there"}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1",'
    '"function":{"name":"use_websearch","arguments":"{\\"q\\""}}]}}]}',
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,'
    '"function":{"arguments":":\\"cats\\"}"}}]}}]}',
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls"}]}',
    "data: [DONE]",
]


def _streaming_response(lines):
    body = "\n".join(lines) + "\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, content=body.encode(), headers={"Content-Type": "application/json"}
        )

    return handler


@pytest.mark.asyncio
async def test_stream_chat_ollama(monkeypatch):
    _install_transport(monkeypatch, _streaming_response(OLLAMA_STREAM_LINES))
    chunks = [
        c
        async for c in providers.stream_chat(
            "qwen3:4b", [{"role": "user", "content": "hi"}]
        )
    ]
    thinking = "".join(c["thinking"] for c in chunks)
    content = "".join(c["content"] for c in chunks)
    assert thinking == "let me think"
    assert content == "Hello world"
    tool_calls = [tc for c in chunks for tc in c["tool_calls"]]
    assert len(tool_calls) == 1
    assert tool_calls[0]["function"]["name"] == "use_websearch"
    assert chunks[-1]["done"] is True


@pytest.mark.asyncio
async def test_stream_chat_groq(monkeypatch, tmp_state):
    providers.set_groq_key("gsk_accepted123456789")
    _install_transport(monkeypatch, _streaming_response(GROQ_STREAM_SSE))
    chunks = [
        c
        async for c in providers.stream_chat(
            "openai/gpt-oss-120b", [{"role": "user", "content": "hi"}]
        )
    ]
    thinking = "".join(c["thinking"] for c in chunks)
    content = "".join(c["content"] for c in chunks)
    assert thinking == "hmm"
    assert content == "Hi there"
    # Tool-call fragments were accumulated into ONE complete call
    tool_calls = [tc for c in chunks for tc in c["tool_calls"]]
    assert len(tool_calls) == 1
    assert tool_calls[0]["id"] == "call_1"
    assert tool_calls[0]["function"]["name"] == "use_websearch"
    assert tool_calls[0]["function"]["arguments"] == '{"q":"cats"}'
    assert chunks[-1]["done"] is True


@pytest.mark.asyncio
async def test_chat_once_groq(monkeypatch, tmp_state):
    providers.set_groq_key("gsk_accepted123456789")

    def handler(request: httpx.Request) -> httpx.Response:
        # Verify the groq payload mapping
        payload = json.loads(request.content)
        assert payload["model"] == "openai/gpt-oss-120b"
        assert payload["stream"] is False
        assert payload["max_tokens"] == 256
        assert payload["temperature"] == 0.2
        assert payload["response_format"] == {"type": "json_object"}
        assert request.headers["Authorization"].startswith("Bearer gsk_")
        return httpx.Response(
            200,
            json={
                "model": "openai/gpt-oss-120b",
                "choices": [
                    {"message": {"role": "assistant", "content": '{"ok": true}'}}
                ],
            },
        )

    _install_transport(monkeypatch, handler)
    data = await providers.chat_once(
        "openai/gpt-oss-120b",
        [{"role": "user", "content": "hi"}],
        think=False,
        format="json",
        options={"num_predict": 256, "temperature": 0.2},
    )
    # Ollama-shaped response regardless of provider
    assert data["message"]["content"] == '{"ok": true}'
    assert data["message"]["role"] == "assistant"
    assert data["done"] is True


@pytest.mark.asyncio
async def test_chat_once_groq_image_messages(monkeypatch, tmp_state):
    """Ollama-style image messages convert to OpenAI content parts."""
    providers.set_groq_key("gsk_accepted123456789")
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"role": "assistant", "content": "a cat"}}]},
        )

    _install_transport(monkeypatch, handler)
    await providers.chat_once(
        "openai/gpt-oss-120b",
        [{"role": "user", "content": "what is this?", "images": ["QUJD"]}],
    )
    msg = captured["payload"]["messages"][0]
    assert isinstance(msg["content"], list)
    assert msg["content"][0] == {"type": "text", "text": "what is this?"}
    assert msg["content"][1]["type"] == "image_url"
    assert msg["content"][1]["image_url"]["url"] == "data:image/png;base64,QUJD"


@pytest.mark.asyncio
async def test_chat_once_ollama_passthrough(monkeypatch):
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["payload"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "message": {"role": "assistant", "content": "local reply"},
                "done": True,
            },
        )

    _install_transport(monkeypatch, handler)
    data = await providers.chat_once(
        "qwen3:4b",
        [{"role": "user", "content": "hi"}],
        think=False,
        format="json",
        options={"num_predict": 128},
    )
    assert captured["url"].endswith("/api/chat")
    assert captured["payload"]["think"] is False
    assert captured["payload"]["format"] == "json"
    assert captured["payload"]["options"] == {"num_predict": 128}
    assert data["message"]["content"] == "local reply"


@pytest.mark.asyncio
async def test_stream_chat_groq_without_key(monkeypatch, tmp_state):
    """Selecting a groq model without a saved key fails loudly."""
    _install_transport(monkeypatch, lambda req: httpx.Response(200, json={}))
    with pytest.raises(RuntimeError, match="[Aa]PI key"):
        async for _ in providers.stream_chat(
            "openai/gpt-oss-120b", [{"role": "user", "content": "hi"}]
        ):
            pass  # pragma: no cover
