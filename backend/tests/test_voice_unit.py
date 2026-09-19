"""Unit tests for the voice pipeline building blocks.

Covers the deterministic, no-IO pieces:
- VoiceStateMachine: the happy-path chain, illegal transitions, recovery,
  STOPPING terminality, and the emitted `state` frame shape.
- RollingBuffer: pre-roll retention, capacity enforcement, snapshot
  ordering (oldest → newest).
- SentenceChunker: sentence-boundary flush at min_chars, max_chars hard
  break, flush() remainder, reset(), and the flush timeout.
- clean_for_tts: fenced code / inline code / markdown stripping.
- AEC: NlmsAec suppresses a synthetic echo (mic = delayed far-end copy)
  by ≥6 dB after adaptation; PassThroughAec is an identity.
- models_store: manifest validation with a tmp_path models dir
  (installed/valid/ready transitions; a changed model → not valid).

No external services are required (no DB, no Ollama, no webrtcvad — the
EnergyVad/NlmsAec numpy fallbacks are exercised). See conftest.py for the
placeholder DATABASE_URL strategy.
"""

import math
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402
from app.api import voice as voice_api  # noqa: E402
from app.voice import models_store  # noqa: E402
from app.voice.aec import NlmsAec, PassThroughAec  # noqa: E402
from app.voice.audio import RollingBuffer  # noqa: E402
from app.voice.state import (  # noqa: E402
    InvalidTransition,
    VoiceState,
    VoiceStateMachine,
)
from app.voice.tts.chunker import SentenceChunker, clean_for_tts  # noqa: E402

# ══════════════════════════════════════════════════════════════════════
# State machine
# ══════════════════════════════════════════════════════════════════════


class TestVoiceStateMachine:
    def test_happy_path_full_cycle(self):
        sm = VoiceStateMachine()
        # start → LISTENING → PROCESSING → SPEAKING → INTERRUPTING → LISTENING
        sm.transition(VoiceState.LISTENING, "start")
        sm.transition(VoiceState.PROCESSING, "asr_final")
        sm.transition(VoiceState.SPEAKING, "first_tts_audio")
        sm.transition(VoiceState.INTERRUPTING, "barge_in")
        sm.transition(VoiceState.LISTENING, "barge_in_complete")
        assert sm.state == VoiceState.LISTENING
        history = sm.history
        assert [t.to_state for t in history] == [
            VoiceState.LISTENING,
            VoiceState.PROCESSING,
            VoiceState.SPEAKING,
            VoiceState.INTERRUPTING,
            VoiceState.LISTENING,
        ]

    def test_turn_without_tts_goes_straight_to_listening(self):
        sm = VoiceStateMachine()
        sm.transition(VoiceState.LISTENING, "start")
        sm.transition(VoiceState.PROCESSING, "asr_final")
        sm.transition(VoiceState.LISTENING, "turn_complete")
        assert sm.state == VoiceState.LISTENING

    def test_illegal_transitions_raise(self):
        sm = VoiceStateMachine()
        with pytest.raises(InvalidTransition):
            sm.transition(VoiceState.SPEAKING)  # IDLE → SPEAKING
        sm.transition(VoiceState.LISTENING, "start")
        with pytest.raises(InvalidTransition):
            sm.transition(VoiceState.SPEAKING)  # LISTENING → SPEAKING
        sm.transition(VoiceState.PROCESSING, "asr_final")
        with pytest.raises(InvalidTransition):
            sm.transition(VoiceState.PROCESSING)  # self-loop not allowed

    def test_recover_to_listening_from_recoverable_states(self):
        for state in (
            VoiceState.PROCESSING,
            VoiceState.SPEAKING,
            VoiceState.INTERRUPTING,
            VoiceState.ERROR,
        ):
            sm = VoiceStateMachine()
            # Walk to the state under test through legal edges.
            if state is VoiceState.ERROR:
                sm.transition(VoiceState.LISTENING, "start")
                sm.transition(VoiceState.ERROR, "boom")
            elif state is VoiceState.PROCESSING:
                sm.transition(VoiceState.LISTENING, "start")
                sm.transition(VoiceState.PROCESSING, "asr_final")
            elif state is VoiceState.SPEAKING:
                sm.transition(VoiceState.LISTENING, "start")
                sm.transition(VoiceState.PROCESSING, "asr_final")
                sm.transition(VoiceState.SPEAKING, "first_tts_audio")
            else:  # INTERRUPTING
                sm.transition(VoiceState.LISTENING, "start")
                sm.transition(VoiceState.PROCESSING, "asr_final")
                sm.transition(VoiceState.INTERRUPTING, "barge_in")
            record = sm.recover_to_listening("recover")
            assert sm.state == VoiceState.LISTENING, state
            assert record.to_state == VoiceState.LISTENING

    def test_recover_to_listening_is_noop_from_listening(self):
        sm = VoiceStateMachine()
        sm.transition(VoiceState.LISTENING, "start")
        sm.recover_to_listening("recover")
        assert sm.state == VoiceState.LISTENING

    def test_stopping_is_terminal(self):
        sm = VoiceStateMachine()
        sm.transition(VoiceState.LISTENING, "start")
        sm.transition(VoiceState.STOPPING, "stop")
        assert sm.state == VoiceState.STOPPING
        with pytest.raises(InvalidTransition):
            sm.transition(VoiceState.LISTENING)
        with pytest.raises(InvalidTransition):
            sm.recover_to_listening()

    def test_state_frame_shape(self):
        sm = VoiceStateMachine()
        record = sm.transition(VoiceState.LISTENING, "start", generation_id="abc123")
        frame = record.frame()
        assert frame == {
            "type": "state",
            "state": "LISTENING",
            "reason": "start",
            "generation_id": "abc123",
        }
        # generation_id omitted when not provided
        frame2 = sm.transition(VoiceState.PROCESSING, "asr_final").frame()
        assert "generation_id" not in frame2

    def test_can_transition_predicate(self):
        sm = VoiceStateMachine()
        assert sm.can_transition(VoiceState.LISTENING)
        assert not sm.can_transition(VoiceState.SPEAKING)
        assert sm.in_state(VoiceState.IDLE)
        assert not sm.in_state(VoiceState.LISTENING, VoiceState.SPEAKING)


# ══════════════════════════════════════════════════════════════════════
# RollingBuffer
# ══════════════════════════════════════════════════════════════════════


class TestRollingBuffer:
    def test_preroll_retained_and_ordered(self):
        buf = RollingBuffer(capacity_ms=300)
        chunks = [bytes([i]) * 1600 for i in range(1, 4)]  # 50 ms each
        for c in chunks:
            buf.push(c)
        snap = buf.snapshot()
        assert snap == b"".join(chunks)  # oldest → newest

    def test_capacity_enforced(self):
        buf = RollingBuffer(capacity_ms=100)  # 3200 bytes
        for i in range(10):
            buf.push(bytes([i]) * 1600)  # 50 ms each
        snap = buf.snapshot()
        assert len(snap) == buf.capacity_bytes
        # The NEWEST data is what survives (the tail).
        assert snap.endswith(bytes([9]) * 1600)

    def test_snapshot_is_a_copy(self):
        buf = RollingBuffer(capacity_ms=200)
        buf.push(b"abcd")
        snap = buf.snapshot()
        buf.push(b"efgh")
        assert snap == b"abcd"  # earlier snapshot unchanged

    def test_clear(self):
        buf = RollingBuffer(capacity_ms=200)
        buf.push(b"abcd")
        buf.clear()
        assert buf.snapshot() == b""

    def test_oversize_frame_keeps_tail(self):
        buf = RollingBuffer(capacity_ms=50)  # 1600 bytes
        big = bytes(range(256)) * 32  # 8192 bytes
        buf.push(big)
        snap = buf.snapshot()
        assert len(snap) == 1600
        assert snap == big[-1600:]

    def test_zero_capacity(self):
        buf = RollingBuffer(capacity_ms=0)
        buf.push(b"abcd")
        assert buf.snapshot() == b""


# ══════════════════════════════════════════════════════════════════════
# SentenceChunker
# ══════════════════════════════════════════════════════════════════════


class TestSentenceChunker:
    def make(self, **kw):
        defaults = dict(min_chars=10, max_chars=60, flush_ms=10_000)
        defaults.update(kw)
        return SentenceChunker(**defaults)

    def test_sentence_boundary_requires_min_chars(self):
        ch = self.make(min_chars=10)
        # "Hi. " is a boundary but only 3 chars → held back.
        assert ch.feed("Hi. ") == []
        # …now the accumulated sentence crosses min_chars at the boundary.
        out = ch.feed("This is long enough now. ")
        assert out == ["Hi. This is long enough now."]

    def test_boundary_before_min_chars_can_qualify_later(self):
        ch = self.make(min_chars=10)
        assert ch.feed("No. ") == []
        # The NEXT boundary's prefix ("No. " + tail) reaches min_chars.
        out = ch.feed("Sure, that works fine. ")
        assert out == ["No. Sure, that works fine."]

    def test_double_newline_is_a_boundary(self):
        ch = self.make(min_chars=1)
        out = ch.feed("Paragraph one.\n\nParagraph two.")
        assert out[0] == "Paragraph one."

    def test_decimals_do_not_split(self):
        ch = self.make(min_chars=1, max_chars=500)
        out = ch.feed("The value is 3.14159 and pi. ")
        assert out == ["The value is 3.14159 and pi."]

    def test_max_chars_breaks_at_last_space(self):
        ch = self.make(min_chars=1, max_chars=20)
        out = ch.feed("aaaaaaaaaa bbbbbbbbbbb cccccccccc")
        # 33 chars ≥ 20 → first chunk breaks at the last space ≤ 20; the
        # remainder stays buffered until flush (normal streaming behavior).
        assert out == ["aaaaaaaaaa", "bbbbbbbbbbb"]
        assert ch.flush() == ["cccccccccc"]

    def test_flush_emits_remainder(self):
        ch = self.make(min_chars=1000, max_chars=5000)  # nothing emits alone
        ch.feed("short tail text")
        assert ch.flush() == ["short tail text"]
        assert ch.flush() == []

    def test_reset_clears_state(self):
        ch = self.make(min_chars=1000, max_chars=5000)
        ch.feed("buffered text that will never emit ")
        ch.reset()
        assert ch.flush() == []
        assert ch.buffered == ""

    def test_flush_timeout_emits_after_delay(self):
        ch = self.make(min_chars=5, max_chars=500, flush_ms=30)
        assert ch.feed("hello world without punctuation") == []
        time.sleep(0.05)
        # A feed("") only re-checks the timeout (the TTS worker polls it).
        out = ch.feed("")
        assert out == ["hello world without punctuation"]

    def test_flush_timeout_needs_min_chars(self):
        ch = self.make(min_chars=500, max_chars=5000, flush_ms=20)
        ch.feed("tiny")
        time.sleep(0.05)
        assert ch.feed("") == []  # below min_chars → still held
        assert ch.flush() == ["tiny"]  # explicit flush ignores min_chars

    def test_token_streaming_accumulates(self):
        ch = self.make(min_chars=10, max_chars=200, flush_ms=10_000)
        out = []
        for token in "This sentence is long enough to emit. ".split(" "):
            out.extend(ch.feed(token + " "))
        assert out == ["This sentence is long enough to emit."]


class TestCleanForTts:
    def test_fenced_code_removed(self):
        text = "Here is the plan.\n```python\nprint('hi')\n```\nDone."
        assert "print" not in clean_for_tts(text)
        assert "Here is the plan." in clean_for_tts(text)
        assert "Done." in clean_for_tts(text)

    def test_inline_code_kept_as_words(self):
        out = clean_for_tts("Run `pip install` now.")
        assert out == "Run pip install now."

    def test_headers_and_bullets_stripped(self):
        out = clean_for_tts("## Section Title\n- first item\n* second item")
        assert "##" not in out
        assert "-" not in out.split("first")[0][-2:]
        assert "Section Title" in out
        assert "first item" in out
        assert "second item" in out

    def test_links_and_images(self):
        out = clean_for_tts("See [the docs](https://x.y) and ![logo](a.png).")
        assert "the docs" in out
        assert "https" not in out
        assert "logo" not in out

    def test_emphasis_markers_removed(self):
        out = clean_for_tts("This is **bold**, *italic*, and _underlined_.")
        assert out == "This is bold, italic, and underlined."

    def test_ordered_list_markers_stripped(self):
        out = clean_for_tts("1. first step\n2. second step")
        assert "1." not in out
        assert "first step" in out

    def test_plain_prose_untouched(self):
        text = "The quick brown fox jumps over the lazy dog."
        assert clean_for_tts(text) == text


# ══════════════════════════════════════════════════════════════════════
# AEC
# ══════════════════════════════════════════════════════════════════════


def _db(x: float) -> float:
    return 20.0 * math.log10(max(x, 1e-12))


class TestAec:
    def test_passthrough_identity(self):
        aec = PassThroughAec()
        aec.push_reference(b"\x01\x00" * 1600)
        mic = bytes(range(256)) * 8
        assert aec.process(mic) == mic

    def test_nlms_suppresses_synthetic_echo(self):
        """mic = far-end delayed by 2000 samples → after adaptation the
        residual energy must drop ≥6 dB vs the input (ERLE ≥ 4x)."""
        rng = np.random.default_rng(42)
        sr = 16000
        n_seconds = 3
        n = sr * n_seconds
        # Colored noise (lowpassed) — more echo-like than white noise.
        far = np.convolve(
            rng.standard_normal(n + 64), np.ones(16) / 16.0, mode="valid"
        ).astype(np.float32)
        far /= max(1e-9, float(np.max(np.abs(far)))) / 0.5  # ~-6 dBFS

        delay = 2000  # 125 ms — inside the ±400 ms search window
        mic = np.zeros(n, dtype=np.float32)
        mic[delay:] = far[: n - delay]

        def to_pcm(x: np.ndarray) -> bytes:
            return (np.clip(x, -1, 1) * 32767.0).astype("<i2").tobytes()

        aec = NlmsAec(taps=2048, mu=0.35)
        frame = 320  # 10 ms
        in_energy = 0.0
        out_energy = 0.0
        frames = n // frame
        warmup = frames // 2  # measure the second half only
        for i in range(frames):
            s = i * frame
            far_pcm = to_pcm(far[s : s + frame])
            mic_pcm = to_pcm(mic[s : s + frame])
            aec.push_reference(far_pcm)
            out = aec.process(mic_pcm)
            if i >= warmup:
                in_energy += float(
                    np.sum(
                        np.square(
                            np.frombuffer(mic_pcm, dtype="<i2").astype(np.float64)
                        )
                    )
                )
                out_energy += float(
                    np.sum(
                        np.square(np.frombuffer(out, dtype="<i2").astype(np.float64))
                    )
                )
        reduction_db = _db(in_energy) - _db(out_energy)
        assert reduction_db >= 6.0, f"only {reduction_db:.1f} dB echo reduction"
        status = aec.status()
        assert status["provider"] == "nlms"
        # The status ERLE is cumulative (includes the adapting warm-up) —
        # it must be positive but is allowed below the steady-state figure.
        assert status["erle"] is not None and status["erle"] > 2.0
        assert status["lag_samples"] == delay  # alignment found the true lag
        assert status["aligned"] is True

    def test_nlms_passes_through_without_reference(self):
        aec = NlmsAec()
        mic = (np.sin(np.arange(1600) * 0.05) * 12000).astype("<i2").tobytes()
        out = aec.process(mic)
        # No far-end fed → identity (up to the float32 round-trip LSB).
        got = np.frombuffer(out, dtype="<i2")
        want = np.frombuffer(mic, dtype="<i2")
        assert len(got) == len(want)
        assert np.allclose(got.astype(float), want.astype(float), atol=2.0)

    def test_reset_clears_state(self):
        aec = NlmsAec()
        aec.push_reference(b"\x01\x02" * 320)
        aec.reset()
        assert aec.status()["far_samples"] == 0


# ══════════════════════════════════════════════════════════════════════
# models_store (manifest validation with tmp_path)
# ══════════════════════════════════════════════════════════════════════


ASR_SPEC = SimpleNamespace(
    provider="qwen3-asr",
    model="Qwen/Qwen3-ASR-0.6B",
    revision="main",
    language=None,
    voice=None,
)
TTS_SPEC = SimpleNamespace(
    provider="pocket-tts",
    model="pocket-tts",
    revision=None,
    language="english_2026-04",
    voice="mary",
)
FAKE_CONFIG = SimpleNamespace(asr=ASR_SPEC, tts=TTS_SPEC)


def asr_entry(**overrides) -> dict:
    entry = {
        "provider": "qwen3-asr",
        "model": "Qwen/Qwen3-ASR-0.6B",
        "revision": "main",
        "type": "asr",
        "role": "default_asr",
        "path": "asr/Qwen3-ASR-0.6B",
        "files": {"config.json": 731, "model.safetensors": 1024},
        "total_bytes": 1755,
        "complete": True,
        "installed_at": "2026-06-26T00:00:00",
        "source": "https://huggingface.co/Qwen/Qwen3-ASR-0.6B@main",
    }
    entry.update(overrides)
    return entry


def tts_entry(**overrides) -> dict:
    entry = {
        "provider": "pocket-tts",
        "model": "pocket-tts",
        "language": "english_2026-04",
        "voice": "mary",
        "type": "tts",
        "role": "default_tts",
        "path": "tts/pocket-tts",
        "files": {},
        "total_bytes": 0,
        "complete": True,
        "preloaded": False,
        "package_installed": True,
        "installed_at": "2026-06-26T00:00:00",
    }
    entry.update(overrides)
    return entry


@pytest.fixture()
def pinned_store(tmp_path, monkeypatch):
    """models_store pinned to a tmp dir + the fake profiles.yml selection."""
    monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(models_store, "_resolved_voice_config", lambda: FAKE_CONFIG)
    return models_store


def write_asr_files(root: Path, sizes=None) -> None:
    d = root / "asr" / "Qwen3-ASR-0.6B"
    d.mkdir(parents=True, exist_ok=True)
    for name, size in (
        sizes or {"config.json": 731, "model.safetensors": 1024}
    ).items():
        (d / name).write_bytes(b"x" * size)


class TestModelsStore:
    def test_not_installed_when_manifest_missing(self, pinned_store):
        assert pinned_store.asr_ready() is False
        status = pinned_store.voice_dependency_status()
        assert status["ready"] is False
        assert status["asr"]["installed"] is False
        assert status["asr"]["reason"] == "not_installed"

    def test_installed_and_valid(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        pinned_store.save_manifest({"asr": asr_entry(), "tts": tts_entry()})
        assert pinned_store.asr_ready() is True
        assert pinned_store.tts_ready() is True
        status = pinned_store.voice_dependency_status()
        assert status["ready"] is True
        assert status["enabled"] is True
        assert status["configured"] == {"asr": True, "tts": True}
        assert status["asr"]["provider"] == "qwen3-asr"
        assert status["asr"]["model"] == "Qwen/Qwen3-ASR-0.6B"
        assert status["asr"]["valid"] is True
        assert status["tts"]["valid"] is True

    def test_changed_model_is_not_valid(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        pinned_store.save_manifest({"asr": asr_entry(model="Qwen/Qwen3-ASR-1.7B")})
        assert pinned_store.asr_ready() is False
        assert pinned_store.voice_dependency_status()["asr"]["reason"] == "changed"

    def test_changed_revision_is_not_valid(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        pinned_store.save_manifest({"asr": asr_entry(revision="v2")})
        assert pinned_store.asr_ready() is False

    def test_missing_file_is_not_installed(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        (tmp_path / "asr" / "Qwen3-ASR-0.6B" / "model.safetensors").unlink()
        pinned_store.save_manifest({"asr": asr_entry()})
        status = pinned_store.voice_dependency_status()["asr"]
        assert status["installed"] is False
        assert "missing file" in status["reason"]

    def test_size_mismatch_is_not_valid(self, pinned_store, tmp_path):
        write_asr_files(tmp_path, sizes={"config.json": 731, "model.safetensors": 512})
        pinned_store.save_manifest({"asr": asr_entry()})
        status = pinned_store.voice_dependency_status()["asr"]
        assert status["installed"] is False
        assert "size mismatch" in status["reason"]

    def test_incomplete_entry_is_not_ready(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        pinned_store.save_manifest({"asr": asr_entry(complete=False)})
        assert pinned_store.asr_ready() is False
        assert pinned_store.voice_dependency_status()["asr"]["reason"] == "incomplete"

    def test_tts_language_change_requires_reinstall(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        pinned_store.save_manifest(
            {"asr": asr_entry(), "tts": tts_entry(language="french_2026-04")}
        )
        assert pinned_store.tts_ready() is False
        assert pinned_store.asr_ready() is True

    def test_tts_voice_change_stays_valid(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        # Only the speaker changed — no re-download needed (installer rule).
        pinned_store.save_manifest({"asr": asr_entry(), "tts": tts_entry(voice="bob")})
        assert pinned_store.tts_ready() is True

    def test_unconfigured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path)
        monkeypatch.setattr(models_store, "_resolved_voice_config", lambda: None)
        status = models_store.voice_dependency_status()
        assert status["ready"] is False
        assert status["configured"] == {"asr": False, "tts": False}

    def test_load_manifest_roundtrip(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        data = {"asr": asr_entry(), "tts": tts_entry()}
        pinned_store.save_manifest(data)
        loaded = pinned_store.load_manifest()
        assert loaded == data
        # corrupt file → {} (never raises)
        (tmp_path / ".manifest.json").write_text("{not json")
        assert pinned_store.load_manifest() == {}

    def test_asr_dir_helper(self, pinned_store, tmp_path):
        d = pinned_store.asr_dir(ASR_SPEC)
        assert d == tmp_path / "asr" / "Qwen3-ASR-0.6B"

    def test_validator_hooks_accept_config_or_spec(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        pinned_store.save_manifest({"asr": asr_entry(), "tts": tts_entry()})
        assert pinned_store.validate_asr_install(FAKE_CONFIG) is True
        assert pinned_store.validate_asr_install(ASR_SPEC) is True
        assert pinned_store.validate_tts_install(TTS_SPEC) is True
        assert pinned_store.validate_asr_install(None) is True  # current spec


# ══════════════════════════════════════════════════════════════════════
# GET /api/voice/status — the frontend mic button's readiness gate
# (frontend/src/voice/voiceStore.ts: {ready, enabled, asr, tts, missing})
# ══════════════════════════════════════════════════════════════════════


class TestVoiceStatusEndpoint:
    @pytest.mark.asyncio
    async def test_not_ready_when_models_missing(self, pinned_store, monkeypatch):
        monkeypatch.setattr(voice_api, "_runtime_missing", lambda: [])
        res = await voice_api.get_voice_status()
        assert res["ready"] is False
        assert res["enabled"] is True
        assert res["asr"]["installed"] is False
        assert res["tts"]["installed"] is False
        assert any(m.startswith("ASR: ") for m in res["missing"])
        assert any(m.startswith("TTS: ") for m in res["missing"])

    @pytest.mark.asyncio
    async def test_ready_when_installed_and_runtime_ok(
        self, pinned_store, tmp_path, monkeypatch
    ):
        write_asr_files(tmp_path)
        pinned_store.save_manifest({"asr": asr_entry(), "tts": tts_entry()})
        monkeypatch.setattr(voice_api, "_runtime_missing", lambda: [])
        res = await voice_api.get_voice_status()
        assert res["ready"] is True
        assert res["missing"] == []
        assert res["asr"]["valid"] is True
        assert res["tts"]["valid"] is True

    @pytest.mark.asyncio
    async def test_runtime_missing_blocks_ready(
        self, pinned_store, tmp_path, monkeypatch
    ):
        write_asr_files(tmp_path)
        pinned_store.save_manifest({"asr": asr_entry(), "tts": tts_entry()})
        monkeypatch.setattr(
            voice_api, "_runtime_missing", lambda: ["Runtime: PyTorch (CPU)"]
        )
        res = await voice_api.get_voice_status()
        # Models valid, but the session would fail at load → not ready.
        assert res["asr"]["valid"] is True
        assert res["ready"] is False
        assert "Runtime: PyTorch (CPU)" in res["missing"]

    @pytest.mark.asyncio
    async def test_disabled_blocks_ready(self, pinned_store, tmp_path, monkeypatch):
        write_asr_files(tmp_path)
        pinned_store.save_manifest({"asr": asr_entry(), "tts": tts_entry()})
        monkeypatch.setattr(voice_api, "_runtime_missing", lambda: [])
        monkeypatch.setattr(settings, "VOICE_ENABLED", False)
        res = await voice_api.get_voice_status()
        assert res["enabled"] is False
        assert res["ready"] is False
