"""
Tests for app/voice/aec.py — the acoustic echo cancellation stack.

numpy IS available in this environment, so the module is exercised for
real (the NlmsAec adaptive filter runs its actual numpy math on synthetic
signals — no audio devices, no model runtimes).

Scope:
  • NlmsAec beyond the existing test_voice_unit.py coverage: far-history
    bookkeeping (push_reference trimming, _fetch_far zero-padding),
    delay alignment branches (_estimate_lag: short tail, silent sides,
    weak correlation, out-of-window lag, degenerate zero-shift window,
    the empty-correlation guard, success), periodic re-alignment
    with the bulk-delay-change filter reset, the keep-previous-lag path,
    double-talk / silent-mic adaptation freezes, ERLE status edges, the
    defensive _process_block short-reference branch.
  • WebRtcAec against a FAKE webrtc_audio_processing module injected
    into sys.modules (API verification: factory probing, echo-cancellation
    enabling attr/method styles, 10 ms framing of push_reference/process,
    the three _call signatures, unrecognized-signature failure, reset,
    every constructor rejection path).
  • create_aec mode selection (none / nlms / webrtc / auto with and
    without an importable verified WebRTC binding).

No network, no DB, no Ollama, no audio hardware. All synthetic PCM is
generated with numpy in-memory.
"""

import sys
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.voice.aec import (  # noqa: E402
    AECProvider,
    NlmsAec,
    PassThroughAec,
    WebRtcAec,
    create_aec,
)

SR = 16000


# ── synthetic signals ─────────────────────────────────────────────────


def _to_pcm(x: np.ndarray) -> bytes:
    return (np.clip(x, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


def _colored_noise(n: int, seed: int = 42) -> np.ndarray:
    rng = np.random.default_rng(seed)
    far = np.convolve(
        rng.standard_normal(n + 15), np.ones(16) / 16.0, mode="valid"
    ).astype(np.float32)
    assert len(far) == n
    far /= max(1e-9, float(np.max(np.abs(far)))) / 0.5
    return far


def _echo_fixture(seconds: float = 1.0, delay: int = 3000, seed: int = 7):
    """far signal + mic = the same signal delayed by `delay` samples."""
    n = int(SR * seconds)
    far = _colored_noise(n, seed)
    mic = np.zeros(n, dtype=np.float32)
    mic[delay:] = far[: n - delay]
    return far, mic


def _run_frames(aec: NlmsAec, far, mic, frames, offset=0, record=None):
    """push_reference + process frame-by-frame like the WS pipeline."""
    frame = 160  # 10 ms
    outs = []
    for i in range(frames):
        s = (offset + i) * frame
        aec.push_reference(_to_pcm(far[s : s + frame]))
        out = aec.process(_to_pcm(mic[s : s + frame]))
        outs.append(out)
        if record is not None:
            record.append(aec)
    return outs


# ── NlmsAec: far-history bookkeeping ──────────────────────────────────


class TestFarHistory:
    def test_push_reference_empty_is_noop(self):
        aec = NlmsAec()
        aec.push_reference(b"")
        assert aec._far_total == 0
        assert len(aec._far) == 0

    def test_fetch_far_zero_pads_around_history(self):
        aec = NlmsAec()
        far = _colored_noise(1000)
        aec.push_reference(_to_pcm(far))
        assert aec._far_base == 0
        assert aec._far_total == 1000
        # window starting before history → zeros in front
        seg = aec._fetch_far(-100, 500)
        assert len(seg) == 500
        assert np.all(seg[:100] == 0)
        assert np.array_equal(seg[100:500], aec._far[:400])
        # window extending past the end → zeros at the back
        seg = aec._fetch_far(900, 500)
        assert np.array_equal(seg[:100], aec._far[900:1000])
        assert np.all(seg[100:] == 0)
        # non-positive length → empty
        assert len(aec._fetch_far(0, 0)) == 0
        assert len(aec._fetch_far(500, -3)) == 0

    def test_push_reference_trims_old_history(self):
        aec = NlmsAec()
        keep = int(SR * 3.0) + aec._search + aec._n + 4096
        n = keep + 20000  # well beyond the retention window
        far = _colored_noise(n, seed=11)
        # feed in 100 ms chunks like the real pipeline
        for s in range(0, n, 1600):
            aec.push_reference(_to_pcm(far[s : s + 1600]))
        assert aec._far_total == n
        assert len(aec._far) <= keep
        # the absolute timeline is preserved after the trim
        assert aec._far_base == n - len(aec._far)
        # and old samples are still fetchable through the padding
        seg = aec._fetch_far(0, 100)
        assert np.all(seg == 0)  # everything that old was dropped

    def test_push_reference_after_gap_rebases(self):
        aec = NlmsAec()
        aec.push_reference(_to_pcm(_colored_noise(1600)))
        first_base = aec._far_base
        aec.push_reference(_to_pcm(_colored_noise(1600, seed=3)))
        # contiguous push → concatenated, no rebase
        assert aec._far_base == first_base
        assert len(aec._far) == 3200

    def test_far_power_recent(self):
        aec = NlmsAec()
        assert aec._far_power_recent() == 0.0  # nothing pushed
        aec.push_reference(_to_pcm(_colored_noise(1600)))
        assert aec._far_power_recent() > 1e-7
        # silent far → ~zero power
        aec2 = NlmsAec()
        aec2.push_reference(_to_pcm(np.zeros(1600, dtype=np.float32)))
        assert aec2._far_power_recent() < 1e-7


# ── NlmsAec: alignment ────────────────────────────────────────────────


class TestAlignment:
    def test_estimate_lag_short_tail(self):
        aec = NlmsAec()
        aec._mic_tail = _colored_noise(100)  # < 320 samples
        aec._mic_tail_base = 0
        assert aec._estimate_lag(np.zeros(160, dtype=np.float32)) is None

    def test_estimate_lag_silent_far(self):
        aec = NlmsAec()
        aec.push_reference(_to_pcm(np.zeros(3200, dtype=np.float32)))
        aec._mic_tail = _colored_noise(1600)
        aec._mic_tail_base = 0
        assert aec._estimate_lag(np.zeros(160, dtype=np.float32)) is None

    def test_estimate_lag_silent_mic(self):
        aec = NlmsAec()
        aec.push_reference(_to_pcm(_colored_noise(3200)))
        aec._mic_tail = np.zeros(1600, dtype=np.float32)
        aec._mic_tail_base = 0
        assert aec._estimate_lag(np.zeros(160, dtype=np.float32)) is None

    def test_estimate_lag_weak_correlation(self):
        aec = NlmsAec()
        # active far (colored) vs an UNRELATED white-noise mic tail —
        # different shape AND different seed → correlation below trust
        aec.push_reference(_to_pcm(_colored_noise(6400, seed=1)))
        rng = np.random.default_rng(99)
        aec._mic_tail = (rng.standard_normal(1600) * 0.3).astype(np.float32)
        aec._mic_tail_base = 0
        assert aec._estimate_lag(np.zeros(160, dtype=np.float32)) is None

    def test_estimate_lag_zero_norm(self):
        """Tail energy concentrated OFF the decimation grid → the
        correlated subsample is silent → norm 0 → no estimate."""
        aec = NlmsAec()
        aec.push_reference(_to_pcm(_colored_noise(6400)))
        tail = np.zeros(1600, dtype=np.float32)
        tail[np.arange(1600) % 8 != 0] = 0.3  # energy on non-decimated taps
        aec._mic_tail = tail
        aec._mic_tail_base = 0
        assert aec._estimate_lag(np.zeros(160, dtype=np.float32)) is None

    def test_estimate_lag_outside_search_window(self):
        """A strong correlation whose implied delay exceeds ±search is
        rejected (search_ms=10 → ±160 samples)."""
        aec = NlmsAec(search_ms=10)
        n = 4096
        # pulse at the very START of the far history; the mic tail is the
        # same pulse → correlation peaks at seg offset ~0, which maps to
        # lag = search + taps (2208) — far outside the ±160 window.
        pulse = np.zeros(n, dtype=np.float32)
        pulse[100:200] = 0.5
        aec.push_reference(_to_pcm(pulse))
        aec._mic_tail = pulse.copy()
        aec._mic_tail_base = 2208  # far_lo = 2208 - 160 - 2048 = 0
        assert aec._estimate_lag(np.zeros(160, dtype=np.float32)) is None

    def test_estimate_lag_degenerate_zero_shift_window(self):
        """taps=0 + search_ms=0 → the fetched far window is exactly as
        long as the mic tail (both decimated), so max_shift == 0 and the
        estimator must return None instead of correlating."""
        aec = NlmsAec(taps=0, search_ms=0)
        aec.push_reference(_to_pcm(_colored_noise(6400)))
        aec._mic_tail = _colored_noise(1600)
        aec._mic_tail_base = 0
        assert aec._estimate_lag(np.zeros(160, dtype=np.float32)) is None

    def test_estimate_lag_empty_correlation_guard(self, monkeypatch):
        """Guard pin: an EMPTY valid-mode correlation must read as "no
        estimate" (keep the previous lag) instead of crashing on argmax.
        numpy always returns |len(a)-len(v)|+1 elements for non-empty
        inputs (it even swaps the arrays when v is longer), so this
        branch is defensive — np.correlate is patched here to pin its
        contract."""
        aec = NlmsAec()
        aec.push_reference(_to_pcm(_colored_noise(6400)))
        aec._mic_tail = _colored_noise(1600)
        aec._mic_tail_base = 0
        monkeypatch.setattr(
            np, "correlate", lambda a, v, mode: np.zeros(0)
        )
        assert aec._estimate_lag(np.zeros(160, dtype=np.float32)) is None

    def test_full_alignment_finds_true_lag(self):
        aec = NlmsAec()
        far, mic = _echo_fixture(seconds=0.8, delay=3000)
        _run_frames(aec, far, mic, frames=80)  # 0.8 s
        status = aec.status()
        assert status["lag_samples"] == 3000
        assert status["aligned"] is True
        # the filter actually trained on the echo path
        assert float(np.sum(np.abs(aec._w))) > 0.0
        assert status["erle"] is not None and status["erle"] > 1.0
        assert status["far_samples"] == 80 * 160

    def test_bulk_delay_change_resets_filter(self, monkeypatch):
        """A re-estimate that moved >64 samples invalidates the filter."""
        aec = NlmsAec()
        aec.push_reference(_to_pcm(_colored_noise(4000)))  # far history
        aec._w[:] = 0.01  # a previously-trained filter
        aec._lag = 2000  # a stale bulk-delay estimate
        aec._aligned = True
        aec._last_realign = 0.0  # force the periodic realign cadence
        monkeypatch.setattr(aec, "_estimate_lag", lambda frame: 3000)
        # silent mic → adaptation is frozen, so only the reset can move w
        aec.process(_to_pcm(np.zeros(160, dtype=np.float32)))
        assert aec._lag == 3000  # re-estimated
        assert float(np.sum(np.abs(aec._w))) == 0.0  # filter was reset

    def test_failed_realign_keeps_previous_lag(self, monkeypatch):
        aec = NlmsAec()
        aec.push_reference(_to_pcm(_colored_noise(4000)))
        aec._w[:] = 0.01
        aec._lag = 2000
        aec._aligned = True
        aec._last_realign = 0.0
        # the estimate cannot be trusted (double-talk) → keep everything
        monkeypatch.setattr(aec, "_estimate_lag", lambda frame: None)
        aec.process(_to_pcm(np.zeros(160, dtype=np.float32)))
        assert aec._lag == 2000  # unchanged
        assert aec._aligned is True
        assert float(np.sum(np.abs(aec._w))) == pytest.approx(0.01 * aec._n)

    def test_double_talk_freezes_adaptation(self):
        aec = NlmsAec()
        far, mic = _echo_fixture(seconds=1.0, delay=3000)
        _run_frames(aec, far, mic, frames=60)
        w_before = aec._w.copy()
        assert float(np.sum(np.abs(w_before))) > 0.0
        # near-end speech: independent noise on top of the (absent) echo
        talk = _colored_noise(160 * 5, seed=123)
        _run_frames(aec, far, talk, frames=5, offset=60)
        # the residual is uncorrelated with the echo estimate → DTD
        # freezes the adaptation: weights must be bit-identical
        assert np.array_equal(aec._w, w_before)

    def test_silent_mic_freezes_adaptation(self):
        aec = NlmsAec()
        far, mic = _echo_fixture(seconds=1.0, delay=3000)
        _run_frames(aec, far, mic, frames=60)
        w_before = aec._w.copy()
        silent = np.zeros(160 * 5, dtype=np.float32)
        _run_frames(aec, far, silent, frames=5, offset=60)
        assert np.array_equal(aec._w, w_before)


class TestBaseProvider:
    def test_interface_stubs(self):
        base = AECProvider()
        with pytest.raises(NotImplementedError):
            base.push_reference(b"")
        with pytest.raises(NotImplementedError):
            base.process(b"")
        with pytest.raises(NotImplementedError):
            base.reset()
        assert base.status() == {"provider": "base"}
        assert base.name == "base"


# ── NlmsAec: processing edges ─────────────────────────────────────────


class TestProcessEdges:
    def test_empty_mic_returns_empty(self):
        aec = NlmsAec()
        assert aec.process(b"") == b""

    def test_passthrough_without_far(self):
        aec = NlmsAec()
        mic = (np.sin(np.arange(1600) * 0.05) * 12000).astype("<i2").tobytes()
        out = aec.process(mic)
        got = np.frombuffer(out, dtype="<i2")
        want = np.frombuffer(mic, dtype="<i2")
        assert len(got) == len(want)
        assert np.allclose(got.astype(float), want.astype(float), atol=2.0)

    def test_short_reference_passes_block_through(self):
        """Defensive branch: a block whose reference window is shorter
        than the filter length returns the mic unprocessed."""
        aec = NlmsAec()
        aec._lag = 0
        empty = aec._process_block(1000, np.zeros(0, dtype=np.float32))
        assert len(empty) == 0  # L=0 → seg < N → y.copy()

    def test_status_erle_none_before_any_processing(self):
        aec = NlmsAec()
        status = aec.status()
        assert status == {
            "provider": "nlms",
            "taps": aec._n,
            "mu": aec._mu,
            "leak": aec._leak,
            "lag_samples": None,
            "aligned": False,
            "erle": None,
            "far_samples": 0,
        }

    def test_reset_restores_factory_state(self):
        aec = NlmsAec()
        far, mic = _echo_fixture(seconds=0.5, delay=3000)
        _run_frames(aec, far, mic, frames=50)
        aec.reset()
        status = aec.status()
        assert status["far_samples"] == 0
        assert status["lag_samples"] is None
        assert status["aligned"] is False
        assert status["erle"] is None
        assert float(np.sum(np.abs(aec._w))) == 0.0


# ── WebRtcAec with a fake binding ─────────────────────────────────────


class FakeEchoCancellation:
    """The apm.echo_cancellation sub-object (attr-style enabling)."""

    def __init__(self):
        self.enabled = False


class FakeApm:
    """A webrtc AudioProcessingModule duck-typed for WebRtcAec."""

    def __init__(self, name="webrtc_audio_processing", proc_style="bytes",
                 reverse=True, ec_style="attr"):
        self.module_name = name
        self.echo_cancellation = FakeEchoCancellation()
        self._ec_style = ec_style
        self._proc_style = proc_style
        self._reverse = reverse
        self.proc_frames = []
        self.reverse_frames = []
        self.reset_calls = 0
        self._enable_calls = []

    # echo cancellation enabling
    def enable_echo_cancellation(self, flag):
        self._enable_calls.append(("method", flag))

    def set_echo_cancellation_enabled(self, flag):
        self._enable_calls.append(("method2", flag))

    # stream processing
    def process_stream(self, data):
        if self._proc_style == "bytes" and isinstance(data, bytes):
            self.proc_frames.append(data)
            return b"P" + data[1:]
        if self._proc_style == "numpy":
            arr = np.frombuffer(data, dtype="<i2")
            self.proc_frames.append(arr)
            return arr * 0  # ndarray result → int16_to_pcm path
        if self._proc_style == "kwargs":
            raise TypeError("positional not supported")
        raise RuntimeError("no signature accepted")  # proc_style="broken"

    def process_reverse_stream(self, frame):
        self.reverse_frames.append(frame)
        return frame

    def reset(self):
        self.reset_calls += 1


def _install_webrtc(monkeypatch, apm, module_name="webrtc_audio_processing",
                    factory_name="AudioProcessingModule"):
    def factory(*args, **kwargs):
        if args or kwargs:
            raise TypeError("factory takes no arguments")
        return apm

    mod = types.ModuleType(module_name)
    setattr(mod, factory_name, factory)
    monkeypatch.setitem(sys.modules, module_name, mod)
    return mod


class TestWebRtcAec:
    def test_attr_style_bytes_roundtrip(self, monkeypatch):
        apm = FakeApm(proc_style="bytes", ec_style="attr")
        _install_webrtc(monkeypatch, apm)
        aec = WebRtcAec()
        assert apm.echo_cancellation.enabled is True
        frame = b"\x01\x02" * 160  # 320 bytes = one 10 ms frame
        aec.push_reference(frame + frame[:160])  # 1.5 frames
        assert len(apm.reverse_frames) == 1  # one full frame consumed
        assert len(aec._far_pending) == 160
        f = b"\x03\x04" * 160  # 320 bytes
        out = aec.process(f * 2 + f[:160])  # 2.5 frames
        assert len(apm.proc_frames) == 2
        # every processed frame came back transformed; the 5 ms tail
        # passes through untouched (the mic is never dropped)
        assert out == (b"P" + f[1:]) * 2 + f[:160]
        assert aec.status() == {"provider": "webrtc", "sample_rate": SR}

    def test_method_style_enabling(self, monkeypatch):
        # no echo_cancellation attribute at all → the method style kicks in
        apm = FakeApm()
        del apm.echo_cancellation
        _install_webrtc(monkeypatch, apm)
        WebRtcAec()
        assert ("method", True) in apm._enable_calls

    def test_numpy_signature(self, monkeypatch):
        apm = FakeApm(proc_style="numpy")
        _install_webrtc(monkeypatch, apm)
        aec = WebRtcAec()
        out = aec.process(b"\x05\x06" * 320)
        # numpy path: result ndarray → int16_to_pcm (zeros here)
        assert out == b"\x00\x00" * 320

    def test_kwargs_signature(self, monkeypatch):
        apm = FakeApm(proc_style="kwargs")

        def process_stream(data=None, sample_rate=None, num_channels=None):
            if data is None:
                raise TypeError("missing kwargs")
            apm.proc_frames.append(data)
            return memoryview(b"K" + data[1:])

        apm.process_stream = process_stream
        _install_webrtc(monkeypatch, apm)
        aec = WebRtcAec()
        frame = b"\x07\x08" * 160  # 320 bytes = one 10 ms frame
        out = aec.process(frame)
        assert out == b"K" + frame[1:]

    def test_kwargs_only_signature(self, monkeypatch):
        """Positional bytes AND numpy both rejected → the kwarg form runs."""
        apm = FakeApm()

        def process_stream(*args, **kwargs):
            if "data" not in kwargs or isinstance(kwargs.get("data"), np.ndarray):
                raise TypeError("kwargs only")
            apm.proc_frames.append(kwargs["data"])
            return memoryview(b"K" + kwargs["data"][1:])

        apm.process_stream = process_stream
        _install_webrtc(monkeypatch, apm)
        aec = WebRtcAec()
        frame = b"\x07\x08" * 160
        assert aec.process(frame) == b"K" + frame[1:]

    def test_numpy_signature_bytes_return(self, monkeypatch):
        """Step-2 numpy call whose result is plain bytes."""
        apm = FakeApm()

        def process_stream(data):
            if isinstance(data, bytes):
                raise TypeError("need ndarray")
            apm.proc_frames.append(data)
            return b"B" + data.tobytes()[1:]

        apm.process_stream = process_stream
        _install_webrtc(monkeypatch, apm)
        aec = WebRtcAec()
        frame = b"\x01\x02" * 160
        assert aec.process(frame) == b"B" + frame[1:]

    def test_kwargs_signature_ndarray_return(self, monkeypatch):
        apm = FakeApm()

        def process_stream(*args, **kwargs):
            if "data" not in kwargs:
                raise TypeError("kwargs only")
            return np.frombuffer(kwargs["data"], dtype="<i2") * 0

        apm.process_stream = process_stream
        _install_webrtc(monkeypatch, apm)
        aec = WebRtcAec()
        assert aec.process(b"\x05\x06" * 160) == b"\x00\x00" * 160

    def test_readonly_ec_attr_falls_back_to_methods(self, monkeypatch):
        class ReadOnlyEC:
            @property
            def enabled(self):
                return False

            @enabled.setter
            def enabled(self, value):
                raise RuntimeError("read-only")

        apm = FakeApm()
        apm.echo_cancellation = ReadOnlyEC()
        _install_webrtc(monkeypatch, apm)
        WebRtcAec()
        assert ("method", True) in apm._enable_calls

    def test_unrecognized_signature_raises(self, monkeypatch):
        apm = FakeApm(proc_style="broken")
        _install_webrtc(monkeypatch, apm)
        aec = WebRtcAec()
        with pytest.raises(RuntimeError, match="no recognized call signature"):
            aec.process(b"\x01\x02" * 320)

    def test_reverse_stream_optional(self, monkeypatch):
        monkeypatch.delattr(FakeApm, "process_reverse_stream")
        apm = FakeApm()
        _install_webrtc(monkeypatch, apm)
        aec = WebRtcAec()
        aec.push_reference(b"\x01\x02" * 320)  # no reverse API → no-op
        assert aec.process(b"\x03\x04" * 320)

    def test_reset(self, monkeypatch):
        apm = FakeApm()
        _install_webrtc(monkeypatch, apm)
        aec = WebRtcAec()
        aec.push_reference(b"\x01\x02" * 320)
        aec.process(b"\x03\x04" * 100)
        aec.reset()
        assert apm.reset_calls == 1
        assert aec._far_pending == b""
        assert aec._mic_pending == b""

    def test_reset_without_apm_reset(self, monkeypatch):
        monkeypatch.delattr(FakeApm, "reset")
        apm = FakeApm()
        _install_webrtc(monkeypatch, apm)
        aec = WebRtcAec()
        aec.reset()  # must not raise
        assert aec._mic_pending == b""

    def test_reset_swallows_apm_failure(self, monkeypatch):
        apm = FakeApm()

        def bad_reset():
            raise RuntimeError("cannot reset")

        apm.reset = bad_reset
        _install_webrtc(monkeypatch, apm)
        aec = WebRtcAec()
        aec.reset()  # swallowed — pending buffers still cleared
        assert aec._mic_pending == b""
        assert aec._far_pending == b""

    # ── constructor rejection paths ──────────────────────────────────

    def test_no_module_importable(self):
        assert "webrtc_audio_processing" not in sys.modules
        assert "pywebrtc_audio" not in sys.modules
        with pytest.raises(RuntimeError, match="no WebRTC audio processing"):
            WebRtcAec()

    def test_no_known_factory(self, monkeypatch):
        mod = types.ModuleType("webrtc_audio_processing")
        monkeypatch.setitem(sys.modules, "webrtc_audio_processing", mod)
        with pytest.raises(RuntimeError, match="none of the known factories"):
            WebRtcAec()

    def test_factory_needs_kwargs(self, monkeypatch):
        apm = FakeApm()

        def factory(*, debug):
            return apm

        mod = types.ModuleType("webrtc_audio_processing")
        mod.AudioProcessingModule = factory
        monkeypatch.setitem(sys.modules, "webrtc_audio_processing", mod)
        WebRtcAec()  # factory() TypeError → factory(debug=False) succeeds

    def test_factory_hard_failure(self, monkeypatch):
        def factory(*a, **k):
            raise ValueError("cannot build APM")

        mod = types.ModuleType("webrtc_audio_processing")
        mod.AudioProcessingModule = factory
        monkeypatch.setitem(sys.modules, "webrtc_audio_processing", mod)
        with pytest.raises(RuntimeError, match="could not instantiate"):
            WebRtcAec()

    def test_cannot_enable_echo_cancellation(self, monkeypatch):
        apm = FakeApm()
        del apm.echo_cancellation

        def refuse(flag):
            raise RuntimeError("nope")

        apm.enable_echo_cancellation = refuse
        apm.set_echo_cancellation_enabled = refuse
        _install_webrtc(monkeypatch, apm)
        with pytest.raises(RuntimeError, match="could not enable echo"):
            WebRtcAec()

    def test_no_process_stream_method(self, monkeypatch):
        monkeypatch.delattr(FakeApm, "process_stream")
        apm = FakeApm()
        _install_webrtc(monkeypatch, apm)
        with pytest.raises(RuntimeError, match="none of the known stream"):
            WebRtcAec()


# ── create_aec mode selection ────────────────────────────────────────


class TestCreateAec:
    def test_none_mode(self):
        assert isinstance(create_aec("none"), PassThroughAec)

    def test_nlms_mode(self):
        assert isinstance(create_aec("nlms"), NlmsAec)

    def test_webrtc_mode_propagates_failure(self):
        with pytest.raises(RuntimeError):
            create_aec("webrtc")

    def test_auto_falls_back_to_nlms(self):
        aec = create_aec("auto")
        assert isinstance(aec, NlmsAec)

    def test_auto_prefers_verified_webrtc(self, monkeypatch):
        apm = FakeApm()
        _install_webrtc(monkeypatch, apm)
        aec = create_aec("auto")
        assert isinstance(aec, WebRtcAec)

    def test_mode_from_settings_when_none_given(self, monkeypatch):
        monkeypatch.setattr(settings, "VOICE_AEC", "  NLMS ")
        aec = create_aec(None)
        assert isinstance(aec, NlmsAec)
        monkeypatch.setattr(settings, "VOICE_AEC", "")
        # empty → default 'auto' → no binding installed → NLMS
        monkeypatch.delitem(sys.modules, "webrtc_audio_processing", raising=False)
        assert isinstance(create_aec(), NlmsAec)


# ── PassThroughAec ────────────────────────────────────────────────────


class TestPassThrough:
    def test_identity_and_status(self):
        aec = PassThroughAec()
        aec.push_reference(b"\x01\x00" * 1600)
        mic = bytes(range(256)) * 8
        assert aec.process(mic) is mic
        aec.reset()
        assert aec.status() == {"provider": "none"}
