"""Streaming speech synthesis (TTS) providers + the sentence chunker.

Voice output synthesizes the EXISTING assistant's streamed text — the
voice session feeds ``run_agent_stream`` ``message`` tokens into
:class:`SentenceChunker` and each emitted sentence is synthesized as it
arrives.

Providers (``spec.provider``, resolved from profiles.yml only):
    "pocket-tts" → :class:`PocketTtsEngine`

Engine contract (:class:`TtsProvider`) — output PCM is s16le mono at
``settings.VOICE_TTS_SAMPLE_RATE`` (24 kHz, voice protocol v1):

    await engine.warm_up()                # load the model once (shared)
    async for pcm in engine.synthesize(text): ...  # 24 kHz s16le chunks
    await engine.cancel()                 # stop in-flight synthesis
    engine.status()                       # readiness dict

Heavy imports (``pocket_tts``) are lazy — a missing runtime raises
:class:`TtsError` with code ``tts_runtime_missing``, fatal=False. The
loaded model instance is cached at module level so TTS is not reloaded
per request.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from app.voice.tts.chunker import SentenceChunker, clean_for_tts

__all__ = [
    "TtsError",
    "TtsProvider",
    "create_tts_engine",
    "PocketTtsEngine",
    "SentenceChunker",
    "clean_for_tts",
]


class TtsError(Exception):
    """TTS failure with a protocol error code + fatality flag.

    ``fatal=True`` tears the session down (unsupported provider); runtime /
    model problems are ``fatal=False`` — the session reports the error and
    finishes the turn text-only.
    """

    def __init__(self, message: str, code: str = "tts_error", fatal: bool = False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.fatal = fatal

    def __str__(self) -> str:  # pragma: no cover — trivial
        return self.message


class TtsProvider:
    """Streaming TTS interface (see module docstring for the contract)."""

    name = "base"

    async def warm_up(self) -> Any:
        """Load the model (no-op when already loaded). Shared instance."""
        raise NotImplementedError

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield PCM s16le mono chunks at settings.VOICE_TTS_SAMPLE_RATE."""
        raise NotImplementedError
        yield b""  # pragma: no cover — makes this an async generator

    async def cancel(self) -> None:
        """Signal the in-flight synthesis (if any) to stop."""
        raise NotImplementedError

    def status(self) -> dict:
        """Readiness summary for the `ready` frame + logs."""
        raise NotImplementedError


def create_tts_engine(spec: Any) -> TtsProvider:
    """Factory: pick the TTS engine for a resolved voice-model spec.

    ``spec`` is the ``tts`` side of ``settings.get_voice_config()`` —
    duck-typed: ``provider`` / ``model`` / ``language`` / ``voice``.
    """
    provider = str(getattr(spec, "provider", "") or "").strip().lower()
    from app.config import settings

    if settings.VOICE_RUNTIME_URL:
        from app.voice.remote import RemoteTtsEngine

        return RemoteTtsEngine(spec)
    if provider == "pocket-tts":
        from app.voice.tts.pocket import PocketTtsEngine

        return PocketTtsEngine(spec)
    raise TtsError(
        f"unsupported TTS provider {provider!r} (supported: pocket-tts)",
        code="tts_unsupported_provider",
        fatal=True,
    )
