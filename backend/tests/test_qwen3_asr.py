"""
Tests for the Qwen3-ASR engine (app/voice/asr/qwen3.py).

Scope:
  • config / dtype policy (_resolve_dtype: aliases, auto→bf16/fp16)
  • local snapshot resolution (_local_snapshot_dir: legacy manifest
    layout, HF hub cache, probe failures) — tmp_path layouts only
  • the streaming AsrProvider surface (start_stream / feed_audio /
    get_partial / finish_stream / cancel / warm_up / status) against a
    FAKE loaded model injected through the module-level model cache
  • partial throttling: min audio, interval spacing, long-utterance cap,
    busy-lock fallback
  • transcription paths for every runtime shape (official qwen_asr
    wrapper, transformers pipeline, processor+model with the TypeError
    retry) plus the error branches (not loaded, runtime missing, model
    error)
  • model loading: qwen_asr primary, transformers fallback class
    probing, pipeline last resort, load timeouts, the negative cache
    (asr_load_failed_recently / TTL expiry)

Mocks — torch, transformers and qwen_asr are NOT installed in this
environment; fake module objects are injected into sys.modules via
monkeypatch (restored automatically). No model is ever downloaded (all
loads run through fakes; HF home is pinned to tmp_path), no Ollama, no
network, no audio devices.
"""

import asyncio
import json
import sys
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.voice import hf_cache, models_store  # noqa: E402
from app.voice.asr import AsrError  # noqa: E402
from app.voice.asr import qwen3 as qw  # noqa: E402
from app.voice.asr.qwen3 import Qwen3AsrEngine, _LoadedModel  # noqa: E402

MODEL_ID = "Qwen/Qwen3-ASR-0.6B"


# ── shared fakes ──────────────────────────────────────────────────────


def make_spec(model=MODEL_ID, revision="main", language=None):
    return SimpleNamespace(
        provider="qwen3-asr", model=model, revision=revision, language=language
    )


class FakeTorch:
    """Module-like torch stand-in (dtype sentinels + no_grad + cuda)."""

    def __init__(self, cuda=False):
        self.bfloat16 = "bf16"
        self.float16 = "fp16"
        self.float32 = "fp32"
        self.cuda = SimpleNamespace(is_available=lambda: cuda)
        self._grad_depth = 0
        self.no_grad_frames = 0

    class _NoGrad:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def no_grad(self):
        self.no_grad_frames += 1
        return FakeTorch._NoGrad()


class FakeQwenAsr:
    """The official-runtime model wrapper."""

    def __init__(self, texts=None, error=None, empty=False):
        self.texts = list(texts or [])
        self.error = error
        self.empty = empty
        self.calls = []

    def transcribe(self, audio, context="", language=None):
        if self.error is not None:
            raise self.error
        self.calls.append({"audio": audio, "context": context,
                           "language": language})
        if self.empty:
            return []
        text = self.texts.pop(0) if self.texts else "transcribed text"
        return [SimpleNamespace(text=text)]


class FakeProcessor:
    """Callable processor (feature extraction + batch_decode)."""

    def __init__(self, generate_error=None, decoded=("hello world",)):
        self.generate_error = generate_error  # callable raising for kwargs
        self.decoded = list(decoded)
        self.calls = []

    def __call__(self, text="", audio=None, sampling_rate=None,
                 return_tensors=None):
        self.calls.append({"text": text, "audio": audio,
                           "sampling_rate": sampling_rate})
        return {"input_features": "FEAT", "attention_mask": "MASK"}

    def batch_decode(self, generated, skip_special_tokens=True):
        return list(self.decoded)


class FakeGenerateModel:
    def __init__(self, processor=None):
        self.processor = processor
        self.generate_kwargs_calls = []
        self.generate_key_calls = []

    def generate(self, **inputs):
        if self.processor is not None and self.processor.generate_error:
            # first attempt (full kwargs) raises; the retry with the
            # canonical feature key only succeeds
            if len(inputs) > 1 or "attention_mask" in inputs:
                self.generate_kwargs_calls.append(inputs)
                raise self.processor.generate_error
        self.generate_key_calls.append(inputs)
        return "TOKENS"


class FakePipeline:
    def __init__(self, text="piped text", result=None):
        self.text = text
        self.result = result
        self.calls = []

    def __call__(self, payload):
        self.calls.append(payload)
        if self.result is not None:
            return self.result
        return {"text": self.text}


@pytest.fixture(autouse=True)
def _fresh_engine_caches():
    """Isolate the module-level model caches between tests."""
    saved_cache = dict(qw._MODEL_CACHE)
    saved_failures = dict(qw._LOAD_FAILURES)
    qw._MODEL_CACHE.clear()
    qw._LOAD_FAILURES.clear()
    yield
    qw._MODEL_CACHE.clear()
    qw._MODEL_CACHE.update(saved_cache)
    qw._LOAD_FAILURES.clear()
    qw._LOAD_FAILURES.update(saved_failures)


@pytest.fixture
def fake_torch(monkeypatch):
    torch = FakeTorch()
    monkeypatch.setitem(sys.modules, "torch", torch)
    return torch


def _install_module(monkeypatch, name, **attrs):
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    monkeypatch.setitem(sys.modules, name, mod)
    return mod


def _pcm(ms: float, sample=b"\x01\x00") -> bytes:
    """ms of s16le 16 kHz mono PCM."""
    return sample * int(16000 * ms / 1000)


def _cached_engine(texts=None, **engine_kwargs):
    """Engine whose model is already 'loaded' via the module cache."""
    fake = FakeQwenAsr(texts=texts)
    qw._MODEL_CACHE[(MODEL_ID, "main")] = _LoadedModel(
        qwen_asr=fake, torch=FakeTorch(), source="fake-snapshot"
    )
    return Qwen3AsrEngine(make_spec(), **engine_kwargs), fake


# ── construction + status ─────────────────────────────────────────────


class TestEngineBasics:
    def test_spec_defaults(self):
        engine = Qwen3AsrEngine(make_spec(revision=None, language=""))
        assert engine._model_id == MODEL_ID
        assert engine._revision == "main"
        assert engine._language is None

    def test_spec_values(self):
        engine = Qwen3AsrEngine(make_spec(language=" English ",
                                          revision="v2"))
        assert engine._revision == "v2"
        assert engine._language == "English"

    def test_missing_spec_fields(self):
        engine = Qwen3AsrEngine(SimpleNamespace())
        assert engine._model_id == ""
        assert engine._revision == "main"
        assert engine._language is None

    def test_status_before_and_after_load(self):
        engine, _ = _cached_engine()
        status = engine.status()
        assert status == {
            "provider": "qwen3-asr",
            "model": MODEL_ID,
            "revision": "main",
            "loaded": False,
            "runtime": "qwen-asr/transformers",
            "partials": True,
        }
        asyncio.run(engine.warm_up())
        assert engine.status()["loaded"] is True

    def test_warm_up_uses_shared_cache(self):
        engine, fake = _cached_engine()
        asyncio.run(engine.warm_up())
        assert engine._loaded is qw._MODEL_CACHE[(MODEL_ID, "main")]
        # a second engine with the same model+revision shares the instance
        engine2 = Qwen3AsrEngine(make_spec())
        asyncio.run(engine2.warm_up())
        assert engine2._loaded is engine._loaded


# ── streaming surface against a cached fake model ─────────────────────


class TestStreaming:
    def test_start_feed_finish_roundtrip(self):
        engine, fake = _cached_engine(
            texts=["final transcript", "final transcript"]
        )

        async def run():
            await engine.start_stream()
            await engine.feed_audio(_pcm(500))
            partial = await engine.get_partial()
            assert partial == "final transcript"
            result = await engine.finish_stream()
            assert result == "final transcript"

        asyncio.run(run())
        # one partial + one final transcription, full buffer each time
        assert len(fake.calls) == 2
        audio, sr = fake.calls[0]["audio"]
        assert sr == qw.ASR_SAMPLE_RATE
        assert len(audio) == 8000  # 500 ms of 16 kHz samples
        # the context prompt is empty and the language is passed through
        assert fake.calls[0]["context"] == ""
        assert fake.calls[0]["language"] is None

    def test_language_passed_to_transcribe(self):
        engine, fake = _cached_engine()
        engine._language = "fr"

        async def run():
            await engine.start_stream()
            await engine.feed_audio(_pcm(500))
            await engine.finish_stream()

        asyncio.run(run())
        assert fake.calls[0]["language"] == "fr"

    def test_feed_before_start_is_dropped(self):
        engine, fake = _cached_engine()

        async def run():
            await engine.feed_audio(_pcm(500))  # stream not open → dropped
            await engine.start_stream()
            assert await engine.finish_stream() == ""

        asyncio.run(run())
        assert fake.calls == []

    def test_finish_with_empty_buffer(self):
        engine, fake = _cached_engine()

        async def run():
            await engine.start_stream()
            assert await engine.finish_stream() == ""

        asyncio.run(run())
        assert fake.calls == []  # nothing buffered → no inference

    def test_get_partial_before_start(self):
        engine, _ = _cached_engine()

        async def run():
            assert await engine.get_partial() == ""

        asyncio.run(run())

    def test_partial_below_min_audio_returns_last(self):
        engine, fake = _cached_engine(texts=["ignored"])
        engine._min_partial_ms = 400  # default

        async def run():
            await engine.start_stream()
            await engine.feed_audio(_pcm(399))  # just under 400 ms
            assert await engine.get_partial() == ""
            assert fake.calls == []

        asyncio.run(run())

    def test_partial_at_min_audio_threshold(self):
        engine, fake = _cached_engine(texts=["first partial"])
        engine._min_partial_ms = 400

        async def run():
            await engine.start_stream()
            await engine.feed_audio(_pcm(400))  # exactly 400 ms → proceeds
            assert await engine.get_partial() == "first partial"
            assert len(fake.calls) == 1

        asyncio.run(run())

    def test_partial_interval_throttling(self):
        engine, fake = _cached_engine(texts=["p1", "p2"])

        async def run():
            await engine.start_stream()
            await engine.feed_audio(_pcm(500))
            assert await engine.get_partial() == "p1"
            # immediately again → throttled by partial_interval_s
            await engine.feed_audio(_pcm(100))
            assert await engine.get_partial() == "p1"
            assert len(fake.calls) == 1  # no second inference
            # once the interval has elapsed a new partial is served
            engine._last_partial_at -= engine._partial_interval_s + 0.01
            assert await engine.get_partial() == "p2"
            assert len(fake.calls) == 2

        asyncio.run(run())

    def test_partial_skipped_when_transcription_empty(self):
        engine, fake = _cached_engine(texts=[""])

        async def run():
            await engine.start_stream()
            await engine.feed_audio(_pcm(500))
            assert await engine.get_partial() == ""

        asyncio.run(run())
        assert len(fake.calls) == 1

    def test_partial_max_sec_cap(self):
        engine, fake = _cached_engine()
        engine._partial_max_sec = 2.0

        async def run():
            await engine.start_stream()
            await engine.feed_audio(_pcm(2500))  # > 2 s buffered
            assert await engine.get_partial() == ""
            assert fake.calls == []  # re-transcription skipped, not queued

        asyncio.run(run())

    def test_partial_returns_last_text_when_lock_busy(self):
        engine, fake = _cached_engine(texts=["busy text"])

        async def run():
            await engine.start_stream()
            await engine.feed_audio(_pcm(500))
            assert await engine.get_partial() == "busy text"  # real partial
            # make the interval look elapsed so the busy-lock branch —
            # not the throttle — is what short-circuits the next call
            engine._last_partial_at -= engine._partial_interval_s + 0.01
            await engine._lock.acquire()  # simulate an in-flight finish
            try:
                # transcription already running → serve the last partial
                assert await engine.get_partial() == "busy text"
            finally:
                engine._lock.release()

        asyncio.run(run())
        assert len(fake.calls) == 1  # the busy partial did not re-transcribe

    def test_cancel_drops_buffer(self):
        engine, fake = _cached_engine()

        async def run():
            await engine.start_stream()
            await engine.feed_audio(_pcm(500))
            await engine.cancel()
            assert await engine.finish_stream() == ""  # buffer cleared

        asyncio.run(run())
        assert fake.calls == []
        # after cancel the stream is closed — later audio is dropped
        assert asyncio.run(_feed_closed(engine)) is None

    def test_feed_after_finish_is_dropped(self):
        engine, fake = _cached_engine()

        async def run():
            await engine.start_stream()
            await engine.feed_audio(_pcm(500))
            await engine.finish_stream()
            await engine.feed_audio(_pcm(500))  # stream closed → dropped
            # finish again → empty (nothing buffered)
            assert await engine.finish_stream() == ""

        asyncio.run(run())
        assert len(fake.calls) == 1

    def test_start_stream_resets_between_utterances(self):
        engine, fake = _cached_engine(texts=["one", "two"])

        async def run():
            await engine.start_stream()
            await engine.feed_audio(_pcm(600))
            await engine.feed_audio(_pcm(600))
            assert await engine.finish_stream() == "one"
            await engine.start_stream()  # fresh utterance
            await engine.feed_audio(_pcm(300))
            assert await engine.finish_stream() == "two"

        asyncio.run(run())
        assert len(fake.calls) == 2
        audio, _ = fake.calls[0]["audio"]
        assert len(audio) == 19200  # 1200 ms of samples

    def test_transcription_failure_raises_asr_error(self):
        engine, _ = _cached_engine()
        engine._loaded = _LoadedModel(
            qwen_asr=FakeQwenAsr(error=RuntimeError("boom"))
        )

        async def run():
            await engine.start_stream()
            await engine.feed_audio(_pcm(500))
            with pytest.raises(AsrError) as exc:
                await engine.finish_stream()
            assert exc.value.code == "asr_model_error"
            assert not exc.value.fatal

        asyncio.run(run())


async def _feed_closed(engine):
    await engine.feed_audio(b"\x00\x00")
    return None


# ── _transcribe runtime shapes ────────────────────────────────────────


class TestTranscribePaths:
    def _engine(self):
        return Qwen3AsrEngine(make_spec())

    def test_not_loaded_raises(self):
        engine = self._engine()
        with pytest.raises(AsrError) as exc:
            engine._transcribe(_pcm(100))
        assert exc.value.code == "asr_not_loaded"

    def test_empty_pcm_returns_empty(self):
        engine = self._engine()
        engine._loaded = _LoadedModel(qwen_asr=FakeQwenAsr())
        assert engine._transcribe(b"") == ""

    def test_qwen_asr_empty_results(self):
        engine = self._engine()
        engine._loaded = _LoadedModel(qwen_asr=FakeQwenAsr(empty=True))
        assert engine._transcribe(_pcm(100)) == ""

    def test_qwen_asr_none_text(self):
        fake = FakeQwenAsr()
        fake.transcribe = lambda audio, context="", language=None: [
            SimpleNamespace(text=None)
        ]
        engine = self._engine()
        engine._loaded = _LoadedModel(qwen_asr=fake)
        assert engine._transcribe(_pcm(100)) == ""

    def test_pipeline_dict_result(self):
        engine = self._engine()
        pipe = FakePipeline(text="piped text")
        engine._loaded = _LoadedModel(pipeline=pipe)
        assert engine._transcribe(_pcm(100)) == "piped text"
        assert pipe.calls[0]["sampling_rate"] == qw.ASR_SAMPLE_RATE

    def test_pipeline_non_dict_result(self):
        engine = self._engine()
        engine._loaded = _LoadedModel(pipeline=FakePipeline(result="junk"))
        assert engine._transcribe(_pcm(100)) == ""

    def test_processor_model_path_with_no_grad(self):
        engine = self._engine()
        torch = FakeTorch()
        processor = FakeProcessor()
        model = FakeGenerateModel()
        engine._loaded = _LoadedModel(
            processor=processor, model=model, torch=torch
        )
        assert engine._transcribe(_pcm(100)) == "hello world"
        assert torch.no_grad_frames == 1
        # generate got the full processor inputs
        assert model.generate_key_calls[0] == {
            "input_features": "FEAT", "attention_mask": "MASK"
        }

    def test_processor_model_typeerror_retry(self):
        engine = self._engine()
        processor = FakeProcessor(generate_error=TypeError("unexpected kwarg"))
        model = FakeGenerateModel(processor=processor)
        engine._loaded = _LoadedModel(
            processor=processor, model=model, torch=FakeTorch()
        )
        assert engine._transcribe(_pcm(100)) == "hello world"
        # the first call raised, the retry used the canonical key only
        assert len(model.generate_kwargs_calls) == 1
        assert model.generate_key_calls[-1] == {"input_features": "FEAT"}

    def test_processor_model_typeerror_without_key_raises(self):
        engine = self._engine()

        class NoKeyProcessor(FakeProcessor):
            def __call__(self, **kwargs):
                return {"unrelated": 1}

        class AlwaysTypeErrorModel:
            def generate(self, **inputs):
                raise TypeError("unexpected keyword")

        engine._loaded = _LoadedModel(
            processor=NoKeyProcessor(),
            model=AlwaysTypeErrorModel(),
            torch=FakeTorch(),
        )
        # inputs carry no recognizable feature key → the TypeError
        # propagates and is wrapped
        with pytest.raises(AsrError) as exc:
            engine._transcribe(_pcm(100))
        assert exc.value.code == "asr_model_error"

    def test_batch_decode_empty(self):
        engine = self._engine()
        processor = FakeProcessor(decoded=[])
        engine._loaded = _LoadedModel(
            processor=processor, model=FakeGenerateModel(), torch=FakeTorch()
        )
        assert engine._transcribe(_pcm(100)) == ""

    def test_transcription_asr_error_propagates_unwrapped(self):
        engine = self._engine()
        fake = FakeQwenAsr(error=AsrError("inner failure", code="inner"))
        engine._loaded = _LoadedModel(qwen_asr=fake)
        with pytest.raises(AsrError) as exc:
            engine._transcribe(_pcm(100))
        assert exc.value.code == "inner"  # not re-wrapped as asr_model_error

    def test_generic_exception_wrapped(self):
        engine = self._engine()
        fake = FakeQwenAsr(error=ValueError("core blew up"))
        engine._loaded = _LoadedModel(qwen_asr=fake)
        with pytest.raises(AsrError) as exc:
            engine._transcribe(_pcm(100))
        assert "core blew up" in str(exc.value)
        assert exc.value.code == "asr_model_error"


# ── dtype policy ──────────────────────────────────────────────────────


class TestResolveDtype:
    def test_explicit_aliases(self, monkeypatch):
        cases = {
            "bfloat16": "bfloat16",
            "bf16": "bfloat16",
            "float16": "float16",
            "fp16": "float16",
            "float32": "float32",
            "fp32": "float32",
        }
        for raw, want in cases.items():
            monkeypatch.setattr(settings, "VOICE_ASR_DTYPE", raw)
            assert qw._resolve_dtype() == want, raw

    def test_auto_without_torch_is_bfloat16(self, monkeypatch):
        monkeypatch.setattr(settings, "VOICE_ASR_DTYPE", "auto")
        # torch is genuinely not installed in this environment
        assert "torch" not in sys.modules
        assert qw._resolve_dtype() == "bfloat16"

    def test_auto_with_cuda_is_float16(self, monkeypatch, fake_torch):
        fake_torch.cuda = SimpleNamespace(is_available=lambda: True)
        monkeypatch.setattr(settings, "VOICE_ASR_DTYPE", "auto")
        assert qw._resolve_dtype() == "float16"

    def test_auto_cpu_torch_is_bfloat16(self, monkeypatch, fake_torch):
        monkeypatch.setattr(settings, "VOICE_ASR_DTYPE", "auto")
        assert qw._resolve_dtype() == "bfloat16"

    def test_unknown_value_falls_back_to_auto(self, monkeypatch):
        monkeypatch.setattr(settings, "VOICE_ASR_DTYPE", "quantum")
        assert qw._resolve_dtype() == "bfloat16"


# ── local snapshot resolution ─────────────────────────────────────────


class TestLocalSnapshotDir:
    def test_legacy_manifest_layout(self, monkeypatch, tmp_path):
        models = tmp_path / "models"
        snap = models / "asr" / "Qwen3-ASR-0.6B"
        snap.mkdir(parents=True)
        (snap / "weights.bin").write_bytes(b"x")
        (models / models_store.MANIFEST_FILENAME).write_text(json.dumps({
            "asr": {"model": MODEL_ID, "complete": True,
                    "path": "asr/Qwen3-ASR-0.6B"},
        }))
        monkeypatch.setattr(models_store, "MODELS_DIR", models)
        assert qw._local_snapshot_dir(MODEL_ID) == str(snap)

    def test_legacy_manifest_empty_dir_is_rejected(self, monkeypatch, tmp_path):
        models = tmp_path / "models"
        snap = models / "asr" / "Qwen3-ASR-0.6B"
        snap.mkdir(parents=True)  # exists but empty
        (models / models_store.MANIFEST_FILENAME).write_text(json.dumps({
            "asr": {"model": MODEL_ID, "complete": True,
                    "path": "asr/Qwen3-ASR-0.6B"},
        }))
        monkeypatch.setattr(models_store, "MODELS_DIR", models)
        # falls through to the hub-cache probe → nothing cached → None
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE",
                            str(tmp_path / "hf"))
        assert qw._local_snapshot_dir(MODEL_ID) is None

    def test_manifest_for_other_model_ignored(self, monkeypatch, tmp_path):
        models = tmp_path / "models"
        snap = models / "asr" / "Other"
        snap.mkdir(parents=True)
        (snap / "w").write_bytes(b"x")
        (models / models_store.MANIFEST_FILENAME).write_text(json.dumps({
            "asr": {"model": "Other/Model", "complete": True, "path": "asr/Other"},
        }))
        monkeypatch.setattr(models_store, "MODELS_DIR", models)
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE",
                            str(tmp_path / "hf"))
        assert qw._local_snapshot_dir(MODEL_ID) is None

    def test_hf_hub_cache_without_manifest(self, monkeypatch, tmp_path):
        monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path / "models")
        hf = tmp_path / "hf"
        repo = hf / "hub" / "models--Qwen--Qwen3-ASR-0.6B"
        (repo / "refs").mkdir(parents=True)
        (repo / "refs" / "main").write_text("abc123")
        snap = repo / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        (snap / "f.bin").write_bytes(b"x")
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(hf))
        assert qw._local_snapshot_dir(MODEL_ID) == str(snap)

    def test_hf_store_manifest_entry(self, monkeypatch, tmp_path):
        """A store:"hf" manifest entry resolves through asr_snapshot_dir."""
        models = tmp_path / "models"
        models.mkdir()
        (models / models_store.MANIFEST_FILENAME).write_text(json.dumps({
            "asr": {"model": MODEL_ID, "complete": True, "store": "hf",
                    "hf_repos": {MODEL_ID: "main"}},
        }))
        monkeypatch.setattr(models_store, "MODELS_DIR", models)
        hf = tmp_path / "hf"
        snap = hf / "hub" / "models--Qwen--Qwen3-ASR-0.6B" / "main"
        snap.mkdir(parents=True)
        (snap / "f.bin").write_bytes(b"x")
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(hf))
        assert qw._local_snapshot_dir(MODEL_ID) == str(snap)

    def test_hf_cache_probe_failure_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path / "models")

        def boom():
            raise RuntimeError("hub cache unreadable")

        monkeypatch.setattr(hf_cache, "ensure_hf_env", boom)
        # the hub probe must never break loading → None (repo-id load)
        assert qw._local_snapshot_dir(MODEL_ID) is None

    def test_manifest_probe_failure_falls_back_to_cache(
        self, monkeypatch, tmp_path
    ):
        def boom():
            raise RuntimeError("manifest unreadable")

        monkeypatch.setattr(models_store, "load_manifest", boom)
        hf = tmp_path / "hf"
        repo = hf / "hub" / "models--Qwen--Qwen3-ASR-0.6B"
        (repo / "refs").mkdir(parents=True)
        (repo / "refs" / "main").write_text("abc123")
        snap = repo / "snapshots" / "abc123"
        snap.mkdir(parents=True)
        (snap / "f.bin").write_bytes(b"x")
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(hf))
        # the manifest probe must never break loading — the hub cache wins
        assert qw._local_snapshot_dir(MODEL_ID) == str(snap)

    def test_nothing_cached_returns_none(self, monkeypatch, tmp_path):
        monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path / "models")
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
        assert qw._local_snapshot_dir(MODEL_ID) is None


# ── model loading ─────────────────────────────────────────────────────


class TestLoadModelFrom:
    def _engine(self, revision="main"):
        return Qwen3AsrEngine(make_spec(revision=revision))

    def test_official_qwen_asr_runtime(self, monkeypatch, fake_torch):
        model_obj = FakeQwenAsr()
        captured = {}

        def from_pretrained(source, dtype=None, low_cpu_mem_usage=False):
            captured.update(source=source, dtype=dtype,
                            low_cpu_mem_usage=low_cpu_mem_usage)
            return model_obj

        _install_module(
            monkeypatch, "qwen_asr",
            Qwen3ASRModel=SimpleNamespace(from_pretrained=from_pretrained),
        )
        engine = self._engine()
        loaded = engine._load_model_from("/snap/dir")
        assert loaded.qwen_asr is model_obj
        assert loaded.source == "/snap/dir"
        assert loaded.torch is fake_torch
        # dtype policy resolved (auto without CUDA → bfloat16 sentinel),
        # low-CPU load requested
        assert captured == {"source": "/snap/dir", "dtype": "bf16",
                            "low_cpu_mem_usage": True}

    def test_qwen_asr_dtype_from_policy(self, monkeypatch, fake_torch):
        captured = {}

        def from_pretrained(source, dtype=None, low_cpu_mem_usage=False):
            captured["dtype"] = dtype
            return FakeQwenAsr()

        _install_module(
            monkeypatch, "qwen_asr",
            Qwen3ASRModel=SimpleNamespace(from_pretrained=from_pretrained),
        )
        monkeypatch.setattr(settings, "VOICE_ASR_DTYPE", "fp16")
        engine = self._engine()
        engine._load_model_from(MODEL_ID)
        assert captured["dtype"] == "fp16"  # FakeTorch.float16 sentinel

    def test_qwen_asr_without_torch_is_runtime_missing(self, monkeypatch):
        assert "torch" not in sys.modules
        _install_module(
            monkeypatch, "qwen_asr",
            Qwen3ASRModel=SimpleNamespace(from_pretrained=lambda *a, **k: object()),
        )
        engine = self._engine()
        with pytest.raises(AsrError) as exc:
            engine._load_model_from("/snap")
        assert exc.value.code == "asr_runtime_missing"
        assert "torch" in str(exc.value)

    def test_qwen_asr_failure_falls_back_to_transformers(
        self, monkeypatch, fake_torch
    ):
        def broken_from_pretrained(*a, **k):
            raise RuntimeError("checkpoint layout mismatch")

        _install_module(
            monkeypatch, "qwen_asr",
            Qwen3ASRModel=SimpleNamespace(
                from_pretrained=broken_from_pretrained,
            ),
        )
        transformer_objs = self._transformers_ok(monkeypatch)
        engine = self._engine()
        loaded = engine._load_model_from(MODEL_ID)
        assert loaded.qwen_asr is None
        assert loaded.model is transformer_objs.model_obj
        assert loaded.processor is transformer_objs.processor_obj

    @staticmethod
    def _transformers_ok(monkeypatch, dedicated_name="Qwen3ASRForConditionalGeneration"):
        processor_obj = FakeProcessor()
        model_obj = SimpleNamespace(eval=lambda: None)

        class Dedicated:
            @staticmethod
            def from_pretrained(source, **kw):
                return model_obj

        class AutoProc:
            @staticmethod
            def from_pretrained(source, **kw):
                return processor_obj

        class AutoModel:
            @staticmethod
            def from_pretrained(source, **kw):
                raise AssertionError("should not be reached")

        mod = _install_module(
            monkeypatch, "transformers",
            AutoProcessor=AutoProc,
            AutoModelForSpeechSeq2Seq=AutoModel,
        )
        setattr(mod, dedicated_name, Dedicated)
        mod._objs = SimpleNamespace(
            processor_obj=processor_obj, model_obj=model_obj
        )
        return mod._objs

    def test_transformers_dedicated_class(self, monkeypatch, fake_torch):
        objs = self._transformers_ok(monkeypatch)
        engine = self._engine()
        loaded = engine._load_model_from("/snap")
        assert loaded.model is objs.model_obj
        assert loaded.source == "/snap"

    def test_transformers_generic_speech_class(self, monkeypatch, fake_torch):
        generic_model = SimpleNamespace(eval=lambda: None)

        class AutoProc:
            @staticmethod
            def from_pretrained(source, **kw):
                return FakeProcessor()

        class AutoModel:
            @staticmethod
            def from_pretrained(source, **kw):
                return generic_model

        _install_module(
            monkeypatch, "transformers",
            AutoProcessor=AutoProc,
            AutoModelForSpeechSeq2Seq=AutoModel,
        )
        engine = self._engine()
        loaded = engine._load_model_from("/snap")
        assert loaded.model is generic_model

    def test_transformers_pipeline_last_resort(self, monkeypatch, fake_torch):
        pipe_obj = FakePipeline()
        pipe_calls = []

        class Failing:
            @staticmethod
            def from_pretrained(source, **kw):
                raise RuntimeError("wrong class for this snapshot")

        class AutoProc:
            @staticmethod
            def from_pretrained(source, **kw):
                return FakeProcessor()

        class AutoModel:
            @staticmethod
            def from_pretrained(source, **kw):
                raise RuntimeError("nope")

        def hf_pipeline(task, model=None, **kw):
            pipe_calls.append((task, model, kw))
            return pipe_obj

        mod = _install_module(
            monkeypatch, "transformers",
            AutoProcessor=AutoProc,
            AutoModelForSpeechSeq2Seq=AutoModel,
            pipeline=hf_pipeline,
        )
        for cls_name in (
            "Qwen3ASRForConditionalGeneration", "Qwen3AsrForConditionalGeneration",
            "Qwen3ASRForCausalLM", "Qwen3AsrForCausalLM",
            "Qwen3ASRModel", "Qwen3AsrModel",
        ):
            setattr(mod, cls_name, Failing)
        engine = self._engine()
        loaded = engine._load_model_from("/snap")
        assert loaded.pipeline is pipe_obj
        assert loaded.model is None
        assert loaded.processor is not None
        assert pipe_calls == [("automatic-speech-recognition", "/snap", {})]

    def test_processor_asr_error_not_rewrapped(self, monkeypatch, fake_torch):
        class AutoProc:
            @staticmethod
            def from_pretrained(source, **kw):
                raise AsrError("custom asr failure", code="custom_code")

        _install_module(monkeypatch, "transformers", AutoProcessor=AutoProc)
        engine = self._engine()
        # an AsrError from the load must propagate as-is, not be wrapped
        with pytest.raises(AsrError) as exc:
            engine._load_model_from("/snap")
        assert exc.value.code == "custom_code"
        assert "failed to load Qwen3-ASR" not in str(exc.value)

    def test_runtime_missing_when_no_modules(self, monkeypatch):
        # neither qwen_asr nor transformers/torch importable
        engine = self._engine()
        with pytest.raises(AsrError) as exc:
            engine._load_model_from("/snap")
        assert exc.value.code == "asr_runtime_missing"
        assert "qwen-asr" in str(exc.value)

    def test_processor_failure_wrapped(self, monkeypatch, fake_torch):
        class AutoProc:
            @staticmethod
            def from_pretrained(source, **kw):
                raise RuntimeError("corrupt checkpoint")

        _install_module(monkeypatch, "transformers", AutoProcessor=AutoProc)
        engine = self._engine()
        with pytest.raises(AsrError) as exc:
            engine._load_model_from("/snap")
        assert exc.value.code == "asr_model_error"
        assert MODEL_ID in str(exc.value)

    def test_revision_kwarg_only_for_repo_id_loads(self, monkeypatch, fake_torch):
        captured = {}

        class AutoProc:
            @staticmethod
            def from_pretrained(source, **kw):
                captured["processor"] = (source, kw)
                return FakeProcessor()

        class Dedicated:
            @staticmethod
            def from_pretrained(source, **kw):
                captured["model"] = (source, kw)
                return SimpleNamespace(eval=lambda: None)

        mod = _install_module(
            monkeypatch, "transformers", AutoProcessor=AutoProc,
        )
        setattr(mod, "Qwen3ASRForConditionalGeneration", Dedicated)
        # source == model_id and revision != local → revision is pinned
        engine = self._engine(revision="v2")
        engine._load_model_from(MODEL_ID)
        assert captured["model"][1] == {"revision": "v2"}
        # a local snapshot dir load does NOT pin the revision
        engine = self._engine(revision="v2")
        engine._load_model_from("/snap/dir")
        assert captured["model"][1] == {}
        # revision "local" never pins
        engine = self._engine(revision="local")
        engine._load_model_from(MODEL_ID)
        assert captured["model"][1] == {}


class TestResolveModelCls:
    def _engine(self):
        return Qwen3AsrEngine(make_spec())

    @staticmethod
    def _transformers(monkeypatch, classes, auto=None):
        mod = _install_module(monkeypatch, "transformers")
        for name, obj in classes.items():
            setattr(mod, name, obj)
        if auto is not None:
            mod.AutoModelForSpeechSeq2Seq = auto
        return mod

    def test_first_dedicated_class_wins(self, monkeypatch):
        first = SimpleNamespace(eval=lambda: None)

        class C1:
            @staticmethod
            def from_pretrained(source, **kw):
                return first

        self._transformers(
            monkeypatch, {"Qwen3ASRForConditionalGeneration": C1}
        )
        assert self._engine()._resolve_model_cls("src", {}) is first

    def test_failing_classes_skipped(self, monkeypatch):
        second = SimpleNamespace(eval=lambda: None)

        class Bad:
            @staticmethod
            def from_pretrained(source, **kw):
                raise RuntimeError("layout mismatch")

        class C2:
            @staticmethod
            def from_pretrained(source, **kw):
                return second

        mod = self._transformers(monkeypatch, {
            "Qwen3ASRForConditionalGeneration": Bad,
            "Qwen3AsrForConditionalGeneration": C2,
        })
        assert mod.Qwen3AsrForConditionalGeneration is C2
        assert self._engine()._resolve_model_cls("src", {}) is second

    def test_generic_auto_class_fallback(self, monkeypatch):
        generic = SimpleNamespace(eval=lambda: None)

        class Bad:
            @staticmethod
            def from_pretrained(source, **kw):
                raise RuntimeError("nope")

        class AutoModel:
            @staticmethod
            def from_pretrained(source, **kw):
                return generic

        mod = self._transformers(
            monkeypatch, {"Qwen3ASRModel": Bad}, auto=AutoModel
        )
        assert mod.AutoModelForSpeechSeq2Seq is AutoModel
        assert self._engine()._resolve_model_cls("src", {}) is generic

    def test_everything_fails_returns_none(self, monkeypatch):
        class Bad:
            @staticmethod
            def from_pretrained(source, **kw):
                raise RuntimeError("nope")

        class AutoModel:
            @staticmethod
            def from_pretrained(source, **kw):
                raise RuntimeError("also nope")

        self._transformers(
            monkeypatch, {"Qwen3ASRModel": Bad}, auto=AutoModel
        )
        assert self._engine()._resolve_model_cls("src", {}) is None

    def test_load_kwargs_forwarded(self, monkeypatch):
        captured = {}

        class C1:
            @staticmethod
            def from_pretrained(source, **kw):
                captured.update(kw)
                return SimpleNamespace(eval=lambda: None)

        self._transformers(
            monkeypatch, {"Qwen3ASRForConditionalGeneration": C1}
        )
        self._engine()._resolve_model_cls("src", {"revision": "v9"})
        assert captured == {"revision": "v9"}


class TestEnsureModel:
    def test_load_success_populates_shared_cache(self, monkeypatch):
        engine = Qwen3AsrEngine(make_spec())
        calls = []
        loaded = _LoadedModel(qwen_asr=FakeQwenAsr(), source="s1")

        def fake_load():
            calls.append(1)
            return loaded

        engine._load_model_sync = fake_load

        async def run():
            first = await engine._ensure_model()
            assert first is loaded
            # second engine, same key → shared instance, no second load
            engine2 = Qwen3AsrEngine(make_spec())
            engine2._load_model_sync = fake_load
            again = await engine2._ensure_model()
            assert again is loaded
            assert calls == [1]

        asyncio.run(run())
        assert qw._MODEL_CACHE[(MODEL_ID, "main")] is loaded
        assert (MODEL_ID, "main") not in qw._LOAD_FAILURES

    def test_load_failure_is_negatively_cached(self, monkeypatch):
        engine = Qwen3AsrEngine(make_spec())

        def bad_load():
            raise RuntimeError("weights unreadable")

        engine._load_model_sync = bad_load

        async def run():
            with pytest.raises(RuntimeError, match="weights unreadable"):
                await engine._ensure_model()
            # the failure is remembered — no second load attempt
            engine._load_model_sync = lambda: pytest.fail(
                "must not reload within the TTL"
            )
            with pytest.raises(AsrError) as exc:
                await engine._ensure_model()
            assert exc.value.code == "asr_load_failed_recently"
            assert not exc.value.fatal
            assert "setup wizard" in str(exc.value)

        asyncio.run(run())
        assert (MODEL_ID, "main") in qw._LOAD_FAILURES

    def test_negative_cache_ttl_expiry_allows_retry(self, monkeypatch):
        engine = Qwen3AsrEngine(make_spec())
        loaded = _LoadedModel(qwen_asr=FakeQwenAsr())
        engine._load_model_sync = lambda: loaded
        # a failure older than the TTL no longer suppresses the load
        qw._LOAD_FAILURES[(MODEL_ID, "main")] = (
            time.monotonic() - qw._LOAD_FAILURE_TTL_S - 1
        )

        async def run():
            assert await engine._ensure_model() is loaded

        asyncio.run(run())
        assert (MODEL_ID, "main") not in qw._LOAD_FAILURES

    def test_load_timeout_is_recoverable_and_cached(self, monkeypatch):
        engine = Qwen3AsrEngine(make_spec())
        engine._load_model_sync = lambda: _LoadedModel(
            qwen_asr=FakeQwenAsr()
        )

        async def instant_timeout(coro, timeout):
            coro.close()
            raise asyncio.TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", instant_timeout)

        async def run():
            with pytest.raises(AsrError) as exc:
                await engine._ensure_model()
            assert exc.value.code == "asr_model_load_timeout"
            assert not exc.value.fatal
            assert "setup wizard" in str(exc.value)

        asyncio.run(run())
        assert (MODEL_ID, "main") in qw._LOAD_FAILURES

    def test_load_timeout_bounded_by_settings(self, monkeypatch):
        captured = {}

        async def fake_wait_for(coro, timeout):
            captured["timeout"] = timeout
            coro.close()
            raise asyncio.TimeoutError

        monkeypatch.setattr(asyncio, "wait_for", fake_wait_for)
        monkeypatch.setattr(settings, "VOICE_ASR_LOAD_TIMEOUT_SEC", 12)
        engine = Qwen3AsrEngine(make_spec())
        engine._load_model_sync = lambda: _LoadedModel()

        async def run():
            with pytest.raises(AsrError):
                await engine._ensure_model()

        asyncio.run(run())
        assert captured["timeout"] == 12.0
        # sub-second settings are clamped up to 1 s
        monkeypatch.setattr(settings, "VOICE_ASR_LOAD_TIMEOUT_SEC", 0)
        qw._LOAD_FAILURES.clear()  # the first timeout negatively cached
        engine = Qwen3AsrEngine(make_spec())
        engine._load_model_sync = lambda: _LoadedModel()

        async def run2():
            with pytest.raises(AsrError):
                await engine._ensure_model()

        asyncio.run(run2())
        assert captured["timeout"] == 1.0


class TestLoadModelSync:
    def test_snapshot_dir_wins_over_repo_id(self, monkeypatch, tmp_path):
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
        monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path / "models")
        engine = Qwen3AsrEngine(make_spec())
        captured = {}

        def fake_load_from(source):
            captured["source"] = source
            return _LoadedModel(source=str(source))

        monkeypatch.setattr(engine, "_load_model_from", fake_load_from)
        monkeypatch.setattr(qw, "_local_snapshot_dir", lambda mid: "/snap")
        loaded = engine._load_model_sync()
        assert captured["source"] == "/snap"
        assert loaded.source == "/snap"

    def test_repo_id_fallback_when_no_snapshot(self, monkeypatch, tmp_path):
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
        monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path / "models")
        engine = Qwen3AsrEngine(make_spec())
        captured = {}

        def fake_load_from(source):
            captured["source"] = source
            return _LoadedModel(source=str(source))

        monkeypatch.setattr(engine, "_load_model_from", fake_load_from)
        monkeypatch.setattr(qw, "_local_snapshot_dir", lambda mid: None)
        engine._load_model_sync()
        assert captured["source"] == MODEL_ID

    def test_runs_with_hub_offline(self, monkeypatch, tmp_path):
        """The load must happen inside offline_hub() — no network."""
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
        monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path / "models")
        engine = Qwen3AsrEngine(make_spec())
        seen = {}

        def fake_load_from(source):
            import os

            seen["offline"] = os.environ.get("HF_HUB_OFFLINE")
            return _LoadedModel(source=str(source))

        monkeypatch.setattr(engine, "_load_model_from", fake_load_from)
        monkeypatch.setattr(qw, "_local_snapshot_dir", lambda mid: None)
        engine._load_model_sync()
        assert seen["offline"] == "1"
        import os

        assert os.environ.get("HF_HUB_OFFLINE") is None  # restored
