from types import SimpleNamespace

import httpx
import pytest

from app.config import settings
from app.voice.asr import create_asr_engine
from app.voice.remote import RemoteAsrEngine, RemoteTtsEngine
from app.voice.tts import create_tts_engine

_HTTPX_ASYNC_CLIENT = httpx.AsyncClient


def _client_factory(transport):
    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return _HTTPX_ASYNC_CLIENT(*args, **kwargs)

    return factory


@pytest.mark.asyncio
async def test_remote_asr_buffers_once_and_forces_profile_language(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["language"] = request.url.params["language"]
        return httpx.Response(200, json={"text": "new project ideas"})

    monkeypatch.setattr(
        "app.voice.remote.httpx.AsyncClient",
        _client_factory(httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(settings, "VOICE_RUNTIME_URL", "http://voice:8766/")
    engine = RemoteAsrEngine(SimpleNamespace(language="English"))
    await engine.start_stream()
    await engine.feed_audio(b"one")
    await engine.feed_audio(b"two")
    assert await engine.get_partial() == ""
    assert await engine.finish_stream() == "new project ideas"
    assert seen == {"body": b"onetwo", "language": "English"}


@pytest.mark.asyncio
async def test_remote_tts_streams_pcm(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/tts"
        return httpx.Response(200, content=b"pcm-audio")

    monkeypatch.setattr(
        "app.voice.remote.httpx.AsyncClient",
        _client_factory(httpx.MockTransport(handler)),
    )
    monkeypatch.setattr(settings, "VOICE_RUNTIME_URL", "http://voice:8766")
    engine = RemoteTtsEngine(SimpleNamespace())
    assert b"".join([chunk async for chunk in engine.synthesize("hello")]) == b"pcm-audio"


def test_factories_use_host_runtime_when_configured(monkeypatch):
    monkeypatch.setattr(settings, "VOICE_RUNTIME_URL", "http://voice:8766")
    asr = create_asr_engine(SimpleNamespace(provider="qwen3-asr"))
    tts = create_tts_engine(SimpleNamespace(provider="pocket-tts"))
    assert isinstance(asr, RemoteAsrEngine)
    assert isinstance(tts, RemoteTtsEngine)
