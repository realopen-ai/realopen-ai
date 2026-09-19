"""Audio plumbing: PCM conversion helpers + the rolling pre-roll ring buffer.

All PCM in the voice pipeline is s16le mono. Input (mic + far-end AEC
reference) is 16 kHz; TTS output is 24 kHz. These formats are fixed by
voice protocol v1 (see app/voice/__init__.py).

The :class:`RollingBuffer` always retains the last N milliseconds of the
cleaned mic signal — even while the assistant is speaking — so that when
VAD detects a barge-in speech onset, the first syllables that arrived
BEFORE the trigger are still available (spec §10: pre-roll 100-300 ms).
"""

from __future__ import annotations

from typing import List

import numpy as np

SAMPLES_PER_MS_16K = 16  # 16000 samples/sec ÷ 1000


# ── PCM conversion ───────────────────────────────────────────────────


def pcm_to_float32(pcm: bytes) -> np.ndarray:
    """Convert s16le mono PCM bytes → float32 numpy array in [-1, 1]."""
    if not pcm:
        return np.zeros(0, dtype=np.float32)
    ints = np.frombuffer(pcm, dtype="<i2")
    return ints.astype(np.float32) / 32768.0


def float32_to_pcm(samples: np.ndarray) -> bytes:
    """Convert float32 array (any range) → s16le mono PCM bytes (clipped)."""
    if samples is None or len(samples) == 0:
        return b""
    clipped = np.clip(samples, -1.0, 1.0)
    return (clipped * 32767.0).astype("<i2").tobytes()


def pcm_to_int16(pcm: bytes) -> np.ndarray:
    """Interpret s16le PCM bytes as an int16 numpy view (no copy when
    the buffer is already aligned)."""
    if not pcm:
        return np.zeros(0, dtype=np.int16)
    return np.frombuffer(pcm, dtype="<i2")


def int16_to_pcm(samples: np.ndarray) -> bytes:
    """int16 numpy array → s16le PCM bytes."""
    if samples is None or len(samples) == 0:
        return b""
    return np.asarray(samples, dtype="<i2").tobytes()


def resample_linear(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Resample a 1-D signal with linear interpolation (numpy-only).

    Accurate enough for speech (ASR front-ends resample more precisely
    themselves); avoids a hard scipy dependency.
    """
    if src_rate == dst_rate or len(samples) == 0:
        return samples
    n_src = len(samples)
    n_dst = max(1, int(round(n_src * dst_rate / src_rate)))
    # Map destination sample positions into source sample space.
    pos = np.arange(n_dst) * (n_src / n_dst)
    idx0 = np.floor(pos).astype(np.int64)
    idx0 = np.clip(idx0, 0, max(0, n_src - 1))
    idx1 = np.clip(idx0 + 1, 0, max(0, n_src - 1))
    frac = (pos - idx0).astype(samples.dtype)
    a = samples[idx0]
    b = samples[idx1]
    return (a * (1 - frac) + b * frac).astype(samples.dtype)


def pcm_energy_db(pcm: bytes) -> float:
    """RMS energy of an s16le PCM chunk in dBFS (−inf for silence)."""
    samples = pcm_to_int16(pcm)
    if len(samples) == 0:
        return -96.0
    rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
    if rms <= 0.0:
        return -96.0
    return 20.0 * np.log10(rms / 32768.0)


# ── Rolling pre-roll buffer ──────────────────────────────────────────


class RollingBuffer:
    """Fixed-capacity rolling buffer of s16le PCM bytes.

    The voice session feeds EVERY cleaned mic frame into this buffer (the
    mic is never muted while the assistant speaks). When a barge-in speech
    onset is detected, the session calls :meth:`snapshot` to seed the new
    utterance with the retained pre-roll, so the first syllables spoken
    before the VAD trigger are not lost.
    """

    def __init__(self, capacity_ms: int, sample_rate: int = 16000):
        if capacity_ms < 0:
            raise ValueError("capacity_ms must be >= 0")
        self._capacity_bytes = (sample_rate // 1000) * capacity_ms * 2  # s16le
        self._sample_rate = sample_rate
        self._chunks: List[bytes] = []
        self._total = 0  # bytes currently stored

    @property
    def capacity_bytes(self) -> int:
        return self._capacity_bytes

    def push(self, pcm: bytes) -> None:
        """Append PCM; drops the oldest data once capacity is exceeded."""
        if not pcm:
            return
        # Zero capacity keeps nothing (an empty ring buffer).
        if self._capacity_bytes <= 0:
            return
        # Reject frames that alone exceed capacity (keep the tail).
        if len(pcm) >= self._capacity_bytes:
            self._chunks = [pcm[-self._capacity_bytes :]]
            self._total = len(self._chunks[0])
            return
        self._chunks.append(pcm)
        self._total += len(pcm)
        while self._total > self._capacity_bytes and self._chunks:
            drop = self._total - self._capacity_bytes
            first = self._chunks[0]
            if drop >= len(first):
                self._total -= len(first)
                self._chunks.pop(0)
            else:
                self._chunks[0] = first[drop:]
                self._total -= drop

    def snapshot(self) -> bytes:
        """Return the currently retained audio (oldest → newest)."""
        return b"".join(self._chunks)

    def clear(self) -> None:
        self._chunks = []
        self._total = 0


def chunk_pcm(pcm: bytes, chunk_bytes: int) -> List[bytes]:
    """Split PCM bytes into fixed-size chunks (last may be short)."""
    if chunk_bytes <= 0:
        raise ValueError("chunk_bytes must be positive")
    if len(pcm) <= chunk_bytes:
        return [pcm] if pcm else []
    return [pcm[i : i + chunk_bytes] for i in range(0, len(pcm), chunk_bytes)]


def split_even_frames(pcm: bytes, frame_bytes: int) -> List[bytes]:
    """Split PCM into whole frames of frame_bytes; returns (frames, leftover).

    Unlike :func:`chunk_pcm`, any trailing partial frame is returned
    separately so the caller can carry it into the next call — VAD and AEC
    need exact frame lengths.
    """
    if frame_bytes <= 0:
        raise ValueError("frame_bytes must be positive")
    usable = (len(pcm) // frame_bytes) * frame_bytes
    frames = [pcm[i : i + frame_bytes] for i in range(0, usable, frame_bytes)]
    leftover = pcm[usable:]
    return frames, leftover


def samples_to_ms(n_samples: int, sample_rate: int = 16000) -> float:
    return n_samples * 1000.0 / sample_rate


def ms_to_samples(ms: float, sample_rate: int = 16000) -> int:
    return int(sample_rate * ms / 1000.0)
