"""Streaming speech recognition (ASR) providers.

Voice input is transcribed by a provider selected EXCLUSIVELY from
profiles.yml (``settings.get_voice_config()`` → ``VoiceModelSpec``-shaped
object with ``provider`` / ``model`` / ``revision`` / ``runtime`` fields).
Nothing here hardcodes a model id.

Providers (``spec.provider``):
    "qwen3-asr" → :class:`Qwen3AsrEngine` (transformers, cross-platform)
                   or :class:`MlxQwen3AsrEngine` when ``spec.runtime == "mlx"``
                   (macOS-only accelerator behind the same interface).

Engine contract (:class:`AsrProvider`) — all audio is s16le mono 16 kHz:

    await engine.start_stream()   # begin a new utterance stream
    await engine.feed_audio(pcm)  # append PCM bytes (buffered internally)
    text = await engine.get_partial()   # throttled interim transcript
    text = await engine.finish_stream() # FINAL transcript (flushes buffer)
    await engine.cancel()         # drop buffered audio (barge-in)
    engine.status()               # readiness dict for the `ready` frame

Heavy imports (torch / transformers / mlx_qwen3_asr) are ALWAYS lazy —
inside methods — so the voice package imports on machines without the ML
runtime; a missing runtime raises :class:`AsrError` with
code="asr_runtime_missing", fatal=False (the message names the module).
Loaded models are cached at module level (keyed by model+revision) so
concurrent sessions share one instance.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AsrError",
    "AsrProvider",
    "create_asr_engine",
    "Qwen3AsrEngine",
    "MlxQwen3AsrEngine",
]


class AsrError(Exception):
    """ASR failure with a protocol error code + fatality flag.

    ``fatal=True`` errors tear the session down (unsupported provider,
    corrupt install); ``fatal=False`` (default) means recoverable — the
    session reports the error and returns to LISTENING.
    """

    def __init__(self, message: str, code: str = "asr_error", fatal: bool = False):
        super().__init__(message)
        self.message = message
        self.code = code
        self.fatal = fatal

    def __str__(self) -> str:  # pragma: no cover — trivial
        return self.message


class AsrProvider:
    """Streaming ASR interface (see module docstring for the contract)."""

    name = "base"

    async def start_stream(self) -> None:
        """Begin a new utterance stream (resets internal audio buffer)."""
        raise NotImplementedError

    async def feed_audio(self, pcm: bytes) -> None:
        """Feed s16le mono 16 kHz PCM bytes for the current stream."""
        raise NotImplementedError

    async def get_partial(self) -> str:
        """Return the best interim transcript ("" when nothing new)."""
        raise NotImplementedError

    async def finish_stream(self) -> str:
        """Flush remaining audio and return the FINAL transcript."""
        raise NotImplementedError

    async def cancel(self) -> None:
        """Drop buffered audio and reset (barge-in / stop)."""
        raise NotImplementedError

    def status(self) -> dict:
        """Readiness summary for the `ready` frame + logs."""
        raise NotImplementedError


def create_asr_engine(spec: Any) -> AsrProvider:
    """Factory: pick the ASR engine for a resolved voice-model spec.

    ``spec`` is the ``asr`` side of ``settings.get_voice_config()`` (or the
    installer's ``VoiceModelSpec``) — duck-typed: only ``provider``,
    ``model``, ``revision`` and optionally ``runtime`` are read.
    """
    provider = str(getattr(spec, "provider", "") or "").strip().lower()
    if provider == "qwen3-asr":
        # Platform-specific accelerator hint (task §12): the MLX build is
        # only selected when profiles.yml explicitly requests it.
        runtime = str(getattr(spec, "runtime", None) or "").strip().lower()
        if runtime == "mlx":
            from app.voice.asr.mlx_engine import MlxQwen3AsrEngine

            return MlxQwen3AsrEngine(spec)
        from app.voice.asr.qwen3 import Qwen3AsrEngine

        return Qwen3AsrEngine(spec)
    raise AsrError(
        f"unsupported ASR provider {provider!r} (supported: qwen3-asr)",
        code="asr_unsupported_provider",
        fatal=True,
    )
