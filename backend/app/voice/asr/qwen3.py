"""Qwen3-ASR engine (transformers, cross-platform: macOS/Windows/Linux).

Qwen3-ASR does not expose a first-class incremental/streaming decode API
in ``transformers``.
This engine therefore implements a**chunked re-transcription** strategy:

* ``feed_audio`` accumulates the utterance's cleaned PCM (s16le 16k mono).
* ``get_partial`` re-transcribes the ENTIRE buffered utterance, throttled
  to at most one inference every ``partial_interval_s`` (default 0.7 s)
  once at least ``min_partial_ms`` (default 400 ms) of audio is buffered.
* ``finish_stream`` transcribes the full buffer and returns the final text.

This is real partial behavior (the transcript grows as audio arrives), at
the cost of repeated full-utterance inference — acceptable for ≤30 s
utterances (``settings.VOICE_MAX_UTTERANCE_SEC``) and far simpler than
maintaining a streaming decoder. The runtime hint for a true streaming
implementation is the optional ``mlx_qwen3_asr`` engine (see mlx_engine).

Model loading:
* lazy — the first ``start_stream()`` triggers the load;
* shared — a module-level cache keyed by (model, revision) so concurrent
  sessions reuse one instance;
* local-first — the setup wizard downloads the snapshot to
  ``data/models/voice/asr/<name>`` (manifest-validated); the engine
  prefers that directory and falls back to the HuggingFace repo id.

The transformers API is probed, never assumed: the dedicated
``Qwen3AsrForConditionalGeneration`` class is tried first (attribute
lookup on the transformers module), then
``AutoModelForSpeechSeq2Seq``/``pipeline`` fallbacks. Any load or
inference failure raises :class:`AsrError` (code ``asr_model_error``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Tuple

import numpy as np

from app.voice.audio import pcm_to_float32, resample_linear
from app.voice.asr import AsrError, AsrProvider

logger = logging.getLogger(__name__)

# Input contract: s16le mono 16 kHz (voice protocol v1).
ASR_SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2

# Partial strategy tuning (chunked re-transcription).
DEFAULT_MIN_PARTIAL_MS = 400  # audio needed before the first partial
DEFAULT_PARTIAL_INTERVAL_S = 0.7  # min spacing between partial inferences


class _LoadedModel:
    """A loaded processor+model pair (shared across sessions)."""

    __slots__ = ("processor", "model", "pipeline", "torch", "source")

    def __init__(self, processor, model, pipeline, torch, source: str):
        self.processor = processor
        self.model = model
        self.pipeline = pipeline  # non-None only in the pipeline fallback
        self.torch = torch
        self.source = source


# Module-level model cache: (model_id, revision) -> _LoadedModel.
# Concurrent sessions share one loaded instance (guarded by one lock).
_MODEL_CACHE: Dict[Tuple[str, str], _LoadedModel] = {}
_MODEL_CACHE_LOCK = asyncio.Lock()


def _local_snapshot_dir(model_id: str) -> Optional[str]:
    """Local snapshot directory for ``model_id`` from the voice manifest,
    when the setup wizard installed it there. None → load from HF."""
    try:
        from app.voice import models_store

        manifest = models_store.load_manifest()
        entry = manifest.get("asr") or {}
        if entry.get("model") == model_id and entry.get("complete"):
            rel = entry.get("path") or ""
            if rel:
                from pathlib import Path

                p = Path(models_store.MODELS_DIR) / rel
                if p.is_dir() and any(p.iterdir()):
                    return str(p)
    except Exception as e:  # manifest probing must never break loading
        logger.debug("models_store manifest probe failed: %s", e)
    return None


class Qwen3AsrEngine(AsrProvider):
    """Qwen3-ASR via transformers (CPU-friendly, cross-platform)."""

    name = "qwen3-asr"
    runtime = "transformers"

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

        self._loaded: Optional[_LoadedModel] = None
        self._buffer = bytearray()
        self._last_partial_text = ""
        self._last_partial_at = 0.0
        self._stream_open = False
        # Serializes transcriptions of this engine instance (one session).
        self._lock = asyncio.Lock()

    # ── AsrProvider interface ────────────────────────────────────────

    async def start_stream(self) -> None:
        """Begin a new utterance (loads the model on first call)."""
        async with self._lock:
            self._buffer.clear()
            self._last_partial_text = ""
            self._last_partial_at = 0.0
            self._stream_open = True
            await self._ensure_model()

    async def feed_audio(self, pcm: bytes) -> None:
        if not self._stream_open:
            return  # audio between streams is dropped (engine was reset)
        self._buffer += pcm

    async def get_partial(self) -> str:
        """Throttled chunked re-transcription partial (see module doc)."""
        if not self._stream_open:
            return ""
        buffered_ms = len(self._buffer) * 1000.0 / (ASR_SAMPLE_RATE * BYTES_PER_SAMPLE)
        if buffered_ms < self._min_partial_ms:
            return self._last_partial_text
        now = time.monotonic()
        if now - self._last_partial_at < self._partial_interval_s:
            return self._last_partial_text
        if self._lock.locked():
            # A transcription is already running (load/finish) — return
            # the last computed partial instead of queueing behind it.
            return self._last_partial_text
        async with self._lock:
            self._last_partial_at = time.monotonic()
            text = await asyncio.to_thread(self._transcribe, bytes(self._buffer))
        if text:
            self._last_partial_text = text
        return self._last_partial_text

    async def finish_stream(self) -> str:
        """Transcribe the full buffered utterance → FINAL transcript."""
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
            "loaded": self._loaded is not None,
            "runtime": self.runtime,
            "partials": True,  # chunked re-transcription partials
        }

    # ── Model loading (shared, lazy) ─────────────────────────────────

    async def _ensure_model(self) -> _LoadedModel:
        if self._loaded is not None:
            return self._loaded
        key = (self._model_id, self._revision)
        async with _MODEL_CACHE_LOCK:
            cached = _MODEL_CACHE.get(key)
            if cached is None:
                cached = await asyncio.to_thread(self._load_model_sync)
                _MODEL_CACHE[key] = cached
                logger.info(
                    "Qwen3Asr model loaded (source=%s, model=%s@%s)",
                    cached.source,
                    self._model_id,
                    self._revision,
                )
            self._loaded = cached
            return cached

    def _load_model_sync(self) -> _LoadedModel:
        try:
            import torch  # noqa: F401 — presence check + inference use
            from transformers import AutoProcessor
        except ImportError as e:
            missing = getattr(e, "name", None) or "torch/transformers"
            raise AsrError(
                f"ASR runtime missing: module {missing!r} is not installed "
                "(setup wizard → voice dependencies)",
                code="asr_runtime_missing",
                fatal=False,
            ) from e

        # Prefer the setup wizard's local snapshot; fall back to HF hub.
        source: Any = _local_snapshot_dir(self._model_id)
        load_kwargs: Dict[str, Any] = {}
        if source is None:
            source = self._model_id
            if self._revision and self._revision != "local":
                load_kwargs["revision"] = self._revision

        try:
            processor = AutoProcessor.from_pretrained(source, **load_kwargs)
            model = self._resolve_model_cls(source, load_kwargs)
            if model is None:
                # Last-resort fallback: the high-level pipeline API.
                from transformers import pipeline as hf_pipeline

                pipe = hf_pipeline(
                    "automatic-speech-recognition", model=source, **load_kwargs
                )
                return _LoadedModel(processor, None, pipe, torch, str(source))
            return _LoadedModel(processor, model, None, torch, str(source))
        except AsrError:
            raise
        except Exception as e:
            raise AsrError(
                f"failed to load Qwen3-ASR model {self._model_id}@{self._revision}: {e}",
                code="asr_model_error",
                fatal=False,
            ) from e

    def _resolve_model_cls(self, source: Any, load_kwargs: Dict[str, Any]):
        """Probe the dedicated Qwen3-ASR class, then speech-seq2seq auto class."""
        import transformers

        # Dedicated classes (name varies across transformers versions).
        for cls_name in (
            "Qwen3AsrForConditionalGeneration",
            "Qwen3AsrForCausalLM",
            "Qwen3AsrModel",
        ):
            cls = getattr(transformers, cls_name, None)
            if cls is not None:
                try:
                    model = cls.from_pretrained(source, **load_kwargs)
                except Exception as e:  # wrong class for this snapshot
                    logger.warning("%s.from_pretrained failed: %s", cls_name, e)
                    continue
                if hasattr(model, "eval"):
                    model.eval()
                return model
        # Generic speech-seq2seq fallback.
        try:
            from transformers import AutoModelForSpeechSeq2Seq

            model = AutoModelForSpeechSeq2Seq.from_pretrained(source, **load_kwargs)
            if hasattr(model, "eval"):
                model.eval()
            return model
        except Exception as e:
            logger.warning("AutoModelForSpeechSeq2Seq failed: %s", e)
            return None

    # ── Transcription core ───────────────────────────────────────────

    def _transcribe(self, pcm: bytes) -> str:
        """Blocking inference — always called via asyncio.to_thread."""
        if self._loaded is None:
            raise AsrError(
                "transcription attempted before model load",
                code="asr_not_loaded",
                fatal=False,
            )
        loaded = self._loaded
        samples = pcm_to_float32(pcm)
        if len(samples) == 0:
            return ""
        # Defensive resample (input contract is 16 kHz; resample_linear is
        # a no-op when already correct).
        samples = resample_linear(samples, ASR_SAMPLE_RATE, ASR_SAMPLE_RATE)
        try:
            if loaded.pipeline is not None:
                result = loaded.pipeline(
                    {"array": np.asarray(samples), "sampling_rate": ASR_SAMPLE_RATE}
                )
                text = result.get("text", "") if isinstance(result, dict) else ""
                return str(text or "")

            inputs = loaded.processor(
                audio=np.asarray(samples),
                sampling_rate=ASR_SAMPLE_RATE,
                return_tensors="pt",
            )
            torch = loaded.torch
            with torch.no_grad():
                try:
                    generated = loaded.model.generate(**inputs)
                except TypeError:
                    # Some ASR heads reject extra processor kwargs — retry
                    # with the canonical feature key only.
                    key = next(
                        (
                            k
                            for k in ("input_features", "input_values", "input_ids")
                            if k in inputs
                        ),
                        None,
                    )
                    if key is None:
                        raise
                    generated = loaded.model.generate(**{key: inputs[key]})
            decoded = loaded.processor.batch_decode(generated, skip_special_tokens=True)
            text = decoded[0] if decoded else ""
            return str(text or "")
        except AsrError:
            raise
        except Exception as e:
            raise AsrError(
                f"Qwen3-ASR transcription failed: {e}",
                code="asr_model_error",
                fatal=False,
            ) from e
