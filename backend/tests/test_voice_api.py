"""Tests for the voice REST API (app/api/voice.py).

Scope (the lines the existing test_voice_*.py files leave uncovered):
  - GET  /api/voice/settings  — settings read (personas + model row)
  - PUT  /api/voice/settings  — update, incl. the host-runtime "prepare"
    call, runtime error propagation (409/502) and ValueError → 400
  - GET  /api/voice/voices    — builtin list + custom voices from the
    host runtime (unreachable runtime → empty custom list)
  - POST /api/voice/voices    — custom voice sample upload (.wav guard,
    20 MB limit, runtime error paths, happy path)
  - GET  /api/voice/status    — host-native branch (healthy / broken) and
    the local models_store branch with the ready/missing computation
  - _side_missing / _runtime_missing pure helpers

Mocks:
  - httpx.AsyncClient inside app/api/voice.py — the host voice runtime is
    never contacted for real.
  - app.services.voice_settings (get/update) and model_prefs.task_row.
  - app.voice.models_store.voice_dependency_status for the status gate.
  - No real audio processing, no model loading, no network.
"""

import sys
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import voice as voice_api  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import model_prefs  # noqa: E402
from app.services import voice_model_installer as vmi  # noqa: E402
from app.services import voice_settings  # noqa: E402
from app.voice import models_store as voice_models_store  # noqa: E402

app = FastAPI()
app.include_router(voice_api.router, prefix="/api")


# ── fixtures ──────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def client(monkeypatch):
    # Keep the host runtime disabled by default; individual tests enable it.
    monkeypatch.setattr(settings, "VOICE_RUNTIME_URL", "")
    monkeypatch.setattr(settings, "VOICE_ENABLED", True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text

    def json(self):
        return self._json

    @property
    def is_error(self):
        return self.status_code >= 400

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "server error", request=None, response=None
            )


class FakeHttpClient:
    """Stand-in for httpx.AsyncClient bound to the voice runtime."""

    get_response: FakeResponse = FakeResponse()
    get_exc: Exception | None = None
    post_response: FakeResponse = FakeResponse()
    post_exc: Exception | None = None
    calls: list = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        type(self).calls.append(("GET", url))
        if type(self).get_exc is not None:
            raise type(self).get_exc
        return type(self).get_response

    async def post(self, url, json=None, content=None, headers=None):
        type(self).calls.append(
            ("POST", url, json, content if content is None else len(content), headers)
        )
        if type(self).post_exc is not None:
            raise type(self).post_exc
        return type(self).post_response


@pytest.fixture
def fake_runtime(monkeypatch):
    """Enable the host runtime URL and swap in the fake HTTP client."""
    monkeypatch.setattr(settings, "VOICE_RUNTIME_URL", "http://voice-runtime:8766/")
    monkeypatch.setattr(voice_api.httpx, "AsyncClient", FakeHttpClient)
    FakeHttpClient.get_response = FakeResponse()
    FakeHttpClient.get_exc = None
    FakeHttpClient.post_response = FakeResponse()
    FakeHttpClient.post_exc = None
    FakeHttpClient.calls = []
    return FakeHttpClient


# ── GET /voice/settings ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_settings_returns_personas_and_model(client, monkeypatch):
    monkeypatch.setattr(
        voice_settings, "get", lambda: {"voice": "mary", "speed": 1.5, "persona": "concise"}
    )

    async def fake_task_row(task):
        assert task == "voice"
        return {"task": "voice", "model": "qwen3:4b", "is_default": True}

    monkeypatch.setattr(model_prefs, "task_row", fake_task_row)
    r = await client.get("/api/voice/settings")
    assert r.status_code == 200
    body = r.json()
    assert body["voice"] == "mary"
    assert body["speed"] == 1.5
    assert body["personas"] == voice_settings.PERSONAS
    assert body["model"]["task"] == "voice"


# ── PUT /voice/settings ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_settings_without_voice_field(client, monkeypatch):
    captured = {}

    def fake_update(payload):
        captured.update(payload)
        return {"voice": "mary", "speed": 2.0, "persona": "friendly"}

    monkeypatch.setattr(voice_settings, "update", fake_update)
    r = await client.put("/api/voice/settings", json={"speed": 2.0, "persona": "friendly"})
    assert r.status_code == 200
    assert r.json()["speed"] == 2.0
    assert captured == {"speed": 2.0, "persona": "friendly"}


@pytest.mark.asyncio
async def test_put_settings_prepares_voice_on_runtime(client, fake_runtime, monkeypatch):
    updated = {}

    def fake_update(payload):
        updated.update(payload)
        return payload

    monkeypatch.setattr(voice_settings, "update", fake_update)
    FakeHttpClient.post_response = FakeResponse(200, {"prepared": True})
    r = await client.put("/api/voice/settings", json={"voice": "alba"})
    assert r.status_code == 200
    # The prepare call hit the runtime with the voice name
    method, url, body, content, headers = FakeHttpClient.calls[-1]
    assert url == "http://voice-runtime:8766/v1/voices/prepare"  # no double slash
    assert body == {"voice": "alba"}
    assert updated == {"voice": "alba"}


@pytest.mark.asyncio
async def test_put_settings_runtime_error_propagates_status(client, fake_runtime):
    FakeHttpClient.post_response = FakeResponse(409, {"detail": "voice not found"})
    r = await client.put("/api/voice/settings", json={"voice": "nope"})
    assert r.status_code == 409
    assert r.json()["detail"] == "voice not found"


@pytest.mark.asyncio
async def test_put_settings_runtime_down_502(client, fake_runtime):
    FakeHttpClient.post_exc = httpx.ConnectError("runtime unreachable")
    r = await client.put("/api/voice/settings", json={"voice": "alba"})
    assert r.status_code == 502
    assert "Could not prepare voice" in r.json()["detail"]


@pytest.mark.asyncio
async def test_put_settings_invalid_value_400(client, monkeypatch):
    def failing_update(payload):
        raise ValueError("Speed must be 0.5, 1, 1.5, or 2")

    monkeypatch.setattr(voice_settings, "update", failing_update)
    r = await client.put("/api/voice/settings", json={"speed": 3})
    assert r.status_code == 400
    assert "Speed" in r.json()["detail"]


# ── GET /voice/voices ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_voices_without_runtime(client):
    r = await client.get("/api/voice/voices")
    assert r.status_code == 200
    body = r.json()
    assert "alba" in body["builtin"]
    assert "mary" in body["builtin"]
    assert len(body["builtin"]) == 21
    assert body["custom"] == []


@pytest.mark.asyncio
async def test_get_voices_merges_custom_from_runtime(client, fake_runtime):
    FakeHttpClient.get_response = FakeResponse(
        200, {"custom": [{"id": "custom:coach", "name": "Coach"}]}
    )
    r = await client.get("/api/voice/voices")
    body = r.json()
    assert body["custom"] == [{"id": "custom:coach", "name": "Coach"}]
    # Fetch used the /v1/voices endpoint without double slash
    method, url = FakeHttpClient.calls[-1]
    assert url == "http://voice-runtime:8766/v1/voices"


@pytest.mark.asyncio
async def test_get_voices_runtime_unreachable_still_lists_builtins(client, fake_runtime):
    FakeHttpClient.get_exc = httpx.ConnectError("down")
    r = await client.get("/api/voice/voices")
    assert r.status_code == 200
    assert r.json()["custom"] == []


# ── POST /voice/voices ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_voice_rejects_non_wav(client):
    r = await client.post(
        "/api/voice/voices",
        files={"file": ("sample.mp3", b"id3data", "audio/mpeg")},
    )
    assert r.status_code == 400
    assert ".wav" in r.json()["detail"]


@pytest.mark.asyncio
async def test_upload_voice_rejects_oversize(client, fake_runtime):
    big = b"\x00" * (20 * 1024 * 1024 + 1)
    r = await client.post(
        "/api/voice/voices", files={"file": ("sample.wav", big, "audio/wav")}
    )
    assert r.status_code == 413
    assert "20 MB" in r.json()["detail"]
    # The runtime was never called
    assert FakeHttpClient.calls == []


@pytest.mark.asyncio
async def test_upload_voice_happy_path(client, fake_runtime):
    FakeHttpClient.post_response = FakeResponse(
        201, {"id": "custom:mine", "name": "Mine"}
    )
    r = await client.post(
        "/api/voice/voices",
        files={"file": ("mine.wav", b"RIFFfake", "audio/wav")},
    )
    assert r.status_code == 200
    assert r.json() == {"id": "custom:mine", "name": "Mine"}
    method, url, body, content, headers = FakeHttpClient.calls[-1]
    assert url == "http://voice-runtime:8766/v1/voices"
    assert content == 8  # raw bytes length
    assert headers["Content-Type"] == "audio/wav"
    assert headers["X-Voice-Name"] == "mine.wav"


@pytest.mark.asyncio
async def test_upload_voice_runtime_error(client, fake_runtime):
    FakeHttpClient.post_response = FakeResponse(422, {"detail": "too short"})
    r = await client.post(
        "/api/voice/voices", files={"file": ("x.wav", b"RIFF", "audio/wav")}
    )
    assert r.status_code == 422
    assert r.json()["detail"] == "too short"


@pytest.mark.asyncio
async def test_upload_voice_runtime_down_502(client, fake_runtime):
    FakeHttpClient.post_exc = httpx.ConnectError("offline")
    r = await client.post(
        "/api/voice/voices", files={"file": ("x.wav", b"RIFF", "audio/wav")}
    )
    assert r.status_code == 502
    assert "Could not import voice" in r.json()["detail"]


# ── _side_missing / _runtime_missing helpers ──────────────────────────


def test_side_missing_none_when_valid():
    assert voice_api._side_missing("ASR", {"valid": True}) is None


def test_side_missing_includes_model_and_reason():
    entry = voice_api._side_missing(
        "ASR", {"valid": False, "model": "qwen3-asr", "reason": "not_installed"}
    )
    assert entry == "ASR: qwen3-asr (not_installed)"


def test_side_missing_without_model_uses_reason_only():
    entry = voice_api._side_missing("TTS", {"valid": False, "reason": "offline"})
    assert entry == "TTS: offline"


def test_side_missing_defaults_reason():
    entry = voice_api._side_missing("TTS", {"valid": False})
    assert entry == "TTS: not_ready"


def test_runtime_missing_lists_entries():
    monkey = pytest.MonkeyPatch()
    try:
        monkey.setattr(
            vmi,
            "voice_runtime_entries",
            lambda profile=None: [
                {"id": "torch-cpu", "description": "PyTorch CPU"},
                {"id": "nagisa"},
            ],
        )
        entries = voice_api._runtime_missing()
        assert entries == ["Runtime: PyTorch CPU", "Runtime: nagisa"]
    finally:
        monkey.undo()


def test_runtime_missing_swallows_errors():
    monkey = pytest.MonkeyPatch()
    try:
        def _raise(profile=None):
            raise RuntimeError("installer unavailable")

        monkey.setattr(vmi, "voice_runtime_entries", _raise)
        assert voice_api._runtime_missing() == []
    finally:
        monkey.undo()


# ── GET /voice/status ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_host_native_healthy(client, fake_runtime):
    FakeHttpClient.get_response = FakeResponse(
        200,
        {
            "ready": True,
            "asr": {"valid": True, "model": "qwen3-asr"},
            "tts": {"valid": True, "model": "pocket-tts"},
        },
    )
    r = await client.get("/api/voice/status")
    assert r.status_code == 200
    body = r.json()
    assert body["ready"] is True
    assert body["enabled"] is True
    assert body["runtime"] == "host-native"
    assert body["asr"]["model"] == "qwen3-asr"
    assert body["missing"] == []


@pytest.mark.asyncio
async def test_status_host_native_tts_missing_fills_valid(client, fake_runtime):
    FakeHttpClient.get_response = FakeResponse(
        200, {"ready": False, "asr": {"valid": True}, "tts": None}
    )
    r = await client.get("/api/voice/status")
    body = r.json()
    assert body["tts"] == {"valid": True}  # None → default shape
    assert body["ready"] is False  # native said not ready


@pytest.mark.asyncio
async def test_status_host_native_disabled_feature(client, fake_runtime, monkeypatch):
    monkeypatch.setattr(settings, "VOICE_ENABLED", False)
    FakeHttpClient.get_response = FakeResponse(200, {"ready": True, "asr": {}, "tts": {}})
    r = await client.get("/api/voice/status")
    assert r.json()["ready"] is False
    assert r.json()["enabled"] is False


@pytest.mark.asyncio
async def test_status_host_native_unreachable(client, fake_runtime):
    FakeHttpClient.get_exc = httpx.ConnectError("no runtime")
    r = await client.get("/api/voice/status")
    body = r.json()
    assert body["ready"] is False
    assert body["runtime"] == "host-native"
    assert body["asr"] == {"valid": False, "reason": "host_runtime_unavailable"}
    assert body["tts"] == {"valid": False, "reason": "host_runtime_unavailable"}
    assert any("Host voice runtime" in m for m in body["missing"])


@pytest.mark.asyncio
async def test_status_local_all_ready(client, monkeypatch):
    monkeypatch.setattr(
        voice_models_store,
        "voice_dependency_status",
        lambda: {
            "enabled": True,
            "asr": {"valid": True, "model": "qwen3-asr"},
            "tts": {"valid": True, "model": "pocket-tts"},
            "runtime": [],
            "ready": True,
        },
    )
    r = await client.get("/api/voice/status")
    body = r.json()
    assert body["ready"] is True
    assert body["missing"] == []
    assert "runtime" not in body  # local branch has no runtime key


@pytest.mark.asyncio
async def test_status_local_missing_models_and_runtime(client, monkeypatch):
    monkeypatch.setattr(
        voice_models_store,
        "voice_dependency_status",
        lambda: {
            "enabled": True,
            "asr": {"valid": False, "model": "qwen3-asr", "reason": "not_installed"},
            "tts": {"valid": False, "reason": "offline"},
            "runtime": [
                {"id": "torch-cpu", "description": "PyTorch CPU"},
                {"id": "nagisa"},
            ],
            "ready": False,
        },
    )
    r = await client.get("/api/voice/status")
    body = r.json()
    assert body["ready"] is False
    assert "ASR: qwen3-asr (not_installed)" in body["missing"]
    assert "TTS: offline" in body["missing"]
    assert "Runtime: PyTorch CPU" in body["missing"]
    assert "Runtime: nagisa" in body["missing"]


@pytest.mark.asyncio
async def test_status_local_ready_blocked_by_runtime_gap(client, monkeypatch):
    # Models fine but a runtime pip package missing → ready must stay False
    monkeypatch.setattr(
        voice_models_store,
        "voice_dependency_status",
        lambda: {
            "enabled": True,
            "asr": {"valid": True},
            "tts": {"valid": True},
            "runtime": [{"id": "soynlp", "description": "Korean tokenizer"}],
            "ready": True,
        },
    )
    r = await client.get("/api/voice/status")
    body = r.json()
    assert body["ready"] is False
    assert body["missing"] == ["Runtime: Korean tokenizer"]


@pytest.mark.asyncio
async def test_status_local_disabled_flag_blocks_ready(client, monkeypatch):
    monkeypatch.setattr(
        voice_models_store,
        "voice_dependency_status",
        lambda: {
            "enabled": False,
            "asr": {"valid": True},
            "tts": {"valid": True},
            "runtime": [],
            "ready": True,
        },
    )
    r = await client.get("/api/voice/status")
    body = r.json()
    assert body["ready"] is False
    assert body["enabled"] is False


# ── misc ──────────────────────────────────────────────────────────────


def test_voice_settings_request_model_fields():
    req = voice_api.VoiceSettingsRequest()
    assert req.voice is None
    assert req.speed is None
    assert req.persona is None
    assert req.custom_personas is None
    payload = voice_api.VoiceSettingsRequest(
        voice="alba", speed=1.5, persona="concise", custom_personas=[{"id": "custom-x"}]
    )
    assert payload.model_dump(exclude_none=True) == {
        "voice": "alba",
        "speed": 1.5,
        "persona": "concise",
        "custom_personas": [{"id": "custom-x"}],
    }
