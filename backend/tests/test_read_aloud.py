import io
import wave
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.api import voice
from app.voice import tts


@pytest.fixture
def speech(monkeypatch):
    monkeypatch.setattr(voice.settings, "VOICE_ENABLED", True)
    monkeypatch.setattr(voice.voice_settings, "get", lambda: {"voice": "custom-test", "speed": 1.5})
    specs = []
    engine = SimpleNamespace(cancel=AsyncMock())

    async def synthesize(text):
        assert text == "Hello"
        yield b"\x00\x00" * 100

    engine.synthesize = synthesize

    def create(spec):
        specs.append(spec)
        return engine

    monkeypatch.setattr(tts, "create_tts_engine", create)
    app = FastAPI()
    app.include_router(voice.router, prefix="/api")
    return TestClient(app), engine, specs


def test_read_aloud_uses_selected_voice_and_returns_wav(speech):
    client, engine, specs = speech
    response = client.post("/api/voice/speech", json={"text": "Hello"})
    assert response.status_code == 200
    assert specs[0].voice == "custom-test"
    assert response.headers["x-playback-speed"] == "1.5"
    with wave.open(io.BytesIO(response.content)) as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getnframes() == 100
    engine.cancel.assert_awaited_once()


def test_failure_is_safe_and_releases_engine(speech):
    client, engine, _ = speech

    async def fail(text):
        raise RuntimeError("secret-token")
        yield b""

    engine.synthesize = fail
    response = client.post("/api/voice/speech", json={"text": "Hello"})
    assert response.status_code == 502
    assert "secret-token" not in response.text
    engine.cancel.assert_awaited_once()


def test_disabled_and_empty_text(speech, monkeypatch):
    client, _, _ = speech
    assert client.post("/api/voice/speech", json={"text": " "}).status_code == 400
    assert client.post("/api/voice/speech", json={"text": "x" * 20001}).status_code == 422
    monkeypatch.setattr(voice.settings, "VOICE_ENABLED", False)
    assert client.post("/api/voice/speech", json={"text": "Hello"}).status_code == 503
