"""Adapters for the loopback host-native voice inference service."""

from __future__ import annotations

from typing import Any, AsyncIterator

import httpx

from app.config import settings
from app.voice.asr import AsrError, AsrProvider
from app.voice.tts import TtsError, TtsProvider


def _base_url() -> str:
    return settings.VOICE_RUNTIME_URL.rstrip("/")


class RemoteAsrEngine(AsrProvider):
    name = "host-native"
    runtime = "remote"

    def __init__(self, spec: Any):
        self._spec = spec
        self._buffer = bytearray()

    async def warm_up(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(f"{_base_url()}/v1/warmup/asr")
                response.raise_for_status()
        except Exception as exc:
            raise AsrError(
                f"host voice runtime unavailable: {exc}",
                code="asr_runtime_unavailable",
            ) from exc

    async def start_stream(self) -> None:
        self._buffer.clear()

    async def feed_audio(self, pcm: bytes) -> None:
        self._buffer.extend(pcm)

    async def get_partial(self) -> str:
        # Qwen's CPU/MLX APIs do not provide a cheap stable partial. Re-running
        # the whole growing buffer is slower than waiting for the final turn.
        return ""

    async def finish_stream(self) -> str:
        pcm = bytes(self._buffer)
        self._buffer.clear()
        if not pcm:
            return ""
        language = str(getattr(self._spec, "language", None) or "")
        try:
            async with httpx.AsyncClient(timeout=45.0) as client:
                response = await client.post(
                    f"{_base_url()}/v1/asr",
                    params={"language": language},
                    content=pcm,
                    headers={"Content-Type": "application/octet-stream"},
                )
                response.raise_for_status()
                return str(response.json().get("text") or "").strip()
        except Exception as exc:
            raise AsrError(str(exc), code="asr_host_error") from exc

    async def cancel(self) -> None:
        self._buffer.clear()

    def status(self) -> dict:
        return {"provider": self.name, "runtime": self.runtime, "url": _base_url()}


class RemoteTtsEngine(TtsProvider):
    name = "host-native"

    def __init__(self, spec: Any):
        self._cancelled = False

    async def warm_up(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(f"{_base_url()}/v1/warmup/tts")
                response.raise_for_status()
        except Exception as exc:
            raise TtsError(
                f"host voice runtime unavailable: {exc}",
                code="tts_runtime_unavailable",
            ) from exc

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        self._cancelled = False
        from app.services import voice_settings

        selected = voice_settings.get()
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                async with client.stream(
                    "POST",
                    f"{_base_url()}/v1/tts",
                    json={"text": text, "voice": selected["voice"]},
                ) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes(8192):
                        if self._cancelled:
                            return
                        if chunk:
                            yield chunk
        except Exception as exc:
            raise TtsError(str(exc), code="tts_host_error") from exc

    async def cancel(self) -> None:
        self._cancelled = True

    def status(self) -> dict:
        return {"provider": self.name, "runtime": "remote", "url": _base_url()}
