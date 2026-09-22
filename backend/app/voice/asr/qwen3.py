"""Qwen3-ASR engine (cross-platform: macOS/Windows/Linux).

Two runtimes, probed in order (NEVER assumed):

1. **``qwen_asr`` (official package)** — the primary path. The
   Qwen/Qwen3-ASR-0.6B checkpoint ships in the *thinker export* layout
   (nested ``thinker_config`` + ``thinker.*`` weight keys), which plain
   ``transformers`` releases mis-parse (they apply default sub-configs →
   a structurally WRONG model). The official ``qwen-asr`` runtime carries
   the matching implementation and loads the snapshot directly::

       from qwen_asr import Qwen3ASRModel
       model = Qwen3ASRModel.from_pretrained(<local snapshot>, dtype=…,
                                             low_cpu_mem_usage=True)

   Memory: ``dtype`` defaults to the ``VOICE_ASR_DTYPE`` policy ("auto" →
   bfloat16 on CPU, float16 with CUDA) — fp32 would double the resident
   size and was the root cause of the "app hangs while weights load" bug
   on low-RAM hosts. ``low_cpu_mem_usage=True`` keeps the peak at ≈ model
   size instead of ≈ 3× model size during load.

2. **transformers fallback** — for environments without ``qwen-asr``.
   Probes the dedicated class names (both ``Qwen3Asr*`` and the released
   ``Qwen3ASR*`` spellings), then the speech-seq2seq auto class and the
   high-level pipeline. Any failure raises :class:`AsrError`.

Honest capability statement (task spec): Qwen3-ASR does not expose a
first-class incremental/streaming decode API. This engine implements
**chunked re-transcription**:

* ``feed_audio`` accumulates the utterance's cleaned PCM (s16le 16k mono).
* ``get_partial`` re-transcribes the ENTIRE buffered utterance, throttled
  to at most one inference every ``partial_interval_s`` (default 0.7 s)
  once at least ``min_partial_ms`` (default 400 ms) of audio is buffered.
* ``finish_stream`` transcribes the full buffer and returns the final text.

Model loading is lazy + shared (module-level cache keyed by model+revision)
with a NEGATIVE cache: a failed load is remembered (with a short TTL) so
repeated partial polls cannot re-trigger load attempts — the failure
surfaces once as an error frame instead of spamming every frame.
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
# Partials are SKIPPED once the utterance exceeds this much buffered audio:
# chunked re-transcription costs a FULL pass per partial, so on slow boxes
# (or long utterances) the queue of growing re-transcriptions never catches
# up and starves the final transcription. The final pass always covers the
# complete utterance, so capping partials only limits mid-utterance updates.
DEFAULT_PARTIAL_MAX_SEC = 8.0

# A failed load is cached for this long before a retry is allowed (the
# negative cache stops per-partial error-frame spam while still letting a
# user recover after installing runtimes without a server restart).
_LOAD_FAILURE_TTL_S = 120.0


def _local_snapshot_dir(model_id: str) -> Optional[str]:
    """Local snapshot directory for ``model_id``, when cached on disk.

    Resolution order:
    1. The persisted HF hub cache (``data/huggingface/hub``) — the setup
       wizard warms it naturally; the snapshot path makes the load a
       purely local file operation.
    2. The legacy manifest layout (``data/models/voice/asr/…``) — pre-cache
       installs keep working (and the wizard seeds them into the cache).
    None → the caller falls back to the natural repo-id load (offline
    guarded — never a silent download).
    """
    try:
        from app.voice import models_store

        manifest = models_store.load_manifest()
        entry = manifest.get("asr") or {}
        if entry.get("model") == model_id and entry.get("complete"):
            # 1. HF cache snapshot (store: "hf" entries).
            snap = models_store.asr_snapshot_dir(entry)
            if snap is not None:
                return str(snap)
            # 2. Legacy local layout.
            rel = entry.get("path") or ""
            if rel:
                from pathlib import Path

                p = Path(models_store.MODELS_DIR) / rel
                if p.is_dir() and any(p.iterdir()):
                    return str(p)
    except Exception as e:  # manifest probing must never break loading
        logger.debug("models_store manifest probe failed: %s", e)
    # Cache hit without a manifest (manual snapshot_download etc.).
    try:
        from app.voice.hf_cache import ensure_hf_env, snapshot_dir

        ensure_hf_env()
        snap = snapshot_dir(model_id, "main")
        if snap is not None:
            return str(snap)
    except Exception as e:  # noqa: BLE001 — probe must never break loading
        logger.debug("HF cache snapshot probe failed: %s", e)
    return None


def _resolve_dtype() -> str:
    """VOICE_ASR_DTYPE policy → a torch dtype name string ("auto" → bf16
    on CPU / fp16 with CUDA)."""
    from app.config import settings

    raw = str(getattr(settings, "VOICE_ASR_DTYPE", "auto") or "auto").strip().lower()
    if raw in ("bfloat16", "float16", "float32", "bf16", "fp16", "fp32"):
        alias = {"bf16": "bfloat16", "fp16": "float16", "fp32": "float32"}
        return alias.get(raw, raw)
    # auto
    try:
        import torch  # noqa: WPS433 — presence probe only

        if torch.cuda.is_available():
            return "float16"
    except Exception:  # noqa: BLE001 — torch missing → engine raises later
        pass
    return "bfloat16"


class _LoadedModel:
    """A loaded ASR runtime (shared across sessions).

    Exactly one of ``qwen_asr`` (the official Qwen3ASRModel wrapper) or
    the transformers trio (processor/model/pipeline) is set.
    """

    __slots__ = ("qwen_asr", "processor", "model", "pipeline", "torch", "source")

    def __init__(
        self,
        qwen_asr: Any = None,
        processor: Any = None,
        model: Any = None,
        pipeline: Any = None,
        torch: Any = None,
        source: str = "",
    ):
        self.qwen_asr = qwen_asr
        self.processor = processor
        self.model = model
        self.pipeline = pipeline
        self.torch = torch
        self.source = source


# Module-level model cache: (model_id, revision) -> _LoadedModel.
# Concurrent sessions share one loaded instance (guarded by one lock).
_MODEL_CACHE: Dict[Tuple[str, str], _LoadedModel] = {}
_MODEL_CACHE_LOCK = asyncio.Lock()
# Negative cache: (model_id, revision) -> monotonic timestamp of failure.
_LOAD_FAILURES: Dict[Tuple[str, str], float] = {}


class Qwen3AsrEngine(AsrProvider):
    """Qwen3-ASR (official qwen-asr runtime primary; transformers fallback)."""

    name = "qwen3-asr"
    runtime = "qwen-asr/transformers"

    def __init__(
        self,
        spec: Any,
        min_partial_ms: int = DEFAULT_MIN_PARTIAL_MS,
        partial_interval_s: float = DEFAULT_PARTIAL_INTERVAL_S,
        partial_max_sec: float = DEFAULT_PARTIAL_MAX_SEC,
    ):
        self._spec = spec
        self._model_id = str(getattr(spec, "model", "") or "")
        self._revision = str(getattr(spec, "revision", None) or "main")
        self._language = str(getattr(spec, "language", None) or "").strip() or None
        self._min_partial_ms = min_partial_ms
        self._partial_interval_s = partial_interval_s
        self._partial_max_sec = partial_max_sec

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
        if buffered_ms > self._partial_max_sec * 1000.0:
            # Long utterance: a full re-transcription pass per partial no
            # longer keeps up — serve the last partial and let the final
            # pass (finish_stream) produce the complete transcript.
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

    async def warm_up(self) -> None:
        """Preload the shared model (called at voice session start so the
        first utterance never pays the load latency)."""
        async with self._lock:
            await self._ensure_model()

    def status(self) -> dict:
        return {
            "provider": self.name,
            "model": self._model_id,
            "revision": self._revision,
            "loaded": self._loaded is not None,
            "runtime": self.runtime,
            "partials": True,  # chunked re-transcription partials
        }

    # ── Model loading (shared, lazy, negatively cached) ──────────────

    async def _ensure_model(self) -> _LoadedModel:
        if self._loaded is not None:
            return self._loaded
        key = (self._model_id, self._revision)
        failed_at = _LOAD_FAILURES.get(key)
        if failed_at is not None and time.monotonic() - failed_at < _LOAD_FAILURE_TTL_S:
            raise AsrError(
                "ASR model failed to load recently — retry suppressed for "
                f"{_LOAD_FAILURE_TTL_S:.0f}s (check the backend log; the "
                "usual cause is a missing qwen-asr runtime — re-run the "
                "setup wizard)",
                code="asr_load_failed_recently",
                fatal=False,
            )
        async with _MODEL_CACHE_LOCK:
            from app.config import settings  # local import avoids cycles

            cached = _MODEL_CACHE.get(key)
            if cached is None:
                try:
                    timeout = max(1.0, float(settings.VOICE_ASR_LOAD_TIMEOUT_SEC))
                    cached = await asyncio.wait_for(
                        asyncio.to_thread(self._load_model_sync), timeout=timeout
                    )
                    _MODEL_CACHE[key] = cached
                    _LOAD_FAILURES.pop(key, None)
                    logger.info(
                        "Qwen3Asr model loaded (source=%s, model=%s@%s)",
                        cached.source,
                        self._model_id,
                        self._revision,
                    )
                except asyncio.TimeoutError:
                    _LOAD_FAILURES[key] = time.monotonic()
                    raise AsrError(
                        f"ASR model load exceeded {timeout:.0f}s — the local "
                        "weights are missing or unreadable (run the setup "
                        "wizard to install them)",
                        code="asr_model_load_timeout",
                        fatal=False,
                    )
                except Exception:
                    _LOAD_FAILURES[key] = time.monotonic()
                    raise
            self._loaded = cached
            return cached

    def _load_model_sync(self) -> _LoadedModel:
        from app.voice.hf_cache import ensure_hf_env, offline_hub

        source: Any = _local_snapshot_dir(self._model_id)
        if source is None:
            source = self._model_id  # natural repo-id load (cache-first)

        # The persisted HF cache owns the weights; the load below runs with
        # the hub OFFLINE so a warm cache resolves instantly and a cold one
        # fails FAST with a clear error instead of downloading mid-turn.
        ensure_hf_env()
        with offline_hub():
            return self._load_model_from(source)

    def _load_model_from(self, source: Any) -> _LoadedModel:
        # Primary: the official qwen-asr runtime.
        try:
            from qwen_asr import Qwen3ASRModel  # noqa: PLC0415
        except ImportError:
            Qwen3ASRModel = None  # type: ignore[assignment]
        if Qwen3ASRModel is not None:
            try:
                import torch  # noqa: F401 — dtype policy below
            except ImportError as e:
                raise AsrError(
                    "ASR runtime missing: module 'torch' is not installed "
                    "(setup wizard → voice dependencies)",
                    code="asr_runtime_missing",
                    fatal=False,
                ) from e
            dtype_name = _resolve_dtype()
            dtype = getattr(torch, dtype_name, torch.bfloat16)
            try:
                model = Qwen3ASRModel.from_pretrained(
                    str(source), dtype=dtype, low_cpu_mem_usage=True
                )
                return _LoadedModel(qwen_asr=model, torch=torch, source=str(source))
            except Exception as e:
                logger.warning(
                    "qwen_asr.from_pretrained failed (%s) — trying the "
                    "plain transformers fallback",
                    e,
                )

        # Fallback: plain transformers (correct only for transformers
        # releases whose qwen3_asr graph matches the snapshot layout).
        try:
            import torch
            from transformers import AutoProcessor
        except ImportError as e:
            missing = getattr(e, "name", None) or "torch/transformers"
            raise AsrError(
                f"ASR runtime missing: module {missing!r} is not installed "
                "(setup wizard → voice dependencies; the recommended "
                "runtime is the official 'qwen-asr' package)",
                code="asr_runtime_missing",
                fatal=False,
            ) from e

        load_kwargs: Dict[str, Any] = {}
        if source == self._model_id and self._revision and self._revision != "local":
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

        # Dedicated classes — BOTH spellings across releases (the released
        # transformers 5.x name is all-caps Qwen3ASR*).
        for cls_name in (
            "Qwen3ASRForConditionalGeneration",
            "Qwen3AsrForConditionalGeneration",
            "Qwen3ASRForCausalLM",
            "Qwen3AsrForCausalLM",
            "Qwen3ASRModel",
            "Qwen3AsrModel",
        ):
            cls = getattr(transformers, cls_name, None)
            if cls is None:
                continue
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
        audio = np.asarray(samples, dtype=np.float32)
        try:
            if loaded.qwen_asr is not None:
                # Official runtime: (samples, sr) tuple + context prompt.
                results = loaded.qwen_asr.transcribe(
                    audio=(audio, ASR_SAMPLE_RATE),
                    context="",
                    language=self._language,
                )
                if not results:
                    return ""
                text = getattr(results[0], "text", "") or ""
                return str(text)

            if loaded.pipeline is not None:
                result = loaded.pipeline(
                    {"array": audio, "sampling_rate": ASR_SAMPLE_RATE}
                )
                text = result.get("text", "") if isinstance(result, dict) else ""
                return str(text or "")

            inputs = loaded.processor(
                text="",
                audio=audio,
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
