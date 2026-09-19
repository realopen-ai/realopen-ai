"""Qwen3-ASR engine via ``mlx_qwen3_asr`` (macOS / Apple-Silicon MLX).

Selected ONLY when profiles.yml sets ``voice.asr.runtime: mlx`` — the
platform-specific accelerator stays isolated behind the shared
:class:`AsrProvider` interface. Every other platform keeps
using the transformers engine in ``app/voice/asr/qwen3.py``.

The ``mlx_qwen3_asr`` package is imported lazily; when it is missing the
engine raises :class:`AsrError` with code ``asr_runtime_missing`` (fatal
False) exactly like the transformers runtime — the session then reports a
recoverable error and the user can re-run setup.

API probing: the ``mlx_qwen3_asr`` surface used here is
``mlx_qwen3_asr.Session`` with a ``transcribe``-style method. Names are
probed with getattr / call-signature fallbacks (never assumed) — see the
comments inline. Audio buffering + the chunked re-transcription partial
strategy mirror :class:`Qwen3AsrEngine`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Tuple

import numpy as np

from app.voice.audio import pcm_to_float32
from app.voice.asr import AsrError, AsrProvider
from app.voice.asr.qwen3 import (
    ASR_SAMPLE_RATE,
    BYTES_PER_SAMPLE,
    DEFAULT_MIN_PARTIAL_MS,
    DEFAULT_PARTIAL_INTERVAL_S,
    _local_snapshot_dir,
)

logger = logging.getLogger(__name__)

# One Session per (model, revision) — shared across concurrent sessions.
_SESSION_CACHE: Dict[Tuple[str, str], Any] = {}
_SESSION_CACHE_LOCK = asyncio.Lock()

# Method names probed on a Session for transcription (in order).
_TRANSCRIBE_METHODS = ("transcribe", "generate", "infer", "__call__")


class MlxQwen3AsrEngine(AsrProvider):
    """Qwen3-ASR on Apple Silicon (MLX) behind the common interface."""

    name = "qwen3-asr"
    runtime = "mlx"

    def __init__(
        self,
        spec: Any,
        min_partial_ms: int = DEFAULT_MIN_PARTIAL_MS,
        partial_interval_s: float = DEFAULT_PARTIAL_INTERVAL_S,
    ):
        self._spec = spec
        self._model_id = str(getattr(spec, "model", "") or "")
        self._revision = str(getattr(spec, "revision", None) or "main")
        self._min_partial_ms = min_partial_ms
        self._partial_interval_s = partial_interval_s

        self._session: Optional[Any] = None
        self._buffer = bytearray()
        self._last_partial_text = ""
        self._last_partial_at = 0.0
        self._stream_open = False
        self._lock = asyncio.Lock()

    # ── AsrProvider interface ────────────────────────────────────────

    async def start_stream(self) -> None:
        async with self._lock:
            self._buffer.clear()
            self._last_partial_text = ""
            self._last_partial_at = 0.0
            self._stream_open = True
            await self._ensure_session()

    async def feed_audio(self, pcm: bytes) -> None:
        if not self._stream_open:
            return
        self._buffer += pcm

    async def get_partial(self) -> str:
        if not self._stream_open:
            return ""
        buffered_ms = len(self._buffer) * 1000.0 / (ASR_SAMPLE_RATE * BYTES_PER_SAMPLE)
        if buffered_ms < self._min_partial_ms:
            return self._last_partial_text
        if time.monotonic() - self._last_partial_at < self._partial_interval_s:
            return self._last_partial_text
        if self._lock.locked():
            return self._last_partial_text
        async with self._lock:
            self._last_partial_at = time.monotonic()
            text = await asyncio.to_thread(self._transcribe, bytes(self._buffer))
        if text:
            self._last_partial_text = text
        return self._last_partial_text

    async def finish_stream(self) -> str:
        async with self._lock:
            pcm = bytes(self._buffer)
            self._buffer.clear()
            self._stream_open = False
            self._last_partial_text = ""
            if not pcm:
                return ""
            text = await asyncio.to_thread(self._transcribe, pcm)
        return text.strip()

    async def cancel(self) -> None:
        async with self._lock:
            self._buffer.clear()
            self._stream_open = False
            self._last_partial_text = ""

    def status(self) -> dict:
        return {
            "provider": self.name,
            "model": self._model_id,
            "revision": self._revision,
            "loaded": self._session is not None,
            "runtime": self.runtime,
            "partials": True,
        }

    # ── Session loading (lazy, shared) ───────────────────────────────

    async def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        key = (self._model_id, self._revision)
        async with _SESSION_CACHE_LOCK:
            cached = _SESSION_CACHE.get(key)
            if cached is None:
                cached = await asyncio.to_thread(self._load_session_sync)
                _SESSION_CACHE[key] = cached
                logger.info(
                    "mlx_qwen3_asr session loaded (model=%s@%s)",
                    self._model_id,
                    self._revision,
                )
            self._session = cached
            return cached

    def _load_session_sync(self) -> Any:
        try:
            import mlx_qwen3_asr  # type: ignore
        except ImportError as e:
            raise AsrError(
                "ASR runtime missing: module 'mlx_qwen3_asr' is not installed "
                "(macOS/MLX voice runtime; run the setup wizard)",
                code="asr_runtime_missing",
                fatal=False,
            ) from e

        Session = getattr(mlx_qwen3_asr, "Session", None)
        if Session is None:
            raise AsrError(
                "mlx_qwen3_asr does not expose a Session class — API not " "recognized",
                code="asr_model_error",
                fatal=False,
            )

        local_dir = _local_snapshot_dir(self._model_id)
        source: Any = local_dir or self._model_id
        # Constructor signatures differ across releases — try the common
        # shapes, cheapest first.
        for args, kwargs in (
            ((source,), {}),
            ((source, self._revision), {}),
            ((), {"model": source}),
            ((), {"model": source, "revision": self._revision}),
        ):
            try:
                return Session(*args, **kwargs)
            except TypeError:
                continue
        raise AsrError(
            f"could not construct mlx_qwen3_asr.Session for {source!r} "
            "(unrecognized constructor signature)",
            code="asr_model_error",
            fatal=False,
        )

    # ── Transcription core ───────────────────────────────────────────

    def _transcribe(self, pcm: bytes) -> str:
        if self._session is None:
            raise AsrError(
                "transcription attempted before session load",
                code="asr_not_loaded",
                fatal=False,
            )
        samples = np.asarray(pcm_to_float32(pcm))
        if len(samples) == 0:
            return ""
        method = None
        for name in _TRANSCRIBE_METHODS:
            cand = getattr(self._session, name, None)
            if callable(cand):
                method = cand
                break
        if method is None:
            raise AsrError(
                "mlx_qwen3_asr Session has no recognized transcribe method",
                code="asr_model_error",
                fatal=False,
            )
        try:
            # Signature probing: (audio, sample_rate) → (audio) → kwargs.
            for args, kwargs in (
                ((samples,), {"sample_rate": ASR_SAMPLE_RATE}),
                ((samples,), {}),
                ((), {"audio": samples, "sample_rate": ASR_SAMPLE_RATE}),
                ((bytes(pcm),), {}),
            ):
                try:
                    result = method(*args, **kwargs)
                    return self._extract_text(result)
                except TypeError:
                    continue
            raise AsrError(
                "mlx_qwen3_asr transcribe call signature not recognized",
                code="asr_model_error",
                fatal=False,
            )
        except AsrError:
            raise
        except Exception as e:
            raise AsrError(
                f"mlx_qwen3_asr transcription failed: {e}",
                code="asr_model_error",
                fatal=False,
            ) from e

    @staticmethod
    def _extract_text(result: Any) -> str:
        """Normalize the many shapes a transcribe() result may take."""
        if result is None:
            return ""
        if isinstance(result, str):
            return result
        if isinstance(result, (list, tuple)):
            return MlxQwen3AsrEngine._extract_text(result[0]) if result else ""
        if isinstance(result, dict):
            for key in ("text", "transcript", "output"):
                if key in result:
                    return MlxQwen3AsrEngine._extract_text(result[key])
            return ""
        if isinstance(result, np.ndarray):
            return str(result)
        return str(result)
