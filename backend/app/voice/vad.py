"""Voice-activity detection.

Two interchangeable implementations behind :class:`VADProvider`:

- :class:`WebRtcVad` — the `webrtcvad` wheel (lazy import; mode from
  settings.VOICE_VAD_MODE; fixed 30 ms frames @16 kHz — the frame length
  exactness is asserted on every feed).
- :class:`EnergyVad` — a pure-numpy fallback: RMS level + adaptive noise
  floor + hangover.

Selection (:func:`create_vad`): webrtcvad when importable, else Energy.

VAD runs on the AEC-CLEANED signal only (see app/voice/aec.py) — never on
the raw mic. :class:`UtteranceTracker` turns per-frame decisions into
utterance events: speech onset (after `start_frames` speech frames), end
of utterance (`silence_ms` after the last speech frame, default 700 ms),
and a hard `max_utterance_ms` cap (default 30 s).
"""

from __future__ import annotations

import logging
from typing import List, Optional

import numpy as np

from app.config import settings
from app.voice.audio import pcm_to_int16, split_even_frames

logger = logging.getLogger(__name__)

# WebRTC VAD @16 kHz requires exactly 30 ms frames: 480 samples s16le.
VAD_FRAME_MS = 30
VAD_FRAME_SAMPLES = 480  # @16 kHz
VAD_FRAME_BYTES = 960  # s16le mono


# ── Provider interface ───────────────────────────────────────────────


class VADProvider:
    """Per-30 ms-frame speech decision."""

    name = "base"

    def is_speech(self, frame: bytes) -> bool:
        raise NotImplementedError

    def status(self) -> dict:
        return {"provider": self.name}


class WebRtcVad(VADProvider):
    """Google's WebRTC VAD via the `webrtcvad` wheel.

    The import is lazy so the module can load on machines without the
    wheel (EnergyVad is used instead — see create_vad()).
    """

    name = "webrtc"

    def __init__(self, mode: Optional[int] = None):
        try:
            import webrtcvad  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "webrtcvad is not installed — install the `webrtcvad-wheels` "
                "package or use EnergyVad"
            ) from e
        self._vad = webrtcvad.Vad()
        mode = settings.VOICE_VAD_MODE if mode is None else mode
        if not 0 <= mode <= 3:
            raise ValueError(f"VAD mode must be 0..3, got {mode}")
        self._vad.set_mode(mode)
        self._mode = mode

    def is_speech(self, frame: bytes) -> bool:
        # Frame-length exactness is a webrtcvad requirement — assert it.
        assert len(frame) == VAD_FRAME_BYTES, (
            f"WebRtcVad requires exactly 30ms frames @16kHz "
            f"({VAD_FRAME_BYTES} bytes), got {len(frame)}"
        )
        try:
            return self._vad.is_speech(frame, sample_rate=16000)
        except Exception:
            return False

    def status(self) -> dict:
        return {"provider": self.name, "mode": self._mode}


class EnergyVad(VADProvider):
    """RMS energy VAD with an adaptive noise floor + hangover.

    Pure numpy — always available, deterministic enough for tests and a
    reasonable fallback when the webrtcvad wheel is missing.

    - speech decision: RMS dBFS >= noise_floor_db + margin
    - noise floor: exponential moving average of the energy of frames
      currently classified as noise
    - hangover: keeps returning True for `hangover_frames` after the last
      speech frame (the UtteranceTracker adds its own end-of-utterance
      silence window; the hangover here only smooths single-frame dips)
    """

    name = "energy"

    def __init__(
        self,
        speech_margin_db: float = 12.0,
        floor_db: float = -60.0,
        ceiling_db: float = -20.0,
        hangover_frames: int = 2,
        adaptation: float = 0.05,
    ):
        self._margin = speech_margin_db
        self._hangover = hangover_frames
        self._adaptation = adaptation
        self._noise_floor_db = floor_db
        self._ceiling_db = ceiling_db
        self._since_speech = hangover_frames + 1
        # instant speech decision (before hangover), for introspection
        self._last_raw = False

    def _rms_db(self, frame: bytes) -> float:
        samples = pcm_to_int16(frame)
        if len(samples) == 0:
            return -96.0
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        if rms <= 0.0:
            return -96.0
        return 20.0 * np.log10(rms / 32768.0)

    def is_speech(self, frame: bytes) -> bool:
        energy = self._rms_db(frame)
        # Absolute gates keep the floor bounded in pathological silence
        # (pure digital zeros) and loud environments.
        floor = max(self._noise_floor_db, -75.0)
        speech = energy >= floor + self._margin or energy >= self._ceiling_db
        self._last_raw = speech

        if speech:
            self._since_speech = 0
            return True

        # Adapt the noise floor toward the observed noise energy.
        self._noise_floor_db += self._adaptation * (energy - self._noise_floor_db)
        if self._since_speech < self._hangover:
            self._since_speech += 1
            return True
        return False

    def status(self) -> dict:
        return {
            "provider": self.name,
            "noise_floor_db": round(self._noise_floor_db, 1),
            "margin_db": self._margin,
        }


def create_vad() -> VADProvider:
    """webrtcvad if importable, else the numpy EnergyVad."""
    try:
        import importlib.util

        if importlib.util.find_spec("webrtcvad") is not None:
            try:
                return WebRtcVad()
            except Exception as e:  # wheel present but broken
                logger.warning("webrtcvad unusable (%s) — using EnergyVad", e)
    except Exception:
        pass
    return EnergyVad()


# ── Utterance tracking ───────────────────────────────────────────────


class UtteranceTracker:
    """Turns per-30 ms VAD decisions into deterministic utterance events.

    Feed exact 30 ms frames (use :func:`feed_pcm` for arbitrary PCM — it
    buffers the remainder). Events are returned from the feed call:

    - "start"       — speech onset confirmed (after `start_frames` speech
                      frames within a window; pre-roll must be captured by
                      the caller BEFORE this point — it always is, because
                      the rolling buffer runs unconditionally)
    - "end"         — end of utterance (`silence_ms` after last speech)
    - "max_length"  — hard cap reached (force end)
    """

    def __init__(
        self,
        vad: VADProvider,
        silence_ms: Optional[int] = None,
        start_frames: int = 3,
        max_utterance_ms: Optional[int] = None,
    ):
        self._vad = vad
        self._silence_frames = max(
            1,
            (settings.VOICE_VAD_SILENCE_MS if silence_ms is None else silence_ms)
            // VAD_FRAME_MS,
        )
        self._start_frames = max(1, start_frames)
        if max_utterance_ms is None:
            max_utterance_ms = settings.VOICE_MAX_UTTERANCE_SEC * 1000
        self._max_utterance_frames = max(1, max_utterance_ms // VAD_FRAME_MS)
        self._in_speech = False
        self._utterance_active = False
        self._speech_run = 0  # consecutive speech frames (for onset)
        self._silence_run = 0  # consecutive silence frames (for end)
        self._utterance_frames = 0
        self._leftover = b""

    @property
    def in_speech(self) -> bool:
        """True while VAD currently sees speech (incl. hangover)."""
        return self._in_speech

    @property
    def utterance_active(self) -> bool:
        """True between 'start' and the corresponding 'end'/'max_length'."""
        return self._utterance_active

    def _vad_frame(self, frame: bytes) -> List[str]:
        events: List[str] = []
        speech = bool(self._vad.is_speech(frame))
        self._in_speech = speech

        if self._utterance_active:
            self._utterance_frames += 1

        if speech:
            self._speech_run += 1
            self._silence_run = 0
        else:
            self._silence_run += 1
            self._speech_run = 0

        if not self._utterance_active and self._speech_run >= self._start_frames:
            self._utterance_active = True
            self._utterance_frames = self._speech_run
            self._silence_run = 0
            events.append("start")

        if self._utterance_active:
            if self._utterance_frames >= self._max_utterance_frames:
                self._utterance_active = False
                self._speech_run = 0
                self._silence_run = 0
                events.append("max_length")
            elif self._silence_run >= self._silence_frames:
                self._utterance_active = False
                self._speech_run = 0
                self._silence_run = 0
                events.append("end")
        return events

    def feed(self, frame: bytes) -> List[str]:
        """Feed exactly one 30 ms frame; returns events."""
        if len(frame) != VAD_FRAME_BYTES:
            raise ValueError(
                f"UtteranceTracker requires exact {VAD_FRAME_MS}ms frames "
                f"({VAD_FRAME_BYTES} bytes), got {len(frame)}"
            )
        return self._vad_frame(frame)

    def feed_pcm(self, pcm: bytes) -> List[str]:
        """Feed arbitrary PCM; buffers the trailing partial 30 ms frame."""
        data = self._leftover + pcm
        frames, self._leftover = split_even_frames(data, VAD_FRAME_BYTES)
        events: List[str] = []
        for frame in frames:
            events.extend(self._vad_frame(frame))
        return events

    def force_end(self) -> bool:
        """Force the current utterance to end (e.g. stop request).

        Returns True when an utterance was active.
        """
        if self._utterance_active:
            self._utterance_active = False
            self._speech_run = 0
            self._silence_run = 0
            return True
        return False

    def status(self) -> dict:
        return {
            "vad": self._vad.status(),
            "in_speech": self._in_speech,
            "utterance_active": self._utterance_active,
        }
