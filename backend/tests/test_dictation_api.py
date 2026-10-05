import httpx
import pytest
from fastapi import FastAPI

from app.api import voice
from app.config import settings
from app.voice import asr


@pytest.mark.asyncio
async def test_dictation_uses_configured_asr(monkeypatch):
    received = []

    class Engine:
        async def start_stream(self):
            received.append("start")

        async def feed_audio(self, pcm):
            received.append(pcm)

        async def finish_stream(self):
            return "A dictated message"

        async def cancel(self):
            received.append("cancel")

    def create(spec):
        assert spec == settings.get_voice_config().asr
        return Engine()

    monkeypatch.setattr(settings, "VOICE_ENABLED", True)
    monkeypatch.setattr(asr, "create_asr_engine", create)
    app = FastAPI()
    app.include_router(voice.router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/voice/transcribe", content=b"\x00\x01")
    assert response.status_code == 200
    assert response.json() == {"text": "A dictated message"}
    assert received == ["start", b"\x00\x01", "cancel"]


@pytest.mark.asyncio
@pytest.mark.parametrize("payload,status", [(b"", 400), (b"x", 400), (b"\0" * (1920000 + 2), 413)])
async def test_dictation_rejects_invalid_audio(monkeypatch, payload, status):
    monkeypatch.setattr(settings, "VOICE_ENABLED", True)
    monkeypatch.setattr(
        asr, "create_asr_engine", lambda spec: pytest.fail("Invalid audio must not load ASR")
    )
    app = FastAPI()
    app.include_router(voice.router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/voice/transcribe", content=payload)
    assert response.status_code == status


@pytest.mark.asyncio
async def test_dictation_disabled(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_ENABLED", False)
    app = FastAPI()
    app.include_router(voice.router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/voice/transcribe", content=b"\0\0")
    assert response.status_code == 503


@pytest.mark.asyncio
async def test_dictation_asr_error_cleans_up(monkeypatch):
    cleaned = []

    class Engine:
        async def start_stream(self):
            raise asr.AsrError("ASR runtime unavailable")

        async def cancel(self):
            cleaned.append(True)

    monkeypatch.setattr(settings, "VOICE_ENABLED", True)
    monkeypatch.setattr(asr, "create_asr_engine", lambda spec: Engine())
    app = FastAPI()
    app.include_router(voice.router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post("/api/voice/transcribe", content=b"\0\0")
    assert response.status_code == 502
    assert cleaned == [True]
