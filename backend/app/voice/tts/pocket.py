"""Pocket TTS engine — streaming synthesis behind :class:`TtsProvider`.

The ``pocket_tts`` package is imported lazily (missing runtime →
:class:`TtsError` code ``tts_runtime_missing``, fatal=False). Its public
surface is probed with getattr / signature fallbacks — NEVER assumed —
because the binding names differ across releases. The names probed (and
the order) follow the task spec's documented API:

    pocket_tts.TTSModel.load_model(...)          # language / model load
    model.get_state_for_audio_prompt(...)        # optional voice state
    model.generate_audio_stream(...)             # streaming synthesis

Whenever an exact name is absent, successive fallbacks are tried and the
outcome is logged — an API that cannot be recognized at runtime raises
``tts_model_error`` (fatal=False) rather than silently producing silence.

Audio conversion: generated audio (numpy float/int arrays or WAV bytes)
is normalized to PCM s16le mono at ``settings.VOICE_TTS_SAMPLE_RATE``
(24 kHz) and emitted in ≤100 ms chunks.

Model instances are cached at module level (keyed by model+language) so
TTS is never reloaded per request; concurrent sessions share one model.
"""

from __future__ import annotations

import asyncio
import io
import logging
import wave
from typing import Any, AsyncIterator, Dict, Optional, Tuple

import numpy as np

from app.config import settings
from app.voice.audio import chunk_pcm, float32_to_pcm, resample_linear
from app.voice.tts import TtsError, TtsProvider

logger = logging.getLogger(__name__)

# 100 ms of s16le @24 kHz — one WS binary frame per announce.
TTS_CHUNK_BYTES = settings.VOICE_TTS_SAMPLE_RATE // 10 * 2
# Supported source sample rates for the WAV-bytes path (others resample).
_WAV_SUPPORTED_RATES = {settings.VOICE_TTS_SAMPLE_RATE, 24000, 22050, 16000}

# Module-level shared model cache: (model_id, language) → loaded model.
_MODEL_CACHE: Dict[Tuple[str, Optional[str]], Any] = {}
_MODEL_CACHE_LOCK = asyncio.Lock()


class PocketTtsEngine(TtsProvider):
    """Pocket TTS (``pocket_tts``) — streaming, sentence-at-a-time."""

    name = "pocket-tts"

    def __init__(self, spec: Any):
        self._spec = spec
        self._model_id = str(getattr(spec, "model", "") or "pocket-tts")
        self._language = getattr(spec, "language", None)
        self._voice = getattr(spec, "voice", None)
        self._model: Optional[Any] = None
        # Per-engine cancellation flag (one engine instance per session;
        # the shared model instance is stateless w.r.t. this flag).
        self._cancelled = False

    # ── TtsProvider interface ────────────────────────────────────────

    async def warm_up(self) -> Any:
        """Load the shared model instance (no-op on subsequent calls)."""
        if self._model is not None:
            return self._model
        key = (self._model_id, self._language)
        async with _MODEL_CACHE_LOCK:
            cached = _MODEL_CACHE.get(key)
            if cached is None:
                cached = await asyncio.to_thread(self._load_model)
                _MODEL_CACHE[key] = cached
                logger.info(
                    "Pocket TTS model loaded (model=%s, language=%s, voice=%s)",
                    self._model_id,
                    self._language,
                    self._voice,
                )
            self._model = cached
            return cached

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        """Yield PCM s16le mono 24 kHz chunks for one text unit."""
        text = (text or "").strip()
        if not text:
            return
        model = await self.warm_up()
        self._cancelled = False
        # Start generation (blocking call) off the event loop.
        audio_iter = await asyncio.to_thread(self._start_generation, model, text)
        if audio_iter is None:
            raise TtsError(
                "pocket_tts did not produce an audio stream "
                "(unrecognized API)",
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

    def status(self) -> dict:
        return {
            "provider": self.name,
            "model": self._model_id,
            "language": self._language,
            "voice": self._voice,
            "loaded": self._model is not None,
            "sample_rate": settings.VOICE_TTS_SAMPLE_RATE,
        }

    # ── model loading ────────────────────────────────────────────────

    def _load_model(self) -> Any:
        try:
            import pocket_tts  # type: ignore
        except ImportError as e:
            raise TtsError(
                "TTS runtime missing: module 'pocket_tts' is not installed "
                "(setup wizard → voice dependencies)",
                code="tts_runtime_missing",
                fatal=False,
            ) from e
        try:
            return self._construct_model(pocket_tts)
        except TtsError:
            raise
        except Exception as e:
            raise TtsError(
                f"pocket_tts model load failed: {e}",
                code="tts_model_error",
                fatal=False,
            ) from e

    def _construct_model(self, pocket_tts: Any) -> Any:
        """Probe the documented constructor shapes, best match first."""
        TTSModel = getattr(pocket_tts, "TTSModel", None)
        if TTSModel is not None:
            load = getattr(TTSModel, "load_model", None)
            if callable(load):
                # Signature variants seen/documented for load_model.
                for args, kwargs in (
                    ((), {}),
                    ((self._language,), {}),
                    ((self._model_id,), {}),
                    ((self._model_id, self._language), {}),
                    ((), {"language": self._language}),
                    ((), {"model": self._model_id, "language": self._language}),
                ):
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
                logger.warning(
                    "pocket_tts.TTSModel.load_model signature not recognized"
                )
            # Some versions construct directly.
            for args, kwargs in (
                ((), {}),
                ((self._language,), {}),
                ((), {"language": self._language}),
            ):
                try:
                    return TTSModel(*args, **kwargs)
                except TypeError:
                    continue
        # Module-level factories as a last resort.
        for name in ("load_model", "get_model", "create_model", "TTS"):
            factory = getattr(pocket_tts, name, None)
            if callable(factory):
                try:
                    return factory()
                except TypeError:
                    try:
                        return factory(self._language)
                    except TypeError:
                        continue
        raise TtsError(
            "pocket_tts API not recognized (no TTSModel.load_model / "
            "module factory with a known signature)",
            code="tts_model_error",
            fatal=False,
        )

    # ── generation ───────────────────────────────────────────────────

    def _start_generation(self, model: Any, text: str) -> Any:
        """Probe generate_audio_stream + the optional audio-prompt state.

        Returns an iterator/async-iterator of audio pieces, or None when
        no recognized call shape produced a stream.
        """
        # Optional voice state (audio-prompted voices) — probed, skipped
        # when the method is absent or its signature differs.
        state = None
        get_state = getattr(model, "get_state_for_audio_prompt", None)
        if callable(get_state):
            for args, kwargs in (
                ((), {}),
                ((self._voice,), {}),
                ((), {"voice": self._voice}),
                ((self._voice, self._language), {}),
            ):
                try:
                    state = get_state(*args, **kwargs)
                    break
                except TypeError:
                    continue
                except Exception as e:
                    logger.debug("get_state_for_audio_prompt failed: %s", e)
                    state = None
                    break

        gen = getattr(model, "generate_audio_stream", None)
        if callable(gen):
            calls = [
                ((), {"text": text}),
                ((text,), {}),
                ((text, state), {}) if state is not None else None,
                ((), {"text": text, "state": state}) if state is not None else None,
                ((), {"text": text, "voice": self._voice}),
                ((), {"text": text, "voice": self._voice, "language": self._language}),
                ((), {"text": text, "language": self._language}),
            ]
            for call in calls:
                if call is None:
                    continue
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
            logger.warning(
                "pocket_tts generate_audio_stream signature not recognized"
            )

        # Non-streaming fallback: a one-shot generate call.
        for name in ("generate_audio", "generate", "synthesize"):
            one_shot = getattr(model, name, None)
            if callable(one_shot):
                try:
                    return iter([one_shot(text)])
                except TypeError:
                    try:
                        return iter([one_shot(text, self._voice)])
                    except TypeError:
                        continue
        return None

    # ── audio conversion ─────────────────────────────────────────────

    def _to_pcm_chunks(self, piece: Any) -> list:
        """One generated audio piece → list of ≤100 ms s16le 24 kHz chunks.

        Handles numpy arrays (float/int, any amplitude), raw WAV bytes and
        raw PCM bytes. Numpy arrays are assumed to be at the engine's
        native output rate (24 kHz) unless the piece carries an explicit
        ``sample_rate``/``sampling_rate`` attribute.
        """
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
            src_rate = target_rate
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


# Sentinel distinguishing "iterator exhausted" from a falsy audio piece.
_SENTINEL = object()


def _next_or_none(iterator: Any) -> Any:
    """Advance a SYNC iterator off-thread; returns _SENTINEL when done."""
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
