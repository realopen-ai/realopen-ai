"""Pocket TTS engine — streaming synthesis behind :class:`TtsProvider`.

The REAL package API (verified against the released ``pocket-tts`` package
— the exact surface the user's local pipeline exercises)::

    from pocket_tts import TTSModel

    model = TTSModel.load_model(language="english_2026-04")   # HF-hub natural
    state = model.get_state_for_audio_prompt("mary")           # voice NAME
    stream = model.generate_audio_stream(state, "text")       # → iterator
    rate = model.sample_rate                                   # 24 000

Model persistence: ``load_model(language=…)`` resolves its weights through
the HuggingFace hub cache, which ``app.voice.hf_cache`` pins to the
persisted data volume (``data/huggingface/hub``). The setup wizard warms
the cache once (model + the configured voice's embedding); the runtime
load below runs inside :func:`offline_hub` so a warm cache loads instantly
and a cold cache fails FAST with a clear "re-run setup" error instead of
downloading ~200 MB mid-conversation (the original "weights being pulled /
app hangs" bug).

Legacy compatibility: installs made by the previous v2 asset layout
(``data/models/voice/tts/…/local-config.yaml`` + local safetensors) still
load offline from that directory — the engine probes the manifest first
and only falls back to the natural hub path when no valid local layout
exists.

Audio conversion: generated pieces (torch tensors / numpy arrays / WAV
bytes) are normalized to PCM s16le mono at ``settings.VOICE_TTS_SAMPLE_RATE``
(24 kHz) and emitted in ≤100 ms chunks. The engine's native output rate is
read from ``model.sample_rate`` (resampled when it differs).

Model instances are cached at module level (keyed by model+language) so
TTS is never reloaded per request; concurrent sessions share one model.
The package is NOT thread-safe for concurrent generation — a module-level
``threading.Lock`` serializes synthesis. Importing ``pocket_tts`` sets
``torch.set_num_threads(1)`` globally (which would cripple ASR inference
in the same process) — the previous thread count is restored after import.
"""

from __future__ import annotations

import asyncio
import io
import logging
import threading
import time
import wave
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Optional, Tuple

import numpy as np

from app.config import settings
from app.voice.audio import chunk_pcm, float32_to_pcm, resample_linear
from app.voice.hf_cache import ensure_hf_env, offline_hub
from app.voice.tts import TtsError, TtsProvider

logger = logging.getLogger(__name__)

# 100 ms of s16le @24 kHz — one WS binary frame per announce.
TTS_CHUNK_BYTES = settings.VOICE_TTS_SAMPLE_RATE // 10 * 2
# Supported source sample rates for the WAV-bytes path (others resample).
_WAV_SUPPORTED_RATES = {settings.VOICE_TTS_SAMPLE_RATE, 24000, 22050, 16000}

# The setup wizard's legacy per-install config (v2 asset layout, kept for
# backward compatibility with pre-HF-cache installs).
LOCAL_CONFIG_NAME = "local-config.yaml"

# A failed load is suppressed for this long (negative cache) so repeated
# warm-ups (per partial poll / per turn) cannot re-trigger load attempts.
_LOAD_FAILURE_TTL_S = 120.0

# Module-level shared model cache: (model_id, language) → loaded model.
_MODEL_CACHE: Dict[Tuple[str, Optional[str]], Any] = {}
_MODEL_CACHE_LOCK = asyncio.Lock()
# Negative cache: (model_id, language) → monotonic timestamp of failure.
_LOAD_FAILURES: Dict[Tuple[str, Optional[str]], float] = {}
# Voice states (per model+voice) — expensive to compute, safe to reuse
# (generate_audio_stream deep-copies state by default).
_VOICE_STATE_CACHE: Dict[Tuple[Optional[str], Optional[str]], Any] = {}
# pocket_tts models are NOT thread-safe for concurrent generation.
_GENERATION_LOCK = threading.Lock()
# Hold this for the complete lifetime of the streaming iterator. Calling a
# generator function does not execute its body, so the threading lock inside
# ``_start_generation`` alone cannot serialize actual model work.
_SYNTHESIS_LOCK = asyncio.Lock()


def _legacy_local_assets_dir() -> Optional[Path]:
    """Legacy v2 layout directory, when the manifest says it is valid.

    Resolved through app.voice.models_store — a valid entry means the
    local-config.yaml + weights + voice state are all on disk. None → the
    engine uses the natural hub-cache path.
    """
    try:
        from app.voice import models_store

        return models_store.tts_assets_dir()
    except Exception as e:  # manifest probing must never break loading
        logger.debug("models_store tts_assets_dir probe failed: %s", e)
        return None


def _voice_state_file(assets_dir: Path, voice: Optional[str]) -> Optional[Path]:
    """The setup-installed voice-state file for ``voice`` (None → default)."""
    name = (voice or "").strip() or "mary"
    safe = name.replace("/", "_").replace("\\", "_")
    candidate = assets_dir / f"voice-{safe}.safetensors"
    if candidate.exists():
        return candidate
    # Legacy layout: any voice-*.safetensors in the assets dir.
    matches = sorted(assets_dir.glob("voice-*.safetensors"))
    return matches[0] if matches else None


class PocketTtsEngine(TtsProvider):
    """Pocket TTS (``pocket_tts``) — streaming, sentence-at-a-time."""

    name = "pocket-tts"

    def __init__(self, spec: Any):
        self._spec = spec
        self._model_id = str(getattr(spec, "model", "") or "pocket-tts")
        self._language = getattr(spec, "language", None)
        self._voice = getattr(spec, "voice", None)
        self._model: Optional[Any] = None
        self._assets_dir: Optional[Path] = None
        self._native_rate: Optional[int] = None
        # Per-engine cancellation flag (one engine instance per session;
        # the shared model instance is stateless w.r.t. this flag).
        self._cancelled = False

    # ── TtsProvider interface ────────────────────────────────────────

    async def warm_up(self) -> Any:
        """Load the shared model instance (no-op on subsequent calls).

        The load is bounded by ``settings.VOICE_TTS_LOAD_TIMEOUT_SEC``: a
        warm HF cache (or legacy local assets) finishes well under it,
        while a cold cache fails fast inside the offline guard with a
        clear recoverable error instead of downloading forever. A failed
        load is negatively cached (short TTL) so repeated calls surface
        one error, not one per partial poll.
        """
        if self._model is not None:
            return self._model
        key = (self._model_id, self._language)
        failed_at = _LOAD_FAILURES.get(key)
        if failed_at is not None and time.monotonic() - failed_at < _LOAD_FAILURE_TTL_S:
            raise TtsError(
                "Pocket TTS model failed to load recently — retry suppressed "
                f"for {_LOAD_FAILURE_TTL_S:.0f}s (check the backend log; the "
                "usual fix is re-running the setup wizard)",
                code="tts_load_failed_recently",
                fatal=False,
            )
        async with _MODEL_CACHE_LOCK:
            cached = _MODEL_CACHE.get(key)
            if cached is None:
                timeout = max(1.0, float(settings.VOICE_TTS_LOAD_TIMEOUT_SEC))
                try:
                    cached = await asyncio.wait_for(
                        asyncio.to_thread(self._load_model), timeout=timeout
                    )
                    _MODEL_CACHE[key] = cached
                    _LOAD_FAILURES.pop(key, None)
                except asyncio.TimeoutError:
                    _LOAD_FAILURES[key] = time.monotonic()
                    raise TtsError(
                        f"Pocket TTS model load exceeded {timeout:.0f}s — the "
                        "local weights are missing or unreadable (run the "
                        "setup wizard to install them)",
                        code="tts_model_load_timeout",
                        fatal=False,
                    )
                except Exception:
                    _LOAD_FAILURES[key] = time.monotonic()
                    raise
                logger.info(
                    "Pocket TTS model loaded (model=%s, language=%s, voice=%s, "
                    "native_rate=%s)",
                    self._model_id,
                    self._language,
                    self._voice,
                    self._native_rate,
                )
            self._model = cached
            return cached

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Serialize complete generations against the shared model instance."""
        async with _SYNTHESIS_LOCK:
            async for chunk in self._synthesize_unlocked(text):
                yield chunk

    async def _synthesize_unlocked(self, text: str) -> AsyncIterator[bytes]:
        """Yield PCM s16le mono 24 kHz chunks for one text unit."""
        text = (text or "").strip()
        if not text:
            return
        model = await self.warm_up()
        state = await self._voice_state(model)
        self._cancelled = False
        # Start generation (blocking call) off the event loop, serialized
        # by the module lock (pocket_tts models are not thread-safe).
        audio_iter = await asyncio.to_thread(self._start_generation, model, state, text)
        if audio_iter is None:
            raise TtsError(
                "pocket_tts did not produce an audio stream " "(unrecognized API)",
                code="tts_model_error",
                fatal=False,
            )
        # The iterator may be sync (most likely) or async — both supported.
        if hasattr(audio_iter, "__anext__"):
            async for piece in audio_iter:
                if self._cancelled:
                    return
                for chunk in self._to_pcm_chunks(piece):
                    yield chunk
                if self._cancelled:
                    return
        else:
            while True:
                piece = await asyncio.to_thread(_next_or_none, audio_iter)
                if piece is _SENTINEL or piece is None:
                    break
                if self._cancelled:
                    return
                for chunk in self._to_pcm_chunks(piece):
                    yield chunk
                if self._cancelled:
                    return

    async def cancel(self) -> None:
        """Stop the in-flight synthesis (checked between chunks)."""
        self._cancelled = True

    async def enable_voice_cloning(self) -> Any:
        """Upgrade an ungated runtime model to the cloning-capable weights.

        Normal conversation startup stays strictly offline. This method is
        called only by the explicit Settings ▸ Voice WAV-import action, after
        the user has authenticated with Hugging Face, so downloading the
        gated weights here is intentional and visible to the user.
        """
        model = await self.warm_up()
        if bool(getattr(model, "has_voice_cloning", False)):
            return model
        key = (self._model_id, self._language)
        async with _MODEL_CACHE_LOCK:
            cached = _MODEL_CACHE.get(key)
            if cached is not None and bool(getattr(cached, "has_voice_cloning", False)):
                self._model = cached
                return cached
            try:
                upgraded = await asyncio.to_thread(self._load_cloning_model)
            except Exception as exc:
                raise TtsError(
                    f"Pocket TTS cloning weights could not be loaded: {exc}",
                    code="tts_voice_cloning_unavailable",
                    fatal=False,
                ) from exc
            if not bool(getattr(upgraded, "has_voice_cloning", False)):
                raise TtsError(
                    "Pocket TTS loaded its catalog-only fallback instead of "
                    "the cloning-capable weights. Verify Hugging Face access.",
                    code="tts_voice_cloning_unavailable",
                    fatal=False,
                )
            _MODEL_CACHE[key] = upgraded
            self._model = upgraded
            for state_key in list(_VOICE_STATE_CACHE):
                if state_key[0] == self._model_id:
                    _VOICE_STATE_CACHE.pop(state_key, None)
            return upgraded

    def status(self) -> dict:
        return {
            "provider": self.name,
            "model": self._model_id,
            "language": self._language,
            "voice": self._voice,
            "loaded": self._model is not None,
            "sample_rate": settings.VOICE_TTS_SAMPLE_RATE,
        }

    # ── voice state (pretrained embedding → model_state) ─────────────

    async def _voice_state(self, model: Any) -> Any:
        """The generation state for the configured voice (cached).

        Natural path: ``model.get_state_for_audio_prompt(<voice name>)``
        with the SPEAKER NAME (e.g. "mary") — exactly the released API.
        The setup wizard pre-cached the embedding for the configured voice;
        at runtime the lookup resolves from the local hub cache (offline).
        Legacy v2 installs keep loading from the local voice state file.
        """
        key = (self._model_id, self._voice)
        cached = _VOICE_STATE_CACHE.get(key)
        if cached is not None:
            return cached
        state = await asyncio.to_thread(self._import_voice_state, model)
        _VOICE_STATE_CACHE[key] = state
        return state

    def _import_voice_state(self, model: Any) -> Any:
        get_state = getattr(model, "get_state_for_audio_prompt", None)
        if not callable(get_state):
            raise TtsError(
                "pocket_tts model has no get_state_for_audio_prompt "
                "(unrecognized API)",
                code="tts_model_error",
                fatal=False,
            )
        # The legacy layout resolves the voice from a LOCAL state file.
        assets = self._assets_dir
        if assets is not None:
            voice_file = _voice_state_file(assets, self._voice)
            if voice_file is not None:
                return self._import_voice_state_from_file(get_state, voice_file)
        # Natural path — the voice NAME, resolved through the hub cache.
        name = str(self._voice or "mary").strip() or "mary"
        with offline_hub():
            try:
                state = get_state(name)
            except TypeError:
                state = None
            except Exception as e:
                raise TtsError(
                    f"pocket_tts voice {name!r} could not be loaded from the "
                    f"local cache ({e}) — re-run the setup wizard so it "
                    "caches the configured voice",
                    code="tts_not_ready",
                    fatal=False,
                ) from e
        if state is not None:
            return state
        # Older signatures accepting a path (kept for compatibility).
        if assets is not None:
            voice_file = _voice_state_file(assets, self._voice)
            if voice_file is not None:
                return self._import_voice_state_from_file(get_state, voice_file)
        raise TtsError(
            f"pocket_tts get_state_for_audio_prompt did not accept voice "
            f"{name!r} (unrecognized signature)",
            code="tts_model_error",
            fatal=False,
        )

    def _import_voice_state_from_file(self, get_state: Any, voice_file: Path) -> Any:
        for args, kwargs in (
            # NOTE: ((str(voice_file),), {}) — the inner tuple needs its
            # comma; ((str(voice_file)), {}) is just (str, {}) and would
            # splat the PATH STRING character-by-character into *args.
            ((str(voice_file),), {}),
            ((), {"voice": str(voice_file)}),
            ((), {"audio_conditioning": str(voice_file)}),
        ):
            try:
                state = get_state(*args, **kwargs)
                if state is not None:
                    return state
            except TypeError:
                continue
            except Exception as e:
                raise TtsError(
                    f"pocket_tts voice state import failed: {e}",
                    code="tts_model_error",
                    fatal=False,
                ) from e
        raise TtsError(
            "pocket_tts get_state_for_audio_prompt signature not recognized",
            code="tts_model_error",
            fatal=False,
        )

    # ── model loading ────────────────────────────────────────────────

    def _load_model(self) -> Any:
        pocket_tts = _import_pocket_tts()
        # 1. Legacy v2 layout: local config + weights on disk → offline load.
        assets = _legacy_local_assets_dir()
        if assets is not None:
            local_cfg = assets / LOCAL_CONFIG_NAME
            if local_cfg.exists():
                self._assets_dir = assets
                try:
                    model = self._construct_model(pocket_tts, local_cfg)
                    self._capture_rate(model)
                    return model
                except TtsError:
                    raise
                except Exception as e:
                    raise TtsError(
                        f"pocket_tts model load failed: {e}",
                        code="tts_model_error",
                        fatal=False,
                    ) from e
        # 2. Natural path: load_model(language=…) through the persisted HF
        #    hub cache — warm → instant; cold → fast clear error (offline).
        ensure_hf_env()
        try:
            model = self._construct_model_natural(pocket_tts)
            self._capture_rate(model)
            return model
        except TtsError:
            raise
        except Exception as e:
            raise TtsError(
                f"pocket_tts model load failed: {e}",
                code="tts_model_error",
                fatal=False,
            ) from e

    def _load_cloning_model(self) -> Any:
        """Load the natural model online; used only for explicit WAV import."""
        pocket_tts = _import_pocket_tts()
        ensure_hf_env()
        # Pocket TTS catches *every* gated-weight download error and silently
        # falls back to its catalog-only model. Fetch the exact weights path
        # from the installed language config first so authentication/network
        # failures remain visible and a successful download cannot fall back.
        from pocket_tts.utils.config import CONFIGS_DIR, load_config
        from pocket_tts.utils.utils import download_if_necessary

        language = str(self._language or "english")
        config = load_config(CONFIGS_DIR / f"{language}.yaml")
        if not config.weights_path:
            raise RuntimeError(
                f"Pocket TTS language {language!r} has no cloning weights"
            )
        download_if_necessary(config.weights_path)
        TTSModel = getattr(pocket_tts, "TTSModel", None)
        load = getattr(TTSModel, "load_model", None) if TTSModel is not None else None
        if not callable(load):
            raise RuntimeError("pocket_tts exposes no TTSModel.load_model")
        kwargs = {"language": self._language} if self._language else {}
        model = load(**kwargs)
        self._capture_rate(model)
        return model

    def _capture_rate(self, model: Any) -> None:
        """Remember the model's native output rate (resample source)."""
        rate = getattr(model, "sample_rate", None)
        if isinstance(rate, int) and rate > 0:
            self._native_rate = rate
        else:
            self._native_rate = settings.VOICE_TTS_SAMPLE_RATE

    def _construct_model_natural(self, pocket_tts: Any) -> Any:
        """``TTSModel.load_model(language=…)`` — the released call shape.

        Runs OFFLINE: the setup wizard warmed the hub cache (data volume),
        so this is a local load. A cold cache raises immediately with a
        clear recoverable error instead of pulling weights mid-conversation.
        """
        TTSModel = getattr(pocket_tts, "TTSModel", None)
        if TTSModel is None:
            raise TtsError(
                "pocket_tts exposes no TTSModel (unrecognized API)",
                code="tts_model_error",
                fatal=False,
            )
        load = getattr(TTSModel, "load_model", None)
        if not callable(load):
            # Some versions construct directly.
            for args, kwargs in (
                ((), {"language": self._language}),
                ((), {}),
            ):
                try:
                    model = TTSModel(*args, **kwargs)
                    if model is not None:
                        return model
                except TypeError:
                    continue
            raise TtsError(
                "pocket_tts API not recognized (no TTSModel.load_model)",
                code="tts_model_error",
                fatal=False,
            )
        calls: List[Tuple[tuple, dict]] = []
        if self._language:
            calls.append(((), {"language": self._language}))
        calls.append(((), {}))
        offline_error: Optional[Exception] = None
        with offline_hub():
            for args, kwargs in calls:
                try:
                    model = load(*args, **kwargs)
                    if model is not None:
                        return model
                except TypeError:
                    continue
                except Exception as e:  # noqa: BLE001 — remember the real cause
                    offline_error = e
                    break
        raise TtsError(
            "Pocket TTS weights are not in the local HF cache "
            f"({offline_error or 'load refused'}) — run the setup wizard "
            "(downloads happen ONLY during setup, never at first use)",
            code="tts_not_ready",
            fatal=False,
        )

    def _construct_model(self, pocket_tts: Any, local_cfg: Path) -> Any:
        """Legacy path: ``TTSModel.load_model(config=<local yaml>)``."""
        TTSModel = getattr(pocket_tts, "TTSModel", None)
        if TTSModel is None:
            raise TtsError(
                "pocket_tts exposes no TTSModel (unrecognized API)",
                code="tts_model_error",
                fatal=False,
            )
        load = getattr(TTSModel, "load_model", None)
        cfg_str = str(local_cfg)
        if callable(load):
            calls: List[Tuple[tuple, dict]] = [
                ((), {"config": cfg_str}),
                ((), {"config_path": cfg_str}),
                ((cfg_str,), {}),
            ]
            if self._language:
                calls.append(((), {"language": self._language, "config": cfg_str}))
            for args, kwargs in calls:
                try:
                    model = load(*args, **kwargs)
                    if model is not None:
                        return model
                except TypeError:
                    continue
                except Exception as e:
                    raise TtsError(
                        f"pocket_tts load_model failed: {e}",
                        code="tts_model_error",
                        fatal=False,
                    ) from e
            raise TtsError(
                "pocket_tts load_model did not accept the local config "
                "(unrecognized signature)",
                code="tts_model_error",
                fatal=False,
            )
        for args, kwargs in (
            ((), {"config": cfg_str}),
            ((cfg_str,), {}),
        ):
            try:
                return TTSModel(*args, **kwargs)
            except TypeError:
                continue
        raise TtsError(
            "pocket_tts API not recognized (no TTSModel.load_model with a "
            "known signature)",
            code="tts_model_error",
            fatal=False,
        )

    # ── generation ───────────────────────────────────────────────────

    def _start_generation(self, model: Any, state: Any, text: str) -> Any:
        """Start one generation (called off-thread; lock-serialized).

        Returns an iterator/async-iterator of audio pieces, or None when
        no recognized call shape produced a stream. The FIRST probe is the
        released signature: ``generate_audio_stream(state, text)``.
        """
        with _GENERATION_LOCK:
            gen = getattr(model, "generate_audio_stream", None)
            if callable(gen):
                calls: List[Tuple[tuple, dict]] = [
                    ((state, text), {}),
                    ((), {"model_state": state, "text_to_generate": text}),
                    ((), {"model_state": state, "text": text}),
                    ((), {"text": text}),
                ]
                for call in calls:
                    args, kwargs = call
                    try:
                        return gen(*args, **kwargs)
                    except TypeError:
                        continue
                    except Exception as e:
                        raise TtsError(
                            f"pocket_tts generate_audio_stream failed: {e}",
                            code="tts_model_error",
                            fatal=False,
                        ) from e
            # Non-streaming fallback: a one-shot generate call.
            for name in ("generate_audio", "generate", "synthesize"):
                one_shot = getattr(model, name, None)
                if callable(one_shot):
                    try:
                        return iter([one_shot(state, text)])
                    except TypeError:
                        try:
                            return iter([one_shot(text)])
                        except TypeError:
                            continue
            return None

    # ── audio conversion ─────────────────────────────────────────────

    def _to_pcm_chunks(self, piece: Any) -> list:
        """One generated audio piece → list of ≤100 ms s16le 24 kHz chunks.

        Handles torch tensors, numpy arrays (float/int, any amplitude),
        raw WAV bytes and raw PCM bytes. Arrays are at the engine's native
        output rate (``model.sample_rate``) unless the piece carries an
        explicit ``sample_rate``/``sampling_rate`` attribute.
        """
        torch_tensor = type(piece).__name__ == "Tensor" or str(type(piece)).endswith(
            "'Tensor'>"
        )
        if torch_tensor:
            try:
                piece = piece.detach().cpu().numpy()
            except Exception:  # noqa: BLE001 — non-torch duck typing
                pass
        pcm = self._piece_to_pcm(piece)
        if not pcm:
            return []
        return chunk_pcm(pcm, TTS_CHUNK_BYTES)

    def _piece_to_pcm(self, piece: Any) -> bytes:
        target_rate = settings.VOICE_TTS_SAMPLE_RATE
        if isinstance(piece, (bytes, bytearray, memoryview)):
            raw = bytes(piece)
            if raw[:4] == b"RIFF" and raw[8:12] == b"WAVE":
                return self._wav_to_pcm(raw, target_rate)
            return raw  # assume already PCM s16le @ target rate
        if isinstance(piece, np.ndarray):
            arr = piece
            if arr.ndim > 1:
                arr = arr.reshape(-1)[...]  # mono: flatten
            src_rate = self._native_rate or target_rate
            for attr in ("sample_rate", "sampling_rate", "sr"):
                rate = getattr(piece, attr, None)
                if isinstance(rate, int) and rate > 0:
                    src_rate = rate
                    break
            samples = arr.astype(np.float32)
            if src_rate != target_rate:
                samples = resample_linear(samples, src_rate, target_rate)
            return float32_to_pcm(samples)
        # Scalar-holder objects (e.g. types.SimpleNamespace(data=...)).
        for attr in ("audio", "data", "samples", "pcm", "wav"):
            inner = getattr(piece, attr, None)
            if inner is not None and inner is not piece:
                return self._piece_to_pcm(inner)
        return b""

    @staticmethod
    def _wav_to_pcm(raw: bytes, target_rate: int) -> bytes:
        """Parse WAV bytes → PCM s16le mono @ target_rate (stdlib wave)."""
        try:
            with wave.open(io.BytesIO(raw)) as wav:
                n_channels = wav.getnchannels() or 1
                samp_width = wav.getsampwidth()
                rate = wav.getframerate() or target_rate
                frames = wav.readframes(wav.getnframes())
        except Exception as e:
            logger.warning("WAV parse failed (%s) — treating as raw PCM", e)
            return raw
        if samp_width == 2 and n_channels == 1 and rate == target_rate:
            return frames
        # Normalize: → float32 mono → resample → s16le.
        if samp_width == 2:
            from app.voice.audio import pcm_to_float32

            samples = pcm_to_float32(frames)
        elif samp_width == 4:  # 32-bit int WAV
            ints = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
            samples = ints
        elif samp_width == 1:  # unsigned 8-bit
            u8 = np.frombuffer(frames, dtype=np.uint8).astype(np.float32)
            samples = (u8 - 128.0) / 128.0
        else:
            return frames
        if n_channels > 1:
            samples = samples.reshape(-1, n_channels).mean(axis=1)
        if rate != target_rate:
            samples = resample_linear(samples, rate, target_rate)
        return float32_to_pcm(samples)


def _import_pocket_tts() -> Any:
    """Import pocket_tts, neutralizing its global torch-thread override.

    ``pocket_tts`` runs ``torch.set_num_threads(1)`` at import time, which
    would cripple ASR inference in the same process. The previous value
    is restored right after the import.
    """
    try:
        import torch  # noqa: WPS433 — thread-count save/restore
    except ImportError:
        torch = None
    threads_before = torch.get_num_threads() if torch is not None else None
    try:
        import pocket_tts  # type: ignore  # noqa: PLC0415
    except ImportError as e:
        raise TtsError(
            "TTS runtime missing: module 'pocket_tts' is not installed "
            "(setup wizard → voice dependencies)",
            code="tts_runtime_missing",
            fatal=False,
        ) from e
    if torch is not None and threads_before is not None:
        try:
            current = torch.get_num_threads()
            if current != threads_before:
                torch.set_num_threads(threads_before)
                logger.debug(
                    "restored torch num_threads=%d (pocket_tts set it to 1)",
                    threads_before,
                )
        except Exception:  # noqa: BLE001 — best-effort restore
            pass
    return pocket_tts


# Sentinel distinguishing "iterator exhausted" from a falsy audio piece.
_SENTINEL = object()


def _next_or_none(iterator: Any) -> Any:
    """Advance a SYNC iterator (generation lock held); _SENTINEL when done."""
    try:
        return next(iterator)
    except StopIteration:
        return _SENTINEL
    except Exception as e:  # provider-side failure mid-stream
        raise TtsError(
            f"pocket_tts audio stream failed: {e}",
            code="tts_model_error",
            fatal=False,
        ) from e
