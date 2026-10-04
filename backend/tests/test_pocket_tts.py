"""
Tests for the Pocket TTS engine (app/voice/tts/pocket.py).

numpy IS available; the pocket_tts package is NOT (and torch is NOT) —
the runtime is fully faked through module-level caches and sys.modules
injections (monkeypatch-restored). No model is ever downloaded (the HF
home is pinned to tmp_path in the rare paths that call ensure_hf_env),
no network, no audio devices.

Scope:
  • pure audio conversion: _piece_to_pcm / _wav_to_pcm / _to_pcm_chunks
    (raw PCM, WAV containers of every width/channel/rate, numpy arrays
    incl. resampling + piece-level rate overrides, torch-Tensor duck
    typing, scalar-holder objects), _next_or_none sentinel semantics
  • _import_pocket_tts: missing runtime error, the torch num_threads
    save/restore around the import
  • _voice_state_file: legacy layout voice-state lookup
  • warm_up: shared model cache, negative cache (tts_load_failed_recently
    + TTL expiry), load timeouts, rate capture
  • model loading: legacy local-config path, natural load_model path,
    every unrecognized-signature error
  • synthesize: sync + async audio iterators, one-shot fallback,
    start-generation call-shape probing, mid-stream cancel
  • voice-state resolution (natural name path, legacy file import,
    signature probing incl. the second assets re-check / late-file
    fallback guard) and enable_voice_cloning
"""

import asyncio
import io
import sys
import time
import types
import wave
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.voice import hf_cache, models_store  # noqa: E402
from app.voice.tts import TtsError  # noqa: E402
from app.voice.tts import pocket as pocket  # noqa: E402
from app.voice.tts.pocket import PocketTtsEngine  # noqa: E402

TARGET_RATE = 24000
CHUNK_BYTES = TARGET_RATE // 10 * 2  # 100 ms of s16le


# ── helpers ───────────────────────────────────────────────────────────


def make_spec(model="pocket-tts", language="english_2026-04", voice="mary"):
    return SimpleNamespace(
        provider="pocket-tts", model=model, language=language, voice=voice
    )


class FakePocketModel:
    """A duck-typed pocket_tts.TTSModel."""

    def __init__(self, pieces=None, sample_rate=TARGET_RATE,
                 state_result="STATE", stream_error=None):
        self.sample_rate = sample_rate
        self.state_calls = []
        self.gen_calls = []
        self._pieces = list(pieces or [])
        self._state_result = state_result
        self._stream_error = stream_error

    def get_state_for_audio_prompt(self, name):
        self.state_calls.append(name)
        if isinstance(self._state_result, Exception):
            raise self._state_result
        return self._state_result

    def generate_audio_stream(self, state, text):
        self.gen_calls.append((state, text))
        if self._stream_error is not None:
            raise self._stream_error
        return iter(list(self._pieces))


def _engine(**spec_kwargs):
    engine = PocketTtsEngine(make_spec(**spec_kwargs))
    return engine


def _engine_with_model(model=None, **spec_kwargs):
    """Engine whose model is already warm (injected, never loaded)."""
    engine = PocketTtsEngine(make_spec(**spec_kwargs))
    engine._model = model or FakePocketModel()
    return engine


def _sine_pcm(ms: float, freq=440.0, rate=TARGET_RATE) -> bytes:
    n = int(rate * ms / 1000)
    t = np.arange(n, dtype=np.float32) / rate
    samples = (np.sin(2 * np.pi * freq * t) * 0.4).astype(np.float32)
    return (np.clip(samples, -1, 1) * 32767.0).astype("<i2").tobytes()


def _wav_bytes(frames: bytes, rate: int, samp_width: int = 2,
               channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(samp_width)
        w.setframerate(rate)
        w.writeframes(frames)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _fresh_pocket_caches():
    """Isolate the module-level caches between tests."""
    saved = (
        dict(pocket._MODEL_CACHE),
        dict(pocket._LOAD_FAILURES),
        dict(pocket._VOICE_STATE_CACHE),
    )
    pocket._MODEL_CACHE.clear()
    pocket._LOAD_FAILURES.clear()
    pocket._VOICE_STATE_CACHE.clear()
    yield
    pocket._MODEL_CACHE.clear()
    pocket._MODEL_CACHE.update(saved[0])
    pocket._LOAD_FAILURES.clear()
    pocket._LOAD_FAILURES.update(saved[1])
    pocket._VOICE_STATE_CACHE.clear()
    pocket._VOICE_STATE_CACHE.update(saved[2])


# ── pure audio conversion ─────────────────────────────────────────────


class TestPieceToPcm:
    def test_raw_bytes_pass_through(self):
        engine = _engine()
        raw = b"\x01\x02" * 500  # not a RIFF header
        assert engine._piece_to_pcm(raw) == raw

    def test_raw_pcm_bytes_with_riff_header(self):
        engine = _engine()
        frames = _sine_pcm(50)
        wav = _wav_bytes(frames, TARGET_RATE)  # 16-bit mono @ target rate
        # the already-conforming container path returns the frames as-is
        assert engine._piece_to_pcm(wav) == frames

    def test_bytearray_and_memoryview_pieces(self):
        engine = _engine()
        raw = bytearray(b"\x03\x04" * 100)
        assert engine._piece_to_pcm(raw) == bytes(raw)
        mv = memoryview(b"\x05\x06" * 100)
        assert engine._piece_to_pcm(mv) == bytes(mv)

    def test_numpy_1d_at_target_rate(self):
        engine = _engine()
        piece = np.frombuffer(_sine_pcm(40), dtype="<i2").astype(
            np.float32
        ) / 32768.0
        out = engine._piece_to_pcm(piece)
        # round-trip: same samples back to s16le
        assert len(out) == len(piece) * 2
        back = np.frombuffer(out, dtype="<i2").astype(np.float32) / 32768.0
        assert np.allclose(back, piece, atol=2.0 / 32768.0)

    def test_numpy_resampled_from_native_rate(self):
        engine = _engine()
        engine._native_rate = 16000  # model outputs 16 kHz
        piece = np.frombuffer(_sine_pcm(40, rate=16000), dtype="<i2").astype(
            np.float32
        ) / 32768.0
        out = engine._piece_to_pcm(piece)
        # 16k → 24k: 1.5x the samples
        assert len(out) == pytest.approx(len(piece) * 3, rel=0.01)

    def test_piece_level_rate_override(self):
        class RatedArray(np.ndarray):
            pass  # ndarray subclass: instances accept attributes

        engine = _engine()
        engine._native_rate = 16000
        piece = (
            np.frombuffer(_sine_pcm(40, rate=16000), dtype="<i2")
            .astype(np.float32)
            .view(RatedArray)
        )
        piece.sampling_rate = 24000  # the piece knows better
        out = engine._piece_to_pcm(piece)
        assert len(out) == pytest.approx(len(piece) * 2, rel=0.01)

    def test_numpy_2d_flattened(self):
        engine = _engine()
        piece = np.zeros((10, 2), dtype=np.float32)
        piece[0, 0] = 0.5
        out = engine._piece_to_pcm(piece)
        assert len(out) == 20 * 2  # 20 mono samples

    def test_scalar_holder_objects(self):
        engine = _engine()
        arr = np.frombuffer(_sine_pcm(20), dtype="<i2").astype(np.float32)
        for attr in ("audio", "data", "samples", "pcm", "wav"):
            holder = SimpleNamespace(**{attr: arr / 32768.0})
            assert engine._piece_to_pcm(holder) == (
                (arr / 32768.0 * 32767.0).astype("<i2").tobytes()
            ), attr

    def test_unknown_piece_returns_empty(self):
        engine = _engine()
        assert engine._piece_to_pcm(42) == b""
        assert engine._piece_to_pcm(object()) == b""


class TestWavToPcm:
    def test_conforming_container_returns_frames(self):
        frames = _sine_pcm(50)
        wav = _wav_bytes(frames, TARGET_RATE)
        assert PocketTtsEngine._wav_to_pcm(wav, TARGET_RATE) == frames

    def test_resampling(self):
        frames = _sine_pcm(100, rate=22050)
        wav = _wav_bytes(frames, 22050)
        out = PocketTtsEngine._wav_to_pcm(wav, TARGET_RATE)
        n_src = len(frames) // 2
        n_dst = len(out) // 2
        assert n_dst == pytest.approx(n_src * TARGET_RATE / 22050, rel=0.02)

    def test_stereo_mixdown(self):
        stereo = np.zeros(200, dtype="<i2")
        stereo[0::2] = 8000  # left loud, right silent
        wav = _wav_bytes(stereo.tobytes(), TARGET_RATE, channels=2)
        out = PocketTtsEngine._wav_to_pcm(wav, TARGET_RATE)
        mono = np.frombuffer(out, dtype="<i2")
        assert len(mono) == 100  # 200 interleaved → 100 mono
        assert mono[0] == pytest.approx(4000, abs=4)  # mean of the pair

    def test_32bit_wav(self):
        ints = (np.arange(100, dtype="<i4") * 100000).astype("<i4")
        wav = _wav_bytes(ints.tobytes(), TARGET_RATE, samp_width=4)
        out = PocketTtsEngine._wav_to_pcm(wav, TARGET_RATE)
        mono = np.frombuffer(out, dtype="<i2")
        assert len(mono) == 100

    def test_8bit_wav(self):
        u8 = np.full(100, 200, dtype=np.uint8)  # +72 above the zero point
        wav = _wav_bytes(u8.tobytes(), TARGET_RATE, samp_width=1)
        out = PocketTtsEngine._wav_to_pcm(wav, TARGET_RATE)
        mono = np.frombuffer(out, dtype="<i2")
        # (200 - 128)/128 * 32767 ≈ 18431
        assert mono[0] == pytest.approx(18431, abs=4)

    def test_unsupported_width_returns_frames_raw(self):
        frames = b"\x00" * 60  # 3-byte samples
        wav = _wav_bytes(frames, TARGET_RATE, samp_width=3)
        assert PocketTtsEngine._wav_to_pcm(wav, TARGET_RATE) == frames

    def test_corrupt_wav_treated_as_raw(self):
        bogus = b"RIFF\x00\x00\x00\x00WAVEjunk-not-a-wav"
        assert PocketTtsEngine._wav_to_pcm(bogus, TARGET_RATE) == bogus


class TestToPcmChunks:
    def test_chunking_to_100ms_frames(self):
        engine = _engine()
        piece = np.frombuffer(_sine_pcm(1000), dtype="<i2").astype(
            np.float32
        ) / 32768.0  # 1 s → 48000 PCM bytes
        chunks = engine._to_pcm_chunks(piece)
        assert len(chunks) == 10
        assert all(len(c) == CHUNK_BYTES for c in chunks)
        assert b"".join(chunks) == (piece * 32767.0).astype("<i2").tobytes()

    def test_empty_piece_yields_no_chunks(self):
        engine = _engine()
        assert engine._to_pcm_chunks(b"") == []
        assert engine._to_pcm_chunks(np.zeros(0, dtype=np.float32)) == []

    def test_torch_tensor_duck_typing(self):
        engine = _engine()

        class Tensor:  # type name is what the engine sniffs for
            def __init__(self, arr):
                self._arr = arr

            def detach(self):
                return self

            def cpu(self):
                return self

            def numpy(self):
                return self._arr / 32768.0

        piece = Tensor(np.frombuffer(_sine_pcm(40), dtype="<i2"))
        out = engine._to_pcm_chunks(piece)
        assert out and all(len(c) <= CHUNK_BYTES for c in out)

    def test_torch_tensor_conversion_failure_falls_back(self):
        engine = _engine()

        class Tensor:
            def detach(self):
                raise RuntimeError("not really torch")

        # detach fails → not convertible → empty PCM → no chunks
        assert engine._to_pcm_chunks(Tensor()) == []


class TestNextOrNone:
    def test_sentinel_semantics(self):
        it = iter([1, 2])
        assert pocket._next_or_none(it) == 1
        assert pocket._next_or_none(it) == 2
        assert pocket._next_or_none(it) is pocket._SENTINEL
        # a falsy-but-real piece must NOT be confused with exhaustion
        it2 = iter([0, b""])
        assert pocket._next_or_none(it2) == 0
        assert pocket._next_or_none(it2) == b""
        assert pocket._next_or_none(it2) is pocket._SENTINEL

    def test_mid_stream_failure_wrapped(self):
        class Boom:
            def __iter__(self):
                return self

            def __next__(self):
                raise RuntimeError("stream broke")

        with pytest.raises(TtsError) as exc:
            pocket._next_or_none(Boom())
        assert exc.value.code == "tts_model_error"
        assert not exc.value.fatal


# ── _import_pocket_tts ────────────────────────────────────────────────


class SequenceTorch:
    """torch stand-in whose get_num_threads follows a script."""

    def __init__(self, values):
        self.values = list(values)
        self.set_calls = []

    def get_num_threads(self):
        return self.values.pop(0) if self.values else 4

    def set_num_threads(self, n):
        self.set_calls.append(n)


class TestImportPocketTts:
    def test_missing_runtime(self):
        assert "pocket_tts" not in sys.modules
        with pytest.raises(TtsError) as exc:
            pocket._import_pocket_tts()
        assert exc.value.code == "tts_runtime_missing"
        assert "pocket_tts" in str(exc.value)
        assert not exc.value.fatal

    def test_thread_override_is_restored(self, monkeypatch):
        # before=4 → import → the package set it to 1 → restore to 4
        torch = SequenceTorch([4, 1])
        monkeypatch.setitem(sys.modules, "torch", torch)
        monkeypatch.setitem(
            sys.modules, "pocket_tts", types.ModuleType("pocket_tts")
        )
        pocket._import_pocket_tts()
        assert torch.set_calls == [4]

    def test_thread_restore_failure_is_swallowed(self, monkeypatch):
        class UnrestorableTorch(SequenceTorch):
            def set_num_threads(self, n):
                raise RuntimeError("cannot set threads")

        torch = UnrestorableTorch([4, 1])
        monkeypatch.setitem(sys.modules, "torch", torch)
        monkeypatch.setitem(
            sys.modules, "pocket_tts", types.ModuleType("pocket_tts")
        )
        # the best-effort restore must never break the import
        assert isinstance(pocket._import_pocket_tts(), types.ModuleType)

    def test_no_thread_change_no_restore(self, monkeypatch):
        torch = SequenceTorch([4, 4])
        monkeypatch.setitem(sys.modules, "torch", torch)
        monkeypatch.setitem(
            sys.modules, "pocket_tts", types.ModuleType("pocket_tts")
        )
        pocket._import_pocket_tts()
        assert torch.set_calls == []

    def test_import_failure_with_torch_present(self, monkeypatch):
        torch = SequenceTorch([4])
        monkeypatch.setitem(sys.modules, "torch", torch)
        # pocket_tts stays unimportable → TtsError, no restore needed
        monkeypatch.delitem(sys.modules, "pocket_tts", raising=False)
        with pytest.raises(TtsError, match="pocket_tts"):
            pocket._import_pocket_tts()


# ── legacy assets / voice state file ──────────────────────────────────


class TestVoiceStateFile:
    def test_default_voice_is_mary(self, tmp_path):
        mary = tmp_path / "voice-mary.safetensors"
        mary.write_bytes(b"s")
        assert pocket._voice_state_file(tmp_path, None) == mary

    def test_named_voice_and_slash_sanitization(self, tmp_path):
        ann = tmp_path / "voice-Ann_Other.safetensors"
        ann.write_bytes(b"s")
        assert pocket._voice_state_file(tmp_path, "Ann/Other") == ann

    def test_missing_named_voice_returns_none(self, tmp_path):
        # no file for the requested voice AND no legacy fallback files
        assert pocket._voice_state_file(tmp_path, "zoe") is None
        assert pocket._voice_state_file(tmp_path, "mary") is None

    def test_fallback_glob(self, tmp_path):
        (tmp_path / "voice-zoe.safetensors").write_bytes(b"x")
        (tmp_path / "voice-ada.safetensors").write_bytes(b"x")
        # the named voice has no file → first voice-*.safetensors wins
        f = pocket._voice_state_file(tmp_path, "missing")
        assert f == tmp_path / "voice-ada.safetensors"

    def test_no_files_returns_none(self, tmp_path):
        assert pocket._voice_state_file(tmp_path, "mary") is None


class TestLegacyAssetsDir:
    def test_passthrough_and_failures(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            models_store, "tts_assets_dir", lambda: tmp_path
        )
        from app.voice.tts import pocket as p

        assert p._legacy_local_assets_dir() is tmp_path
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: None)
        assert p._legacy_local_assets_dir() is None

        def boom():
            raise RuntimeError("manifest unreadable")

        monkeypatch.setattr(models_store, "tts_assets_dir", boom)
        # probing must never break loading → None (natural path)
        assert p._legacy_local_assets_dir() is None


# ── warm_up + model cache ─────────────────────────────────────────────


class TestWarmUp:
    def test_shared_cache_no_reload(self):
        calls = []
        model = FakePocketModel()

        def load():
            calls.append(1)
            return model

        engine = _engine()
        engine._load_model = load
        got = asyncio.run(engine.warm_up())
        assert got is model
        engine2 = _engine()
        engine2._load_model = load
        assert asyncio.run(engine2.warm_up()) is model
        assert calls == [1]  # second engine shared the instance
        assert pocket._MODEL_CACHE[("pocket-tts", "english_2026-04")] is model

    def test_already_warm_is_noop(self):
        engine = _engine_with_model()
        model = engine._model
        engine._load_model = lambda: pytest.fail("must not reload")
        assert asyncio.run(engine.warm_up()) is model

    def test_load_failure_negatively_cached(self):
        engine = _engine()

        def bad():
            raise RuntimeError("weights unreadable")

        engine._load_model = bad
        with pytest.raises(RuntimeError, match="weights unreadable"):
            asyncio.run(engine.warm_up())
        # a repeat warm-up within the TTL surfaces ONE recoverable error
        engine._load_model = lambda: pytest.fail("must not reload")
        with pytest.raises(TtsError) as exc:
            asyncio.run(engine.warm_up())
        assert exc.value.code == "tts_load_failed_recently"
        assert not exc.value.fatal
        assert "setup wizard" in str(exc.value)

    def test_negative_cache_ttl_expiry_allows_retry(self):
        model = FakePocketModel()
        engine = _engine()
        engine._load_model = lambda: model
        pocket._LOAD_FAILURES[("pocket-tts", "english_2026-04")] = (
            time.monotonic() - pocket._LOAD_FAILURE_TTL_S - 1
        )
        assert asyncio.run(engine.warm_up()) is model
        assert ("pocket-tts", "english_2026-04") not in pocket._LOAD_FAILURES

    def test_load_timeout_recoverable_and_cached(self, monkeypatch):
        engine = _engine()
        engine._load_model = lambda: FakePocketModel()

        async def instant_timeout(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", instant_timeout)
        with pytest.raises(TtsError) as exc:
            asyncio.run(engine.warm_up())
        assert exc.value.code == "tts_model_load_timeout"
        assert not exc.value.fatal
        assert "setup wizard" in str(exc.value)
        assert ("pocket-tts", "english_2026-04") in pocket._LOAD_FAILURES

    def test_timeout_bounded_by_settings(self, monkeypatch):
        captured = {}

        async def fake_wait_for(coro, timeout):
            captured["timeout"] = timeout
            coro.close()
            raise asyncio.TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
        monkeypatch.setattr(settings, "VOICE_TTS_LOAD_TIMEOUT_SEC", 12)
        engine = _engine()
        engine._load_model = lambda: FakePocketModel()
        with pytest.raises(TtsError):
            asyncio.run(engine.warm_up())
        assert captured["timeout"] == 12.0
        monkeypatch.setattr(settings, "VOICE_TTS_LOAD_TIMEOUT_SEC", 0)
        pocket._LOAD_FAILURES.clear()
        engine = _engine()
        engine._load_model = lambda: FakePocketModel()
        with pytest.raises(TtsError):
            asyncio.run(engine.warm_up())
        assert captured["timeout"] == 1.0  # clamped up to 1 s

    def test_capture_rate(self):
        engine = _engine()
        engine._capture_rate(SimpleNamespace(sample_rate=16000))
        assert engine._native_rate == 16000
        engine._capture_rate(SimpleNamespace(sample_rate=24000))
        assert engine._native_rate == 24000
        for bad in (None, 0, "24k", -1):
            engine._capture_rate(SimpleNamespace(sample_rate=bad))
            assert engine._native_rate == settings.VOICE_TTS_SAMPLE_RATE

    def test_status(self):
        engine = _engine()
        assert engine.status() == {
            "provider": "pocket-tts",
            "model": "pocket-tts",
            "language": "english_2026-04",
            "voice": "mary",
            "loaded": False,
            "sample_rate": TARGET_RATE,
        }
        engine._model = FakePocketModel()
        assert engine.status()["loaded"] is True


# ── model loading paths ───────────────────────────────────────────────


class TestLoadModel:
    def _pkg(self, tts_model):
        pkg = types.ModuleType("pocket_tts")
        pkg.TTSModel = tts_model
        return pkg

    def test_legacy_local_config(self, monkeypatch, tmp_path):
        captured = {}

        class TTSModel:
            @staticmethod
            def load_model(config=None, **kw):
                captured.update(config=config, kw=kw)
                return FakePocketModel(sample_rate=16000)

        monkeypatch.setattr(
            pocket, "_import_pocket_tts", lambda: self._pkg(TTSModel)
        )
        assets = tmp_path / "tts" / "pocket"
        assets.mkdir(parents=True)
        (assets / pocket.LOCAL_CONFIG_NAME).write_text("weights: x")
        monkeypatch.setattr(
            models_store, "tts_assets_dir", lambda: assets
        )
        engine = _engine()
        model = engine._load_model()
        assert isinstance(model, FakePocketModel)
        assert engine._assets_dir is assets
        assert engine._native_rate == 16000
        assert captured["config"] == str(assets / pocket.LOCAL_CONFIG_NAME)

    def test_legacy_load_failure_wrapped(self, monkeypatch, tmp_path):
        class TTSModel:
            @staticmethod
            def load_model(config=None, **kw):
                raise RuntimeError("corrupt weights")

        monkeypatch.setattr(
            pocket, "_import_pocket_tts", lambda: self._pkg(TTSModel)
        )
        assets = tmp_path
        (assets / pocket.LOCAL_CONFIG_NAME).write_text("x")
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: assets)
        engine = _engine()
        with pytest.raises(TtsError) as exc:
            engine._load_model()
        assert exc.value.code == "tts_model_error"

    def test_natural_load_model(self, monkeypatch, tmp_path):
        captured = {}

        class TTSModel:
            @staticmethod
            def load_model(language=None, **kw):
                captured.update(language=language)
                return FakePocketModel()

        monkeypatch.setattr(
            pocket, "_import_pocket_tts", lambda: self._pkg(TTSModel)
        )
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: None)
        monkeypatch.setattr(
            hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf")
        )
        engine = _engine()
        model = engine._load_model()
        assert isinstance(model, FakePocketModel)
        assert engine._assets_dir is None
        assert captured["language"] == "english_2026-04"
        assert engine._native_rate == TARGET_RATE

    def test_natural_offline_failure(self, monkeypatch, tmp_path):
        class TTSModel:
            @staticmethod
            def load_model(language=None, **kw):
                raise RuntimeError("LocalEntryNotFoundError: cold cache")

        monkeypatch.setattr(
            pocket, "_import_pocket_tts", lambda: self._pkg(TTSModel)
        )
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: None)
        monkeypatch.setattr(
            hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf")
        )
        engine = _engine()
        with pytest.raises(TtsError) as exc:
            engine._load_model()
        assert exc.value.code == "tts_not_ready"
        assert "not in the local HF cache" in str(exc.value)

    def test_construct_model_natural_constructor_fallback(
        self, monkeypatch, tmp_path
    ):
        made = FakePocketModel()

        class TTSModel:  # no load_model at all
            def __init__(self, language=None):
                self.language = language

            @classmethod
            def build(cls, language=None):
                return made

        monkeypatch.setattr(
            pocket, "_import_pocket_tts", lambda: self._pkg(TTSModel)
        )
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: None)
        # the natural path calls ensure_hf_env() — keep it on tmp_path so
        # nothing is created inside the checkout
        monkeypatch.setattr(
            hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf")
        )
        engine = _engine()
        # constructor with language kwarg succeeds on the first shape
        loaded = engine._load_model()
        assert isinstance(loaded, TTSModel)
        assert loaded.language == "english_2026-04"

    def test_construct_model_unrecognized_signature(self, monkeypatch, tmp_path):
        class TTSModel:
            @staticmethod
            def load_model(config=None, **kw):
                raise TypeError("unknown kwarg")

            def __init__(self, *a, **k):
                raise TypeError("no constructor either")

        monkeypatch.setattr(
            pocket, "_import_pocket_tts", lambda: self._pkg(TTSModel)
        )
        assets = tmp_path
        (assets / pocket.LOCAL_CONFIG_NAME).write_text("x")
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: assets)
        engine = _engine()
        with pytest.raises(TtsError) as exc:
            engine._load_model()
        assert "unrecognized signature" in str(exc.value)

    def test_no_tts_model_attribute(self, monkeypatch, tmp_path):
        pkg = types.ModuleType("pocket_tts")  # no TTSModel at all
        monkeypatch.setattr(pocket, "_import_pocket_tts", lambda: pkg)
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: None)
        monkeypatch.setattr(
            hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf")
        )
        engine = _engine()
        with pytest.raises(TtsError, match="no TTSModel"):
            engine._load_model()

    def test_legacy_no_tts_model_attribute(self, monkeypatch, tmp_path):
        pkg = types.ModuleType("pocket_tts")  # no TTSModel at all
        monkeypatch.setattr(pocket, "_import_pocket_tts", lambda: pkg)
        assets = tmp_path
        (assets / pocket.LOCAL_CONFIG_NAME).write_text("x")
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: assets)
        engine = _engine()
        with pytest.raises(TtsError, match="no TTSModel"):
            engine._load_model()

    def test_legacy_constructor_success(self, monkeypatch, tmp_path):
        # no load_model at all — the TTSModel(config=...) constructor works
        class TTSModel:
            def __init__(self, config=None):
                self.config = config

        monkeypatch.setattr(
            pocket, "_import_pocket_tts", lambda: self._pkg(TTSModel)
        )
        assets = tmp_path
        (assets / pocket.LOCAL_CONFIG_NAME).write_text("x")
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: assets)
        engine = _engine()
        model = engine._load_model()
        assert isinstance(model, TTSModel)
        assert model.config == str(assets / pocket.LOCAL_CONFIG_NAME)

    def test_legacy_constructor_all_shapes_fail(self, monkeypatch, tmp_path):
        class TTSModel:
            def __init__(self, *a, **k):
                raise TypeError("no known signature")

        monkeypatch.setattr(
            pocket, "_import_pocket_tts", lambda: self._pkg(TTSModel)
        )
        assets = tmp_path
        (assets / pocket.LOCAL_CONFIG_NAME).write_text("x")
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: assets)
        engine = _engine()
        with pytest.raises(TtsError, match="no TTSModel.load_model"):
            engine._load_model()

    def test_legacy_constructor_hard_failure_wrapped(
        self, monkeypatch, tmp_path
    ):
        class TTSModel:  # non-TypeError constructor failure → wrapped
            def __init__(self, config=None):
                raise ValueError("bad config")

        monkeypatch.setattr(
            pocket, "_import_pocket_tts", lambda: self._pkg(TTSModel)
        )
        assets = tmp_path
        (assets / pocket.LOCAL_CONFIG_NAME).write_text("x")
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: assets)
        engine = _engine()
        with pytest.raises(TtsError, match="model load failed"):
            engine._load_model()

    def test_natural_constructor_hard_failure_wrapped(
        self, monkeypatch, tmp_path
    ):
        class TTSModel:  # non-TypeError constructor failure → wrapped
            def __init__(self, *a, **k):
                raise ValueError("bad constructor")

        monkeypatch.setattr(
            pocket, "_import_pocket_tts", lambda: self._pkg(TTSModel)
        )
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: None)
        monkeypatch.setattr(
            hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf")
        )
        engine = _engine()
        with pytest.raises(TtsError, match="model load failed"):
            engine._load_model()

    def test_natural_no_load_model_all_shapes_fail(
        self, monkeypatch, tmp_path
    ):
        class TTSModel:
            def __init__(self, *a, **k):
                raise TypeError("no known signature")

        monkeypatch.setattr(
            pocket, "_import_pocket_tts", lambda: self._pkg(TTSModel)
        )
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: None)
        monkeypatch.setattr(
            hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf")
        )
        engine = _engine()
        with pytest.raises(TtsError, match="no TTSModel.load_model"):
            engine._load_model()

    def test_natural_language_kwarg_typeerror_retry(
        self, monkeypatch, tmp_path
    ):
        captured = []

        class TTSModel:
            @staticmethod
            def load_model(**kw):
                captured.append(kw)
                if "language" in kw:
                    raise TypeError("language not accepted")
                return FakePocketModel()

        monkeypatch.setattr(
            pocket, "_import_pocket_tts", lambda: self._pkg(TTSModel)
        )
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: None)
        monkeypatch.setattr(
            hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf")
        )
        engine = _engine()
        assert isinstance(engine._load_model(), FakePocketModel)
        # the language call was attempted first, then the bare retry
        assert captured == [{"language": "english_2026-04"}, {}]


# ── start_generation call-shape probing ───────────────────────────────


class TestStartGeneration:
    def _engine(self, model):
        engine = _engine_with_model(model)
        return engine

    def test_released_signature(self):
        model = FakePocketModel(pieces=[])
        engine = self._engine(model)
        state = "STATE"
        stream = engine._start_generation(model, state, "hello")
        assert stream is not None
        assert model.gen_calls == [(state, "hello")]

    def test_model_state_text_kwargs_shape(self):
        class Picky:
            def generate_audio_stream(self, model_state=None,
                                      text_to_generate=None):
                if model_state is None or text_to_generate is None:
                    raise TypeError("missing kwargs")
                return iter([b"pcm"])

        engine = self._engine(Picky())
        assert engine._start_generation(Picky(), "S", "txt") is not None

    def test_text_only_shape(self):
        class TextOnly:
            def generate_audio_stream(self, text):
                return iter([b"pcm"])

        model = TextOnly()
        engine = self._engine(model)
        stream = engine._start_generation(model, "S", "txt")
        assert stream is not None

    def test_all_shapes_fail_returns_none(self):
        class Picky:
            def generate_audio_stream(self, *a, **k):
                raise TypeError("nope")

        model = Picky()
        engine = self._engine(model)
        # no one-shot methods either → None
        assert engine._start_generation(model, "S", "txt") is None

    def test_generation_error_wrapped(self):
        model = FakePocketModel(
            stream_error=RuntimeError("model exploded")
        )
        engine = self._engine(model)
        with pytest.raises(TtsError) as exc:
            engine._start_generation(model, "S", "txt")
        assert exc.value.code == "tts_model_error"

    def test_one_shot_fallback(self):
        class OneShot:
            def generate_audio(self, state, text):
                return b"one-shot-pcm"

        model = OneShot()
        engine = self._engine(model)
        stream = engine._start_generation(model, "S", "txt")
        assert list(stream) == [b"one-shot-pcm"]

    def test_one_shot_text_only(self):
        class OneShotText:
            def generate(self, text):  # (state, text) → TypeError → retry
                return b"text-only-pcm"

        model = OneShotText()
        engine = self._engine(model)
        stream = engine._start_generation(model, "S", "txt")
        assert list(stream) == [b"text-only-pcm"]

    def test_one_shot_all_retries_exhausted(self):
        class PickyOneShot:
            def generate(self, *a):
                raise TypeError("no generate shape works")

            def synthesize(self, text):
                return b"synth-pcm"

        model = PickyOneShot()
        engine = self._engine(model)
        # generate's (state, text) AND (text) retries both fail →
        # the probe moves on to the next one-shot method name
        stream = engine._start_generation(model, "S", "txt")
        assert list(stream) == [b"synth-pcm"]


# ── synthesize flow ───────────────────────────────────────────────────


class TestSynthesize:
    def _collect(self, engine, text):
        async def run():
            return [chunk async for chunk in engine.synthesize(text)]

        return asyncio.run(run())

    def test_sync_iterator_stream(self):
        pieces = [
            np.frombuffer(_sine_pcm(120), dtype="<i2").astype(np.float32)
            / 32768.0
            for _ in range(3)
        ]
        model = FakePocketModel(pieces=pieces)
        engine = _engine_with_model(model)
        chunks = self._collect(engine, "hello world")
        # each 120 ms piece (5760 bytes) → one full 100 ms chunk + tail
        assert len(chunks) == 6
        assert all(len(c) <= CHUNK_BYTES for c in chunks)
        # the voice state was resolved through the natural name path
        assert model.state_calls == ["mary"]
        assert model.gen_calls[0][1] == "hello world"

    def test_async_iterator_stream(self):
        class AsyncPieces:
            def __init__(self, pieces):
                self._it = iter(pieces)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

        class AsyncModel(FakePocketModel):
            def generate_audio_stream(self, state, text):
                self.gen_calls.append((state, text))
                return AsyncPieces(
                    [np.zeros(240, dtype=np.float32) for _ in range(2)]
                )

        model = AsyncModel()
        engine = _engine_with_model(model)
        chunks = self._collect(engine, "async pieces")
        assert len(chunks) == 2
        assert all(len(c) == 480 for c in chunks)

    def test_empty_text_short_circuits(self):
        model = FakePocketModel()
        engine = _engine_with_model(model)
        for empty in ("", "   "):
            assert self._collect(engine, empty) == []
        assert model.gen_calls == []
        assert model.state_calls == []

    def test_no_stream_produced_raises(self):
        class NoStream:
            def get_state_for_audio_prompt(self, name):
                return "S"

        engine = _engine_with_model(NoStream())
        with pytest.raises(TtsError) as exc:
            self._collect(engine, "hello")
        assert exc.value.code == "tts_model_error"
        assert "did not produce an audio stream" in str(exc.value)

    def test_cancel_stops_mid_stream(self):
        pieces = [
            np.zeros(2400, dtype=np.float32) for _ in range(3)
        ]  # 100 ms each
        model = FakePocketModel(pieces=pieces)
        engine = _engine_with_model(model)

        async def run():
            seen = []
            async for chunk in engine.synthesize("cancel me"):
                seen.append(chunk)
                if len(seen) == 2:
                    await engine.cancel()
            return seen

        seen = asyncio.run(run())
        assert len(seen) == 2  # the third piece was never emitted

    def test_sync_cancel_lands_during_piece_fetch(self):
        """Cancel while the generator is suspended in the to_thread fetch
        → the next piece is discarded before yielding (not after)."""
        pieces = [np.zeros(2400, dtype=np.float32) for _ in range(3)]
        model = FakePocketModel(pieces=pieces)
        engine = _engine_with_model(model)

        async def run():
            gen = engine.synthesize("hello")
            seen = [await gen.__anext__()]
            asyncio.create_task(engine.cancel())  # runs during the fetch
            async for chunk in gen:
                seen.append(chunk)
            return seen

        seen = asyncio.run(run())
        assert len(seen) == 1  # pieces 2 and 3 dropped

    def test_async_cancel_after_yielded_chunk(self):
        class AsyncPieces:
            def __init__(self, pieces):
                self._it = iter(pieces)

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

        class AsyncModel(FakePocketModel):
            def generate_audio_stream(self, state, text):
                self.gen_calls.append((state, text))
                return AsyncPieces(
                    [np.zeros(2400, dtype=np.float32) for _ in range(3)]
                )

        engine = _engine_with_model(AsyncModel())

        async def run():
            seen = []
            async for chunk in engine.synthesize("hello"):
                seen.append(chunk)
                if len(seen) == 1:
                    await engine.cancel()
            return seen

        seen = asyncio.run(run())
        assert len(seen) == 1

    def test_async_cancel_during_piece_fetch(self):
        class SlowAsyncPieces:
            def __init__(self):
                self.count = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                await asyncio.sleep(0.02)  # gives the cancel task a slot
                self.count += 1
                if self.count > 3:
                    raise StopAsyncIteration
                return np.zeros(2400, dtype=np.float32)

        class SlowAsyncModel(FakePocketModel):
            def generate_audio_stream(self, state, text):
                return SlowAsyncPieces()

        engine = _engine_with_model(SlowAsyncModel())

        async def run():
            seen = []
            async for chunk in engine.synthesize("hello"):
                seen.append(chunk)
                if len(seen) == 1:
                    asyncio.create_task(engine.cancel())
            return seen

        seen = asyncio.run(run())
        assert 1 <= len(seen) < 3  # cancelled mid-fetch, never completed


# ── voice-state resolution ────────────────────────────────────────────


class TestVoiceState:
    def test_cached_per_model_and_voice(self):
        model = FakePocketModel()
        engine = _engine_with_model(model)

        async def run():
            s1 = await engine._voice_state(model)
            s2 = await engine._voice_state(model)
            return s1, s2

        s1, s2 = asyncio.run(run())
        assert s1 == s2 == "STATE"
        assert model.state_calls == ["mary"]  # imported once, cached

    def test_missing_get_state_raises(self):
        class Bare:
            pass

        engine = _engine_with_model(Bare())
        with pytest.raises(TtsError) as exc:
            asyncio.run(engine._voice_state(Bare()))
        assert exc.value.code == "tts_model_error"
        assert "get_state_for_audio_prompt" in str(exc.value)

    def test_legacy_file_import_positional(self, tmp_path):
        calls = []

        class LegacyModel:
            def get_state_for_audio_prompt(self, *a, **k):
                calls.append((a, k))
                if not a:
                    raise TypeError("needs a path")
                return "FILE-STATE"

        engine = _engine_with_model(LegacyModel())
        engine._assets_dir = tmp_path
        (tmp_path / "voice-mary.safetensors").write_bytes(b"s")
        state = asyncio.run(engine._voice_state(LegacyModel()))
        assert state == "FILE-STATE"
        assert calls[0][0] == (str(tmp_path / "voice-mary.safetensors"),)

    def test_legacy_file_import_all_signatures_fail(self, tmp_path):
        class LegacyModel:
            def get_state_for_audio_prompt(self, *a, **k):
                raise TypeError("bad signature")

        engine = _engine_with_model(LegacyModel())
        engine._assets_dir = tmp_path
        (tmp_path / "voice-mary.safetensors").write_bytes(b"s")
        with pytest.raises(TtsError) as exc:
            asyncio.run(engine._voice_state(LegacyModel()))
        assert "signature not recognized" in str(exc.value)

    def test_legacy_file_import_hard_failure(self, tmp_path):
        class LegacyModel:
            def get_state_for_audio_prompt(self, *a, **k):
                raise RuntimeError("state file corrupt")

        engine = _engine_with_model(LegacyModel())
        engine._assets_dir = tmp_path
        (tmp_path / "voice-mary.safetensors").write_bytes(b"s")
        with pytest.raises(TtsError) as exc:
            asyncio.run(engine._voice_state(LegacyModel()))
        assert "voice state import failed" in str(exc.value)

    def test_natural_name_typeerror_falls_to_file(self, tmp_path):
        class TypeErrorThenFile:
            def __init__(self):
                self.calls = 0

            def get_state_for_audio_prompt(self, *a, **k):
                self.calls += 1
                if len(a) == 1 and isinstance(a[0], str) and self.calls == 1:
                    raise TypeError("name not accepted")
                return "FILE-STATE"

        engine = _engine_with_model(TypeErrorThenFile())
        engine._assets_dir = tmp_path
        (tmp_path / "voice-mary.safetensors").write_bytes(b"s")
        state = asyncio.run(engine._voice_state(TypeErrorThenFile()))
        assert state == "FILE-STATE"

    def test_natural_typeerror_without_voice_file(self, tmp_path):
        """Assets dir set but holding NO voice file: the natural name call
        raises TypeError (older path-only signature) → the assets dir is
        re-checked, still finds nothing → the unrecognized-signature
        error (covers the second _voice_state_file probe)."""

        class OlderSignature:
            def get_state_for_audio_prompt(self, name):
                raise TypeError("older API takes a path")

        engine = _engine_with_model(OlderSignature())
        engine._assets_dir = tmp_path  # exists, but no voice-*.safetensors
        with pytest.raises(TtsError) as exc:
            asyncio.run(engine._voice_state(OlderSignature()))
        assert exc.value.code == "tts_model_error"
        assert "did not accept voice" in str(exc.value)

    def test_natural_typeerror_late_file_fallback(self, tmp_path, monkeypatch):
        """Guard pin for the late-file fallback import: both probes pass
        identical arguments, so production can never see a voice file
        appear only on the re-check — _voice_state_file is patched to do
        exactly that, pinning that a late-found file IS imported."""
        voice_file = tmp_path / "voice-mary.safetensors"
        voice_file.write_bytes(b"s")
        probes = []

        def flaky_probe(assets_dir, voice):
            probes.append((assets_dir, voice))
            return voice_file if len(probes) > 1 else None

        class OlderSignature:
            def get_state_for_audio_prompt(self, *a, **k):
                if a and str(a[0]).endswith(".safetensors"):
                    return "FILE-STATE"
                raise TypeError("older API takes a path")

        engine = _engine_with_model(OlderSignature())
        engine._assets_dir = tmp_path
        monkeypatch.setattr(pocket, "_voice_state_file", flaky_probe)
        state = asyncio.run(engine._voice_state(OlderSignature()))
        assert state == "FILE-STATE"
        assert len(probes) == 2  # the assets dir was re-checked

    def test_natural_name_hard_failure(self):
        class HardFail:
            def get_state_for_audio_prompt(self, name):
                raise RuntimeError("cache miss for voice")

        engine = _engine_with_model(HardFail())
        with pytest.raises(TtsError) as exc:
            asyncio.run(engine._voice_state(HardFail()))
        assert exc.value.code == "tts_not_ready"
        assert "re-run the setup wizard" in str(exc.value)

    def test_unrecognized_signature_without_assets(self):
        class Weird:
            def get_state_for_audio_prompt(self, *a, **k):
                raise TypeError("nope")

        engine = _engine_with_model(Weird())
        with pytest.raises(TtsError) as exc:
            asyncio.run(engine._voice_state(Weird()))
        assert "unrecognized signature" in str(exc.value)


# ── voice cloning ─────────────────────────────────────────────────────


class TestVoiceCloning:
    def test_model_already_cloning_capable(self):
        model = FakePocketModel()
        model.has_voice_cloning = True
        engine = _engine_with_model(model)
        assert asyncio.run(engine.enable_voice_cloning()) is model

    def test_cache_hit_returns_upgraded(self):
        upgraded = FakePocketModel()
        upgraded.has_voice_cloning = True
        pocket._MODEL_CACHE[("pocket-tts", "english_2026-04")] = upgraded
        engine = _engine_with_model(FakePocketModel())
        assert asyncio.run(engine.enable_voice_cloning()) is upgraded
        assert engine._model is upgraded

    def test_cloning_load_failure(self):
        engine = _engine_with_model(FakePocketModel())

        def bad():
            raise RuntimeError("gated download failed")

        engine._load_cloning_model = bad
        with pytest.raises(TtsError) as exc:
            asyncio.run(engine.enable_voice_cloning())
        assert exc.value.code == "tts_voice_cloning_unavailable"

    def test_upgraded_without_flag_rejected(self):
        engine = _engine_with_model(FakePocketModel())
        engine._load_cloning_model = lambda: FakePocketModel()
        with pytest.raises(TtsError) as exc:
            asyncio.run(engine.enable_voice_cloning())
        assert "catalog-only fallback" in str(exc.value)

    def test_success_updates_cache_and_purges_states(self):
        engine = _engine_with_model(FakePocketModel())
        pocket._VOICE_STATE_CACHE[("pocket-tts", "mary")] = "OLD-STATE"
        pocket._VOICE_STATE_CACHE[("other-model", "mary")] = "KEPT"

        upgraded = FakePocketModel()
        upgraded.has_voice_cloning = True
        engine._load_cloning_model = lambda: upgraded
        assert asyncio.run(engine.enable_voice_cloning()) is upgraded
        assert engine._model is upgraded
        assert pocket._MODEL_CACHE[("pocket-tts", "english_2026-04")] is (
            upgraded
        )
        # the voice states for THIS model were dropped; other models kept
        assert ("pocket-tts", "mary") not in pocket._VOICE_STATE_CACHE
        assert pocket._VOICE_STATE_CACHE[("other-model", "mary")] == "KEPT"

    def test_load_cloning_model_flow(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf")
        )
        cfg_mod = types.ModuleType("pocket_tts.utils.config")
        cfg_mod.CONFIGS_DIR = Path("/cfgs")
        cfg_mod.load_config = lambda p: SimpleNamespace(
            weights_path="/w/weights.safetensors"
        )
        dl_calls = []
        dl_mod = types.ModuleType("pocket_tts.utils.utils")
        dl_mod.download_if_necessary = lambda p: dl_calls.append(p)
        made = FakePocketModel()
        captured = {}

        class TTSModel:
            @staticmethod
            def load_model(**kw):
                captured.update(kw)
                return made

        pkg = types.ModuleType("pocket_tts")
        pkg.TTSModel = TTSModel
        monkeypatch.setitem(sys.modules, "pocket_tts", pkg)
        monkeypatch.setitem(
            sys.modules, "pocket_tts.utils", types.ModuleType("pocket_tts.utils")
        )
        monkeypatch.setitem(
            sys.modules, "pocket_tts.utils.config", cfg_mod
        )
        monkeypatch.setitem(
            sys.modules, "pocket_tts.utils.utils", dl_mod
        )
        engine = _engine()
        model = engine._load_cloning_model()
        assert model is made
        assert dl_calls == ["/w/weights.safetensors"]
        assert captured == {"language": "english_2026-04"}
        assert engine._native_rate == TARGET_RATE

    def test_load_cloning_model_no_weights(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf")
        )
        cfg_mod = types.ModuleType("pocket_tts.utils.config")
        cfg_mod.CONFIGS_DIR = Path("/cfgs")
        cfg_mod.load_config = lambda p: SimpleNamespace(weights_path=None)
        dl_mod = types.ModuleType("pocket_tts.utils.utils")
        dl_mod.download_if_necessary = lambda p: None
        pkg = types.ModuleType("pocket_tts")
        pkg.TTSModel = SimpleNamespace()  # never reached
        monkeypatch.setitem(sys.modules, "pocket_tts", pkg)
        monkeypatch.setitem(
            sys.modules, "pocket_tts.utils", types.ModuleType("pocket_tts.utils")
        )
        monkeypatch.setitem(
            sys.modules, "pocket_tts.utils.config", cfg_mod
        )
        monkeypatch.setitem(
            sys.modules, "pocket_tts.utils.utils", dl_mod
        )
        engine = _engine()
        with pytest.raises(RuntimeError, match="no cloning weights"):
            engine._load_cloning_model()

    def test_load_cloning_model_missing_api(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf")
        )
        cfg_mod = types.ModuleType("pocket_tts.utils.config")
        cfg_mod.CONFIGS_DIR = Path("/cfgs")
        cfg_mod.load_config = lambda p: SimpleNamespace(
            weights_path="/w/weights.safetensors"
        )
        dl_mod = types.ModuleType("pocket_tts.utils.utils")
        dl_mod.download_if_necessary = lambda p: None
        pkg = types.ModuleType("pocket_tts")  # no TTSModel
        monkeypatch.setitem(sys.modules, "pocket_tts", pkg)
        monkeypatch.setitem(
            sys.modules, "pocket_tts.utils", types.ModuleType("pocket_tts.utils")
        )
        monkeypatch.setitem(
            sys.modules, "pocket_tts.utils.config", cfg_mod
        )
        monkeypatch.setitem(
            sys.modules, "pocket_tts.utils.utils", dl_mod
        )
        engine = _engine()
        with pytest.raises(RuntimeError, match="no TTSModel.load_model"):
            engine._load_cloning_model()
