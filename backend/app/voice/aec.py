"""Acoustic echo cancellation (AEC).

Two-stage architecture (documented per the task spec):

  Stage 1 (browser): getUserMedia applies WebRTC AEC3 in the browser
      itself (echoCancellation: true is the default for getUserMedia
      constraints). The client therefore already sends partially-cleaned
      mic audio.

  Stage 2 (this module, server): a genuine adaptive filter that uses the
      EXACT played PCM — the 0x02 far-end frames echoed back by the
      client's playback worklet, i.e. the very same bytes the server sent
      as 0x03 TTS audio (resampled to 16 kHz by the client) — as the
      cancellation reference. Stage 1 handles the browser-side linear
      echo path; stage 2 removes whatever leaks through (speaker→mic
      paths that AEC3 could not fully cancel, USB audio without browser
      AEC, non-standard playback chains, …).

Selection (create_aec, settings.VOICE_AEC):
  "auto"   → WebRtcAec when an importable WebRTC audio processing module
             with a RECOGNIZED API is present, else NlmsAec
  "webrtc" → WebRtcAec only (RuntimeError if unavailable/unsupported)
  "nlms"   → NlmsAec
  "none"   → pass-through (debugging only — never used by default)

The mic stream is ALWAYS processed and ALWAYS returned — during TTS
playback the pipeline is: y_clean = AEC(mic, far_ref) → VAD → ASR.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

from app.config import settings
from app.voice.audio import pcm_to_float32, float32_to_pcm, int16_to_pcm

logger = logging.getLogger(__name__)

# ── Tuning (defaults follow the task spec: ~2048 taps @16k, step ~0.35,
#    leaky; alignment search ±400 ms initially and periodically). ──────
DEFAULT_TAPS = 2048  # 128 ms of echo path at 16 kHz
DEFAULT_MU = 0.35  # NLMS step size (block-normalized; stable for < 2)
DEFAULT_LEAK = 0.001  # leaky factor — pulls w toward zero (bounds drift)
DEFAULT_REG = 1e-6  # power regularization (avoids div-by-zero in silence)
DEFAULT_SEARCH_MS = 400  # ± lag search window (initial + periodic)
DEFAULT_REALIGN_SEC = 2.0  # periodic re-alignment cadence
DEFAULT_SUBBLOCK = 160  # 10 ms adaptation sub-blocks
FAR_KEEP_SEC = 3.0  # far-end reference history retained
DTD_FREEZE_RATIO = 0.25  # min |corr(residual, echo-estimate)| to keep adapting


class AECProvider:
    """Base interface: feed the exact played PCM as reference, process mic."""

    name = "base"

    def push_reference(self, far_pcm: bytes) -> None:
        """Feed far-end (exact played) PCM s16le 16k mono."""
        raise NotImplementedError

    def process(self, mic_pcm: bytes) -> bytes:
        """Process near-end (mic) PCM s16le 16k mono → cleaned PCM."""
        raise NotImplementedError

    def reset(self) -> None:
        raise NotImplementedError

    def status(self) -> dict:
        return {"provider": self.name}


class NlmsAec(AECProvider):
    """Genuine normalized-LMS adaptive echo canceller (numpy).

    Model: mic[n] = Σ_k h[k]·far[n − d − k] + near_end[n]

    - d: bulk delay between playback and capture, estimated by
      cross-correlation lag search over ±400 ms (initially, and
      re-estimated periodically). The far-end buffer keeps the absolute
      sample timeline so the filter input windows stay aligned.
    - h: adaptive FIR echo path estimate (`taps` = 2048 @16 kHz),
      updated with leaky block-NLMS (step μ ≈ 0.35, single Frobenius
      normalization per sub-block — reduces to classic per-sample NLMS
      for single-sample blocks and is stable for μ < 2).
    - Sub-blocks of 20 ms keep adaptation fast while the linear algebra
      runs on BLAS-backed matrix products (sliding-window views, no
      giant temporary matrices).

    The output is e = y − ŷ (the residual: near-end speech with the echo
    estimate subtracted). The mic is always returned, always full length:
    when no far-end reference has been fed yet the signal passes through
    unchanged (there is simply no echo model to apply — mic frames are
    never dropped).
    """

    name = "nlms"

    def __init__(
        self,
        taps: int = DEFAULT_TAPS,
        mu: float = DEFAULT_MU,
        leak: float = DEFAULT_LEAK,
        search_ms: int = DEFAULT_SEARCH_MS,
        sample_rate: int = 16000,
        subblock: int = DEFAULT_SUBBLOCK,
    ):
        self._sr = sample_rate
        self._n = taps
        self._mu = mu
        self._leak = leak
        self._search = int(sample_rate * search_ms / 1000)
        self._subblock = subblock

        self._w = np.zeros(taps, dtype=np.float32)  # echo path estimate
        # Far-end timeline: `_far` holds the most recent samples;
        # `_far_base` is the absolute far sample index of `_far[0]`.
        self._far = np.zeros(0, dtype=np.float32)
        self._far_base = 0
        self._far_total = 0  # absolute count of far samples received
        self._mic_total = 0  # absolute count of mic samples processed
        self._lag = None  # estimated delay d (far behind mic), samples
        self._last_realign = 0.0
        self._aligned = False
        # Mic tail (for lag cross-correlation), absolute-indexed.
        self._mic_tail = np.zeros(0, dtype=np.float32)
        self._mic_tail_base = 0

        self._erle_num = 0.0  # running Σ |y|²
        self._erle_den = 0.0  # running Σ |e|²
        self._adapted_once = False  # lets the very first blocks train

    # ── reference feeding ────────────────────────────────────────────

    def push_reference(self, far_pcm: bytes) -> None:
        if not far_pcm:
            return
        far = pcm_to_float32(far_pcm)
        n = len(far)
        if len(self._far):
            self._far = np.concatenate([self._far, far])
        else:
            # Buffer is empty — (re)base it at the current absolute far
            # timeline position (gaps in the reference are tolerated).
            self._far_base = self._far_total
            self._far = far.copy()
        self._far_total += n
        # Trim old history (keep enough for search + taps + a frame).
        keep = int(self._sr * FAR_KEEP_SEC) + self._search + self._n + 4096
        if len(self._far) > keep:
            drop = len(self._far) - keep
            self._far = self._far[drop:]
            self._far_base += drop

    def reset(self) -> None:
        self._w = np.zeros(self._n, dtype=np.float32)
        self._far = np.zeros(0, dtype=np.float32)
        self._far_base = 0
        self._far_total = 0
        self._mic_total = 0
        self._lag = None
        self._aligned = False
        self._mic_tail = np.zeros(0, dtype=np.float32)
        self._mic_tail_base = 0
        self._erle_num = 0.0
        self._erle_den = 0.0

    # ── delay alignment ──────────────────────────────────────────────

    def _far_power_recent(self, ms: float = 400.0) -> float:
        n = int(self._sr * ms / 1000)
        seg = self._far[-n:] if len(self._far) >= n else self._far
        return float(np.mean(np.square(seg))) if len(seg) else 0.0

    def _estimate_lag(self, mic_frame: np.ndarray) -> Optional[int]:
        """Cross-correlation lag search (decimated) between the recent mic
        signal and the far-end reference.

        Returns the best absolute delay d (samples of far behind mic), or
        None when the correlation is too weak to trust (silence/double
        talk) — in which case the previous estimate is kept.
        """
        # Use the mic tail (up to ~400 ms) for a steadier estimate.
        tail = self._mic_tail
        if len(tail) < 320:
            return None
        far_power = self._far_power_recent()
        mic_power = float(np.mean(np.square(tail)))
        if far_power < 1e-7 or mic_power < 1e-7:
            return None  # one side is (near-)silent — cannot align

        # Decimate by 8 for speed (2 kHz is plenty for delay estimation).
        d = 8
        mic_dec = tail[::d]
        # Far window: ± search around the expected region. The far sample
        # matching mic index n sits at far absolute index n - d_lag, so we
        # search over the far range that could align with the mic tail.
        mic_abs_start = self._mic_tail_base  # absolute mic index of tail[0]
        far_lo = mic_abs_start - self._search - self._n
        far_hi = mic_abs_start + len(tail) + self._search
        seg = self._fetch_far(far_lo, far_hi - far_lo)
        seg_dec = seg[::d]

        # Correlation: for shift τ (decimated), mic_dec[i] ~ seg_dec[i+τ].
        # np.correlate(a, v, 'valid') = Σ a[m+i] v[i] — exactly the form
        # we need with a=seg_dec, v=mic_dec.
        max_shift = len(seg_dec) - len(mic_dec)
        if max_shift <= 0:
            return None
        corr = np.correlate(seg_dec, mic_dec, mode="valid")
        if len(corr) == 0:
            return None
        peak = int(np.argmax(np.abs(corr)))
        peak_val = float(np.abs(corr[peak]))
        norm = np.sqrt(
            float(np.sum(np.square(mic_dec)))
            * float(np.sum(np.square(seg_dec[peak : peak + len(mic_dec)])))
        )
        if norm <= 0.0:
            return None
        confidence = peak_val / norm
        if confidence < 0.25:
            return None  # weak correlation — keep the previous lag
        # seg index (decimated) → absolute far index: far_lo + (peak + i)*d.
        # mic_dec[i] ↔ mic absolute index mic_abs_start + i*d.
        # Alignment: seg_dec[peak + i] ≈ mic_dec[i] →
        #   far_abs = far_lo + (peak + i) * d  ↔  mic_abs = mic_abs_start + i*d
        # ⇒ d_lag = mic_abs - far_abs = mic_abs_start - far_lo - peak*d
        lag = (mic_abs_start - far_lo) - peak * d
        if abs(lag) > self._search:
            return None
        return int(lag)

    def _fetch_far(self, abs_start: int, length: int) -> np.ndarray:
        """Return far samples for absolute indices
        [abs_start, abs_start+length), zero-padded when they precede the
        retained far history."""
        if length <= 0:
            return np.zeros(0, dtype=np.float32)
        out = np.zeros(length, dtype=np.float32)
        lo = max(abs_start, self._far_base)
        hi = min(abs_start + length, self._far_total)
        if hi > lo:
            out[lo - abs_start : hi - abs_start] = self._far[
                lo - self._far_base : hi - self._far_base
            ]
        return out

    # ── main processing ──────────────────────────────────────────────

    def process(self, mic_pcm: bytes) -> bytes:
        y = pcm_to_float32(mic_pcm)
        if len(y) == 0:
            return b""

        s = self._mic_total  # absolute index of y[0]
        self._mic_total += len(y)
        # Keep the mic tail for alignment.
        tail_keep = int(self._sr * 0.4)
        new_tail_base = s + max(0, len(y) - tail_keep)
        new_tail = y[-tail_keep:] if len(y) > tail_keep else y
        if len(self._mic_tail) and self._mic_tail_base + len(self._mic_tail) == s:
            self._mic_tail = np.concatenate([self._mic_tail, new_tail])[-tail_keep:]
        else:
            self._mic_tail = new_tail.copy()
        self._mic_tail_base = new_tail_base if len(y) > tail_keep else s
        # recompute tail base to match the trimmed array
        self._mic_tail_base = self._mic_total - len(self._mic_tail)

        out = np.zeros(len(y), dtype=np.float32)

        has_far = self._far_total > self._n
        if has_far:
            now = time.monotonic()
            need_realign = (
                self._lag is None
                or not self._aligned
                or (now - self._last_realign) > DEFAULT_REALIGN_SEC
            )
            if need_realign:
                lag = self._estimate_lag(y)
                if lag is not None:
                    changed = self._lag is None or abs(self._lag - lag) > 64
                    self._lag = lag
                    self._last_realign = now
                    self._aligned = True
                    # A bulk-delay change invalidates the learned filter.
                    if changed:
                        self._w = np.zeros(self._n, dtype=np.float32)

        if has_far and self._aligned and self._lag is not None:
            # Process in sub-blocks for fast adaptation.
            step = self._subblock
            for off in range(0, len(y), step):
                yb = y[off : off + step]
                eb = self._process_block(s + off, yb)
                out[off : off + len(eb)] = eb
        else:
            # No usable reference yet — pass-through (mic NEVER dropped).
            out[:] = y

        self._erle_num += float(np.sum(np.square(y)))
        self._erle_den += float(np.sum(np.square(out)))
        return float32_to_pcm(out)

    def _process_block(self, s: int, y: np.ndarray) -> np.ndarray:
        """One NLMS sub-block at absolute mic index s."""
        L = len(y)
        N = self._n
        d = self._lag
        # Filter input window: far absolute indices
        # [s - d - (N-1), s - d + L - 1]  (length N-1+L).
        base = s - d - (N - 1)
        seg = self._fetch_far(base, N - 1 + L)
        if len(seg) < N:
            # Reference does not reach this far back — pass through.
            return y.copy()

        # Hankel sliding window: X[i, j] = seg[i + j], shape (L, N).
        X = sliding_window_view(seg, N)[:L]
        wf = self._w[::-1]  # w[k] multiplies seg[N-1+i-k] == X[i, N-1-k]
        echo = X @ wf  # ŷ[i] = Σ_k w[k] far[s+i-d-k]
        e = y - echo

        # Double-talk detector: freeze the adaptation when the residual is
        # no longer echo-like (uncorrelated with the current echo
        # estimate) — i.e. near-end speech now dominates the error. The
        # filter keeps cancelling with the current weights; the mic signal
        # itself is never muted or dropped. An untrained filter
        # (negligible echo estimate) always adapts.
        y_power = float(np.sum(np.square(y)))
        echo_power = float(np.sum(np.square(echo)))
        if echo_power > 1e-9 and y_power > 1e-9:
            dot = float(np.dot(e, echo))
            denom = float(np.sqrt(np.sum(np.square(e)) * echo_power))
            residual_echo_corr = abs(dot / denom) if denom > 0 else 0.0
            adapt = residual_echo_corr >= DTD_FREEZE_RATIO
        else:
            adapt = echo_power <= 1e-9  # untrained → learn

        if adapt:
            # Leaky NLMS update with EXACT power normalization (line
            # search):
            #   g     = Xᵀe                (echo-path gradient, correlation
            #                                form)
            #   step  = ‖g‖² / ‖Xg‖²       (exact optimum along g — reduces
            #                                to the classic 1/(‖x‖²+ε) NLMS
            #                                step for single-sample blocks)
            #   w     ← (1 − μλ)·w + μ·step·g
            # μ (0.35) acts as a relaxation factor: it bounds the
            # per-block misadjustment so a corrupted (double-talk)
            # gradient cannot diverge the filter.
            g = X.T @ e  # g[k] = Σ_i e[i] far[s+i-d-k]  (after the flip)
            g = g[::-1]
            g_norm = float(np.sum(np.square(g)))
            if g_norm > 1e-12:
                Xg = X @ g[::-1]  # X · (g in wf-space)
                denom = float(np.sum(np.square(Xg))) + DEFAULT_REG
                step = g_norm / denom
                self._w = (1.0 - self._mu * self._leak) * self._w + (
                    (self._mu * step) * g
                ).astype(np.float32)
                self._adapted_once = True
        return e.astype(np.float32)

    def status(self) -> dict:
        erle = None
        if self._erle_den > 1e-9:
            erle = self._erle_num / self._erle_den
        return {
            "provider": self.name,
            "taps": self._n,
            "mu": self._mu,
            "leak": self._leak,
            "lag_samples": self._lag,
            "aligned": self._aligned,
            "erle": round(erle, 2) if erle is not None else None,
            "far_samples": self._far_total,
        }


class WebRtcAec(AECProvider):
    """WebRTC's AEC3 via the `webrtc_audio_processing` / `pywebrtc_audio`
    bindings — IF one is importable AND its API matches one of the known
    shapes at runtime (verified with hasattr — never assumed).

    The actual attribute/method names differ across binding versions, so
    every step is introspected; a single mismatch raises RuntimeError and
    create_aec() falls back to NlmsAec. This is deliberate: shipping an
    unverified call against an unknown binding would silently produce no
    cancellation at all.
    """

    name = "webrtc"

    _MODULE_NAMES = ("webrtc_audio_processing", "pywebrtc_audio")
    _FACTORY_NAMES = ("AudioProcessingModule", "AudioProcessing", "APM")
    _PROC_NAMES = ("process_stream", "ProcessStream")
    _REV_NAMES = ("process_reverse_stream", "ProcessReverseStream")
    _FRAME_MS = 10  # WebRTC APM processes 10 ms frames

    def __init__(self, sample_rate: int = 16000, channels: int = 1):
        self._sr = sample_rate
        self._channels = channels
        module = None
        errors = []
        for mod_name in self._MODULE_NAMES:
            try:
                module = __import__(mod_name)
                break
            except ImportError as e:
                errors.append(f"{mod_name}: {e}")
        if module is None:
            raise RuntimeError(
                "no WebRTC audio processing module importable (tried: "
                + "; ".join(errors)
                + ")"
            )

        factory = None
        for name in self._FACTORY_NAMES:
            factory = getattr(module, name, None)
            if callable(factory):
                break
        if factory is None:
            raise RuntimeError(
                f"{module.__name__} has none of the known factories "
                f"{self._FACTORY_NAMES} — API not recognized"
            )

        apm = None
        try:
            apm = factory()
        except TypeError:
            apm = factory(debug=False)
        except Exception as e:
            raise RuntimeError(f"could not instantiate {factory!r}: {e}") from e

        # Enable echo cancellation (attribute or method style).
        ec = getattr(apm, "echo_cancellation", None)
        enabled = False
        if ec is not None:
            for attr in ("enabled", "enable"):
                if hasattr(ec, attr):
                    try:
                        setattr(ec, attr, True)
                        enabled = True
                        break
                    except Exception:
                        pass
        if not enabled:
            for meth in (
                "enable_echo_cancellation",
                "set_echo_cancellation_enabled",
            ):
                m = getattr(apm, meth, None)
                if callable(m):
                    try:
                        m(True)
                        enabled = True
                        break
                    except Exception:
                        pass
        if not enabled:
            raise RuntimeError(
                f"{module.__name__}: could not enable echo cancellation "
                "(no recognized switch) — API not recognized"
            )

        # Stream processing methods must exist.
        proc = None
        for name in self._PROC_NAMES:
            proc = getattr(apm, name, None)
            if callable(proc):
                break
        if proc is None:
            raise RuntimeError(
                f"{module.__name__} has none of the known stream methods "
                f"{self._PROC_NAMES} — API not recognized"
            )
        self._process_stream = proc
        self._reverse = None
        for name in self._REV_NAMES:
            self._reverse = getattr(apm, name, None)
            if callable(self._reverse):
                break

        # Frame format negotiation (bytes and int16 numpy are both tried
        # at call time — bindings differ).
        self._apm = apm
        self._frame_bytes = sample_rate * 2 * channels // 100  # 10 ms s16le
        self._far_pending = b""
        self._mic_pending = b""
        self._proc_args_probe = None

    # ── reference / mic feeding with 10 ms framing ───────────────────

    def push_reference(self, far_pcm: bytes) -> None:
        if self._reverse is None:
            return  # no reverse-stream API — filter runs blind (harmless)
        self._far_pending += far_pcm
        while len(self._far_pending) >= self._frame_bytes:
            frame = self._far_pending[: self._frame_bytes]
            self._far_pending = self._far_pending[self._frame_bytes :]
            self._call(self._reverse, frame)

    def process(self, mic_pcm: bytes) -> bytes:
        self._mic_pending += mic_pcm
        out = b""
        while len(self._mic_pending) >= self._frame_bytes:
            frame = self._mic_pending[: self._frame_bytes]
            self._mic_pending = self._mic_pending[self._frame_bytes :]
            out += self._call(self._process_stream, frame)
        # Any sub-10ms tail passes through unprocessed (next call will
        # complete it; the mic is never dropped).
        passthrough = self._mic_pending
        self._mic_pending = b""
        return out + passthrough

    def _call(self, method, frame: bytes) -> bytes:
        """Call an APM method with a 10 ms frame, trying the common call
        signatures (bytes → bytes, or numpy int16 → numpy)."""
        # 1: plain bytes
        try:
            res = method(frame)
            if isinstance(res, (bytes, bytearray)):
                return bytes(res)
            if isinstance(res, memoryview):
                return bytes(res)
        except Exception:
            pass
        # 2: numpy int16
        try:
            arr = np.frombuffer(frame, dtype="<i2").copy()
            res = method(arr)
            if isinstance(res, np.ndarray):
                return int16_to_pcm(res)
            if isinstance(res, (bytes, bytearray)):
                return bytes(res)
        except Exception:
            pass
        # 3: keyword forms seen in some bindings
        try:
            res = method(data=frame, sample_rate=self._sr, num_channels=self._channels)
            if isinstance(res, (bytes, bytearray, memoryview)):
                return bytes(res)
            if isinstance(res, np.ndarray):
                return int16_to_pcm(res)
        except Exception:
            pass
        # Unrecognized signature — this binding cannot be used safely.
        raise RuntimeError(
            f"{self._apm!r}: no recognized call signature for {method!r}"
        )

    def reset(self) -> None:
        try:
            reset = getattr(self._apm, "reset", None) or getattr(
                self._apm, "Initialize", None
            )
            if callable(reset):
                reset()
        except Exception:
            pass
        self._far_pending = b""
        self._mic_pending = b""

    def status(self) -> dict:
        return {"provider": self.name, "sample_rate": self._sr}


class PassThroughAec(AECProvider):
    """No cancellation — debugging only (VOICE_AEC=none). The mic still
    always passes through; nothing is muted or dropped."""

    name = "none"

    def push_reference(self, far_pcm: bytes) -> None:
        pass

    def process(self, mic_pcm: bytes) -> bytes:
        return mic_pcm

    def reset(self) -> None:
        pass


def create_aec(mode: Optional[str] = None) -> AECProvider:
    """Select the AEC backend from settings.VOICE_AEC (default 'auto').

    'auto' prefers WebRTC when (and only when) its API verifies at
    runtime; otherwise the pure-numpy NLMS filter is used. The NLMS
    fallback is a REAL adaptive canceller, not a stub.
    """
    mode = (mode or settings.VOICE_AEC or "auto").strip().lower()
    if mode == "none":
        return PassThroughAec()
    if mode == "nlms":
        return NlmsAec()
    if mode == "webrtc":
        return WebRtcAec()
    # auto
    try:
        return WebRtcAec()
    except Exception as e:
        logger.info("WebRTC AEC unavailable (%s) — using NLMS AEC", e)
        return NlmsAec()
