"""Qwen3-ASR engine via ``mlx_qwen3_asr`` (macOS / Apple-Silicon MLX).

Selected when profiles.yml sets ``voice.asr.runtime: mlx`` OR (the default
``auto``) when ``mlx_qwen3_asr`` is importable — Apple-Silicon hosts get the
native MLX engine, everything else keeps the transformers engine in
``app/voice/asr/qwen3.py``. The accelerator stays isolated behind the shared
:class:`AsrProvider` interface.

The REAL package API (verified against the released ``mlx-qwen3-asr``
package — this is the exact surface the user's local pipeline exercises)::

    from mlx_qwen3_asr import Session

    session = Session(model="Qwen/Qwen3-ASR-0.6B")        # HF-hub natural load
    state = session.init_streaming(chunk_size_sec=2.0, max_context_sec=30.0)
    state = session.feed_audio(np.float32_audio, state)   # → state, state.text
    state = session.finish_streaming(state)               # → final state.text

The engine implements EXACTLY that — windowed streaming with text-prefix
rollback (the official Qwen3-ASR recipe) — no buffered re-transcription.

Model persistence: ``Session(model=<repo id>)`` resolves weights through the
HuggingFace hub cache, which ``app.voice.hf_cache`` pins to the persisted
data volume (``data/huggingface/hub``). The setup wizard warms it once; the
runtime load below runs inside :func:`offline_hub` so a warm cache loads
instantly and a cold cache fails FAST with a clear "re-run setup" error
instead of downloading 1.9 GB mid-conversation (the original hang bug).

API probing: constructor and streaming call shapes are probed with getattr /
signature fallbacks so minor release drift degrades gracefully (falls back
to the buffered ``transcribe`` path of older versions).
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
)
from app.voice.hf_cache import ensure_hf_env, offline_hub

logger = logging.getLogger(__name__)

# One Session per (model, revision) — shared across concurrent sessions.
_SESSION_CACHE: Dict[Tuple[str, str], Any] = {}
_SESSION_CACHE_LOCK = asyncio.Lock()
# Negative cache: (model, revision) → monotonic timestamp of failure.
_LOAD_FAILURES: Dict[Tuple[str, str], float] = {}
_LOAD_FAILURE_TTL_S = 120.0

# Method names probed on a Session for transcription (streaming-API-less
# releases fall back to these).
_TRANSCRIBE_METHODS = ("transcribe", "generate", "infer", "__call__")

# Streaming window (mirrors the package's documented recipe + the user's
# working local pipeline: 2 s windows, 30 s rolling context).
_STREAM_CHUNK_SEC = 2.0
_STREAM_MAX_CONTEXT_SEC = 30.0


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
        self._language = str(getattr(spec, "language", None) or "").strip() or None
        self._min_partial_ms = min_partial_ms
        self._partial_interval_s = partial_interval_s

        self._session: Optional[Any] = None
        # Streaming-API state (init_streaming/feed_audio/finish_streaming).
        self._stream_state: Optional[Any] = None
        self._streaming_api = False
        # Fallback buffered mode (no streaming API on the Session).
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
            self._stream_state = None
            self._stream_open = True
            await self._ensure_session()
            if self._streaming_api:
                await asyncio.to_thread(self._init_streaming)

    async def feed_audio(self, pcm: bytes) -> None:
        if not self._stream_open:
            return
        if self._streaming_api:
            samples = np.asarray(pcm_to_float32(pcm), dtype=np.float32)
            if len(samples) == 0:
                return
            async with self._lock:
                # Real streaming: the package updates state.text as audio
                # arrives (windowed re-decode with prefix rollback).
                await asyncio.to_thread(self._feed_streaming, samples)
        else:
            self._buffer += pcm

    async def get_partial(self) -> str:
        if not self._stream_open:
            return ""
        if self._streaming_api:
            # state.text IS the interim transcript — no extra inference.
            return self._last_partial_text
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
            if self._streaming_api:
                state = self._stream_state
                self._stream_state = None
                self._stream_open = False
                self._last_partial_text = ""
                if state is None:
                    return ""
                text = await asyncio.to_thread(self._finish_streaming, state)
                return str(text or "").strip()
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
            self._stream_state = None
            self._stream_open = False
            self._last_partial_text = ""

    async def warm_up(self) -> None:
        """Preload the shared MLX session (voice session start)."""
        async with self._lock:
            await self._ensure_session()

    def status(self) -> dict:
        return {
            "provider": self.name,
            "model": self._model_id,
            "revision": self._revision,
            "loaded": self._session is not None,
            "runtime": self.runtime,
            "partials": True,
        }

    # ── Session loading (lazy, shared, offline-guarded) ──────────────

    async def _ensure_session(self) -> Any:
        if self._session is not None:
            return self._session
        key = (self._model_id, self._revision)
        failed_at = _LOAD_FAILURES.get(key)
        if failed_at is not None and time.monotonic() - failed_at < _LOAD_FAILURE_TTL_S:
            raise AsrError(
                "ASR model failed to load recently — retry suppressed for "
                f"{_LOAD_FAILURE_TTL_S:.0f}s (check the backend log; the usual "
                "fix is re-running the setup wizard)",
                code="asr_load_failed_recently",
                fatal=False,
            )
        async with _SESSION_CACHE_LOCK:
            cached = _SESSION_CACHE.get(key)
            if cached is None:
                try:
                    cached = await asyncio.to_thread(self._load_session_sync)
                    _SESSION_CACHE[key] = cached
                    _LOAD_FAILURES.pop(key, None)
                    logger.info(
                        "mlx_qwen3_asr session loaded (model=%s@%s, hf cache=%s)",
                        self._model_id,
                        self._revision,
                        ensure_hf_env(),
                    )
                except Exception:
                    # Negative cache — repeated partial polls cannot
                    # re-trigger load attempts for the TTL window.
                    _LOAD_FAILURES[key] = time.monotonic()
                    raise
            self._session = cached
            self._streaming_api = hasattr(cached, "init_streaming")
            return cached

    def _load_session_sync(self) -> Any:
        try:
            import mlx_qwen3_asr  # type: ignore  # noqa: PLC0415
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

        # Pin the HF cache at the persisted data volume BEFORE the package
        # resolves any weights, then load OFFLINE: warm cache → instant local
        # load; cold cache → clear error (never a mid-conversation download).
        ensure_hf_env()
        source: Any = self._model_id
        try:
            with offline_hub():
                for args, kwargs in (
                    ((), {"model": source}),
                    ((source,), {}),
                    ((), {"model": source, "revision": self._revision}),
                    ((source, self._revision), {}),
                ):
                    try:
                        session = Session(*args, **kwargs)
                        if session is not None:
                            return session
                    except TypeError:
                        continue
        except Exception as e:
            name = type(e).__name__
            raise AsrError(
                f"mlx_qwen3_asr could not load {source!r} from the local HF "
                f"cache ({name}: {e}) — the weights are not installed. "
                "Re-run the setup wizard (downloads happen ONLY during "
                "setup, never at first use)",
                code="asr_not_ready",
                fatal=False,
            ) from e
        raise AsrError(
            f"could not construct mlx_qwen3_asr.Session for {source!r} "
            "(unrecognized constructor signature)",
            code="asr_model_error",
            fatal=False,
        )

    # ── Streaming-API bridge (real package surface) ──────────────────

    def _init_streaming(self) -> None:
        """Open a streaming window on the loaded session."""
        session = self._session
        init = getattr(session, "init_streaming", None)
        if not callable(init):
            self._streaming_api = False
            return
        for args, kwargs in (
            (
                (),
                {
                    "chunk_size_sec": _STREAM_CHUNK_SEC,
                    "max_context_sec": _STREAM_MAX_CONTEXT_SEC,
                    "language": self._language,
                },
            ),
            ((), {"language": self._language}),
            ((), {"chunk_size_sec": _STREAM_CHUNK_SEC, "language": self._language}),
        ):
            try:
                state = init(*args, **kwargs)
                if state is not None:
                    self._stream_state = state
                    return
            except TypeError:
                continue
        self._streaming_api = False
        logger.warning(
            "mlx_qwen3_asr init_streaming signature not recognized — "
            "falling back to buffered transcription"
        )

    def _feed_streaming(self, samples: np.ndarray) -> None:
        """Feed float32 audio into the streaming state (off the event loop)."""
        session = self._session
        state = self._stream_state
        if session is None or state is None:
            return
        feed = getattr(session, "feed_audio", None)
        if not callable(feed):
            self._streaming_api = False
            return
        for args, kwargs in (
            ((samples, state), {}),
            ((), {"audio": samples, "state": state}),
            ((samples,), {}),
        ):
            try:
                new_state = feed(*args, **kwargs)
                # Released versions return the updated state; an earlier
                # build mutated the supplied state and returned None. Both
                # are one successful feed and must never cause the same audio
                # chunk to be submitted again through a fallback signature.
                effective_state = new_state if new_state is not None else state
                self._stream_state = effective_state
                text = getattr(effective_state, "text", None)
                if isinstance(text, str) and text:
                    self._last_partial_text = text
                return
            except TypeError:
                continue
        self._streaming_api = False

    def _finish_streaming(self, state: Any) -> str:
        """Close the streaming window → final transcript (off the loop)."""
        session = self._session
        finish = getattr(session, "finish_streaming", None)
        if callable(finish):
            try:
                final = finish(state)
                if final is not None:
                    if isinstance(final, str):
                        return final
                    return str(getattr(final, "text", "") or "")
            except TypeError:
                pass
        return str(getattr(state, "text", "") or "")

    # ── Buffered transcription fallback (older package surface) ──────

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
                (
                    (samples,),
                    {"sample_rate": ASR_SAMPLE_RATE, "language": self._language},
                ),
                ((samples,), {"language": self._language}),
                (
                    (),
                    {
                        "audio": samples,
                        "sample_rate": ASR_SAMPLE_RATE,
                        "language": self._language,
                    },
                ),
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
