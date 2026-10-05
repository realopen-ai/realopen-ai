"""Unit tests for the voice pipeline building blocks.

Covers the deterministic, no-IO pieces (per the task spec):
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

import asyncio
import json
import math
import os
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
from app.api import setup as setup_api  # noqa: E402
from app.api import voice as voice_api  # noqa: E402
from app.voice import models_store  # noqa: E402
from app.voice import hf_cache  # noqa: E402
from app.voice.aec import NlmsAec, PassThroughAec  # noqa: E402
from app.voice.audio import RollingBuffer  # noqa: E402
from app.voice.state import (  # noqa: E402
    InvalidTransition,
    VoiceState,
    VoiceStateMachine,
)
from app.voice.tts.chunker import SentenceChunker, clean_for_tts  # noqa: E402


class TestVoiceNoiseGate:
    def test_keyboard_impulses_do_not_open_an_utterance(self):
        from app.voice.vad import EnergyVad, UtteranceTracker, VAD_FRAME_SAMPLES

        vad = EnergyVad(hangover_frames=2)
        tracker = UtteranceTracker(vad, start_frames=8, silence_ms=300)
        click = np.full(VAD_FRAME_SAMPLES, 12000, dtype="<i2").tobytes()
        quiet = np.zeros(VAD_FRAME_SAMPLES, dtype="<i2").tobytes()
        events = []
        for _ in range(6):
            events.extend(tracker.feed(click))
            events.extend(tracker.feed(quiet))
            events.extend(tracker.feed(quiet))
        assert "start" not in events

    def test_sustained_voice_like_audio_opens_an_utterance(self):
        from app.voice.vad import EnergyVad, UtteranceTracker, VAD_FRAME_SAMPLES

        vad = EnergyVad(hangover_frames=2)
        tracker = UtteranceTracker(vad, start_frames=8, silence_ms=300)
        voiced = np.full(VAD_FRAME_SAMPLES, 5000, dtype="<i2").tobytes()
        events = []
        for _ in range(8):
            events.extend(tracker.feed(voiced))
        assert events == ["start"]

    def test_voice_prompt_is_spoken_and_adaptive(self):
        from app.prompts import format_prompt

        prompt = format_prompt("voice_mode")
        assert "Write for the ear" in prompt
        assert "Adapt depth" in prompt
        assert "Never claim you cannot create" in prompt

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
        "files": {
            "model.safetensors": 1024,
            "tokenizer.model": 512,
            "voice-mary.safetensors": 2048,
            "local-config.yaml": 300,
        },
        "total_bytes": 3884,
        "complete": True,
        "assets_schema": 2,
        "preloaded": True,
        "package_installed": True,
        "installed_at": "2026-06-26T00:00:00",
    }
    entry.update(overrides)
    return entry


@pytest.fixture()
def pinned_store(tmp_path, monkeypatch):
    """models_store pinned to a tmp dir + the fake profiles.yml selection.

    The HF cache home is pinned too (hermetic HF-store validation).
    """
    monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path)
    monkeypatch.setattr(models_store, "_resolved_voice_config", lambda: FAKE_CONFIG)
    monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
    monkeypatch.setattr(models_store, "_missing_runtime_entries", lambda: [])
    return models_store


def write_asr_files(root: Path, sizes=None) -> None:
    d = root / "asr" / "Qwen3-ASR-0.6B"
    d.mkdir(parents=True, exist_ok=True)
    for name, size in (
        sizes or {"config.json": 731, "model.safetensors": 1024}
    ).items():
        (d / name).write_bytes(b"x" * size)


def write_tts_files(root: Path, sizes=None) -> None:
    d = root / "tts" / "pocket-tts"
    d.mkdir(parents=True, exist_ok=True)
    for name, size in (
        sizes
        or {
            "model.safetensors": 1024,
            "tokenizer.model": 512,
            "voice-mary.safetensors": 2048,
            "local-config.yaml": 300,
        }
    ).items():
        (d / name).write_bytes(b"x" * size)


def write_ready_hf_manifest(store, monkeypatch) -> None:
    """Materialize the two minimal HF snapshots and their v3 manifest."""
    asr_repo = "Qwen/Qwen3-ASR-0.6B"
    tts_repo = "kyutai/pocket-tts-without-voice-cloning"
    for repo_id, files in (
        (asr_repo, {"config.json": b"c" * 731, "model.safetensors": b"w" * 1024}),
        (tts_repo, {"model.safetensors": b"t" * 2048}),
    ):
        root = hf_cache.repo_cache_dir(repo_id)
        snap = root / "snapshots" / "abc123"
        snap.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            (snap / name).write_bytes(content)
        (root / "refs").mkdir(parents=True, exist_ok=True)
        (root / "refs" / "main").write_text("abc123")
    asr = asr_entry(store="hf")
    asr.pop("path")
    asr["hf_repos"] = {asr_repo: "snapshots/abc123"}
    asr["files"] = {
        f"{asr_repo}::config.json": 731,
        f"{asr_repo}::model.safetensors": 1024,
    }
    tts = tts_entry(store="hf")
    tts.pop("path")
    tts["hf_repos"] = {tts_repo: "snapshots/abc123"}
    tts["files"] = {f"{tts_repo}::model.safetensors": 2048}
    monkeypatch.setattr(models_store, "_pocket_tts_importable", lambda: True)
    store.save_manifest({"asr": asr, "tts": tts})


class TestModelsStore:
    def test_not_installed_when_manifest_missing(self, pinned_store):
        assert pinned_store.asr_ready() is False
        status = pinned_store.voice_dependency_status()
        assert status["ready"] is False
        assert status["asr"]["installed"] is False
        assert status["asr"]["reason"] == "not_installed"

    def test_legacy_install_requires_migration(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        write_tts_files(tmp_path)
        pinned_store.save_manifest({"asr": asr_entry(), "tts": tts_entry()})
        assert pinned_store.asr_ready() is False
        assert pinned_store.tts_ready() is False
        status = pinned_store.voice_dependency_status()
        assert status["ready"] is False
        assert status["enabled"] is True
        assert status["configured"] == {"asr": True, "tts": True}
        assert status["asr"]["provider"] == "qwen3-asr"
        assert status["asr"]["model"] == "Qwen/Qwen3-ASR-0.6B"
        assert status["asr"]["legacy"] is True
        assert "migrate" in status["tts"]["reason"]

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
        assert "legacy voice install" in status["reason"]

    def test_size_mismatch_is_not_valid(self, pinned_store, tmp_path):
        write_asr_files(tmp_path, sizes={"config.json": 731, "model.safetensors": 512})
        pinned_store.save_manifest({"asr": asr_entry()})
        status = pinned_store.voice_dependency_status()["asr"]
        assert status["installed"] is False
        assert "legacy voice install" in status["reason"]

    def test_incomplete_entry_is_not_ready(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        pinned_store.save_manifest({"asr": asr_entry(complete=False)})
        assert pinned_store.asr_ready() is False
        assert pinned_store.voice_dependency_status()["asr"]["reason"] == "incomplete"

    def test_tts_language_change_requires_reinstall(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        write_tts_files(tmp_path)
        pinned_store.save_manifest(
            {"asr": asr_entry(), "tts": tts_entry(language="french_2026-04")}
        )
        assert pinned_store.tts_ready() is False
        assert pinned_store.asr_ready() is False

    def test_tts_empty_files_is_not_valid(self, pinned_store, tmp_path):
        """TTS entries with NO recorded files are INVALID: the runtime
        never downloads at first use, so empty assets = not installed
        (the old bug let voice sessions start and hang on the first turn)."""
        write_asr_files(tmp_path)
        write_tts_files(tmp_path)
        pinned_store.save_manifest(
            {
                "asr": asr_entry(),
                "tts": tts_entry(files={}, total_bytes=0, preloaded=False),
            }
        )
        assert pinned_store.tts_ready() is False
        status = pinned_store.voice_dependency_status()
        assert status["ready"] is False
        assert "legacy voice install" in status["tts"]["reason"]

    def test_tts_assets_dir_helper(self, pinned_store, tmp_path):
        write_tts_files(tmp_path)
        pinned_store.save_manifest({"tts": tts_entry()})
        assert pinned_store.tts_assets_dir() == tmp_path / "tts" / "pocket-tts"
        # Empty-file entry → None (not valid)
        pinned_store.save_manifest({"tts": tts_entry(files={})})
        assert pinned_store.tts_assets_dir() is None

    def test_tts_voice_change_stays_valid(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        write_tts_files(tmp_path)
        # Only the speaker changed — no re-download needed (installer rule).
        pinned_store.save_manifest({"asr": asr_entry(), "tts": tts_entry(voice="bob")})
        assert pinned_store.tts_ready() is False

    def test_unconfigured(self, tmp_path, monkeypatch):
        monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path)
        monkeypatch.setattr(models_store, "_resolved_voice_config", lambda: None)
        status = models_store.voice_dependency_status()
        assert status["ready"] is False
        assert status["configured"] == {"asr": False, "tts": False}

    def test_load_manifest_roundtrip(self, pinned_store, tmp_path):
        write_asr_files(tmp_path)
        write_tts_files(tmp_path)
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
        write_tts_files(tmp_path)
        pinned_store.save_manifest({"asr": asr_entry(), "tts": tts_entry()})
        assert pinned_store.validate_asr_install(FAKE_CONFIG) is False
        assert pinned_store.validate_asr_install(ASR_SPEC) is False
        assert pinned_store.validate_tts_install(TTS_SPEC) is False
        assert pinned_store.validate_asr_install(None) is False

    # ── HF-store (schema v3) validation ──────────────────────────────

    def test_hf_store_models_and_runtime_are_ready(self, pinned_store, monkeypatch):
        write_ready_hf_manifest(pinned_store, monkeypatch)
        status = pinned_store.voice_dependency_status()
        assert status["ready"] is True
        assert status["runtime"] == []

    def _write_hf_repo(self, tmp_path, repo_id, files, sha="abc123", ref="main"):
        """Materialize a hub-cache layout: refs/main + snapshots/<sha>/files."""
        root = hf_cache.repo_cache_dir(repo_id)
        snap = root / "snapshots" / sha
        for rel, content in files.items():
            target = snap / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        ref_dir = root / "refs"
        ref_dir.mkdir(parents=True, exist_ok=True)
        (ref_dir / ref).write_text(sha)
        return root

    def test_hf_store_asr_valid(self, pinned_store, tmp_path):
        self._write_hf_repo(
            tmp_path,
            "Qwen/Qwen3-ASR-0.6B",
            {"config.json": b"x" * 731, "model.safetensors": b"w" * 1024},
        )
        entry = asr_entry(
            store="hf",
            path=None,
            files={},
        )
        entry.pop("path")
        entry["hf_repos"] = {"Qwen/Qwen3-ASR-0.6B": "snapshots/abc123"}
        entry["files"] = {
            "Qwen/Qwen3-ASR-0.6B::config.json": 731,
            "Qwen/Qwen3-ASR-0.6B::model.safetensors": 1024,
        }
        pinned_store.save_manifest({"asr": entry})
        assert pinned_store.asr_ready() is True
        status = pinned_store.voice_dependency_status()
        assert status["asr"]["valid"] is True
        assert status["asr"]["store"] == "hf"
        # The engine's snapshot resolution points at the cached files.
        snap = pinned_store.asr_snapshot_dir()
        assert snap is not None and (snap / "model.safetensors").is_file()

    def test_hf_store_asr_missing_cache_file(self, pinned_store, tmp_path):
        entry = asr_entry()
        entry.pop("path")
        entry["store"] = "hf"
        entry["hf_repos"] = {"Qwen/Qwen3-ASR-0.6B": "snapshots/abc123"}
        entry["files"] = {
            "Qwen/Qwen3-ASR-0.6B::model.safetensors": 1024,
        }
        pinned_store.save_manifest({"asr": entry})  # no cache on disk
        status = pinned_store.voice_dependency_status()["asr"]
        assert status["installed"] is False
        assert "HF cache snapshot missing" in status["reason"]

    def test_hf_store_asr_size_mismatch(self, pinned_store, tmp_path):
        self._write_hf_repo(
            tmp_path,
            "Qwen/Qwen3-ASR-0.6B",
            {"model.safetensors": b"w" * 512},
        )
        entry = asr_entry()
        entry.pop("path")
        entry["store"] = "hf"
        entry["hf_repos"] = {"Qwen/Qwen3-ASR-0.6B": "snapshots/abc123"}
        entry["files"] = {"Qwen/Qwen3-ASR-0.6B::model.safetensors": 1024}
        pinned_store.save_manifest({"asr": entry})
        status = pinned_store.voice_dependency_status()["asr"]
        assert status["installed"] is False
        assert "size mismatch" in status["reason"]

    def test_hf_store_tts_valid(self, pinned_store, tmp_path, monkeypatch):
        repo = "kyutai/pocket-tts-without-voice-cloning"
        self._write_hf_repo(tmp_path, repo, {"model.safetensors": b"w" * 2048})
        entry = tts_entry()
        entry.pop("path")
        entry["store"] = "hf"
        entry["hf_repos"] = {repo: "snapshots/abc123"}
        entry["files"] = {f"{repo}::model.safetensors": 2048}
        pinned_store.save_manifest({"tts": entry})
        # pocket_tts must be importable for hf-store TTS readiness
        # (find_spec cannot see a sys.modules fake — patch the probe).
        monkeypatch.setattr(models_store, "_pocket_tts_importable", lambda: True)
        assert pinned_store.tts_ready() is True
        # HF-store installs never expose a legacy assets dir.
        assert pinned_store.tts_assets_dir() is None

    def test_hf_store_tts_requires_package(self, pinned_store, tmp_path, monkeypatch):
        repo = "kyutai/pocket-tts-without-voice-cloning"
        self._write_hf_repo(tmp_path, repo, {"model.safetensors": b"w" * 2048})
        entry = tts_entry()
        entry.pop("path")
        entry["store"] = "hf"
        entry["hf_repos"] = {repo: "snapshots/abc123"}
        entry["files"] = {f"{repo}::model.safetensors": 2048}
        pinned_store.save_manifest({"tts": entry})
        # Simulate a fresh container: pocket_tts not installed yet.
        monkeypatch.setattr(models_store, "_pocket_tts_importable", lambda: False)
        status = pinned_store.voice_dependency_status()["tts"]
        assert status["valid"] is False
        assert "pocket-tts" in status["reason"]

    def test_runtime_manifest_entry_helper(self, pinned_store, tmp_path):
        assert pinned_store.runtime_manifest_entry() is None
        pinned_store.save_manifest(
            {
                "runtime": {
                    "packages": [{"id": "pocket-tts", "pip_name": "pocket-tts"}],
                    "installed_at": "2026-09-19T00:00:00",
                }
            }
        )
        entry = pinned_store.runtime_manifest_entry()
        assert entry is not None
        assert entry["packages"][0]["pip_name"] == "pocket-tts"


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
    async def test_ollama_failure_does_not_abort_voice_setup(self, monkeypatch):
        monkeypatch.setattr(
            setup_api,
            "_get_models_to_pull",
            lambda profile, modules: [
                {
                    "id": "chat",
                    "module": "assistant",
                    "provider": "ollama",
                    "kind": "ollama",
                },
                {
                    "id": "pocket-tts",
                    "module": "voice",
                    "provider": "pip",
                    "kind": "voice_runtime",
                },
            ],
        )

        class UnreachableOllama:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def get(self, url):
                raise RuntimeError("offline")

        async def fake_voice_stream(*args, **kwargs):
            yield {
                "event": "pull_done",
                "model": "pocket-tts",
                "module": "voice",
                "provider": "pip",
                "kind": "voice_runtime",
            }

        monkeypatch.setattr(setup_api.httpx, "AsyncClient", UnreachableOllama)
        monkeypatch.setattr(
            setup_api.voice_model_installer,
            "stream_install_voice_dependencies",
            fake_voice_stream,
        )

        response = await setup_api.pull_setup_models(
            setup_api.InstallSetupModelsRequest(
                profile="apple_medium", enabled_modules=["assistant"]
            )
        )
        chunks = [chunk async for chunk in response.body_iterator]
        body = b"".join(
            chunk if isinstance(chunk, bytes) else chunk.encode() for chunk in chunks
        ).decode()
        events = [
            json.loads(line.removeprefix("data: "))
            for line in body.splitlines()
            if line.startswith("data: ")
        ]

        assert not any(event["event"] == "setup_error" for event in events)
        assert any(
            event["event"] == "pull_error" and event["kind"] == "ollama"
            for event in events
        )
        assert any(
            event["event"] == "pull_done" and event["model"] == "pocket-tts"
            for event in events
        )

    def test_linux_runtime_cannot_resolve_cuda_torch_transitively(self, monkeypatch):
        from app.services import voice_model_installer as vmi

        monkeypatch.setattr(vmi, "_on_apple_host", lambda: False)
        torch_pkg = vmi.RUNTIME_PACKAGES["torch-cpu"]
        accelerate_pkg = vmi.RUNTIME_PACKAGES["accelerate"]
        assert torch_pkg.pip_name.endswith("+cpu")
        assert any(
            "download.pytorch.org/whl/cpu" in arg for arg in torch_pkg.pip_extra_args
        )
        assert accelerate_pkg.pip_name == "accelerate==1.12.0"
        assert "--no-deps" in accelerate_pkg.pip_extra_args
        assert vmi.RUNTIME_PACKAGES["nagisa"].pip_name == "nagisa==0.2.11"
        assert vmi.RUNTIME_PACKAGES["soynlp"].pip_name == "soynlp==0.0.493"
        assert "psutil" in vmi._asr_runtime_packages()

    @pytest.mark.asyncio
    async def test_ready_when_installed_and_runtime_ok(
        self, pinned_store, tmp_path, monkeypatch
    ):
        write_ready_hf_manifest(pinned_store, monkeypatch)
        res = await voice_api.get_voice_status()
        assert res["ready"] is True
        assert res["missing"] == []
        assert res["asr"]["valid"] is True
        assert res["tts"]["valid"] is True

    @pytest.mark.asyncio
    async def test_runtime_missing_blocks_ready(
        self, pinned_store, tmp_path, monkeypatch
    ):
        write_ready_hf_manifest(pinned_store, monkeypatch)
        monkeypatch.setattr(
            models_store,
            "_missing_runtime_entries",
            lambda: [{"id": "torch-cpu", "description": "PyTorch (CPU)"}],
        )
        res = await voice_api.get_voice_status()
        # Models valid, but the session would fail at load → not ready.
        assert res["asr"]["valid"] is True
        assert res["ready"] is False
        assert "Runtime: PyTorch (CPU)" in res["missing"]

    @pytest.mark.asyncio
    async def test_disabled_blocks_ready(self, pinned_store, tmp_path, monkeypatch):
        write_ready_hf_manifest(pinned_store, monkeypatch)
        monkeypatch.setattr(settings, "VOICE_ENABLED", False)
        res = await voice_api.get_voice_status()
        assert res["enabled"] is False
        assert res["ready"] is False


# ══════════════════════════════════════════════════════════════════════
# Setup-side NATURAL TTS warm-up + install race fixes (user directive):
#   - pocket_tts downloads its own weights naturally during setup
#     (load_model + get_state_for_audio_prompt) into the persisted HF
#     cache; the manifest records the resulting repos/snapshots
#   - concurrent pull-models requests must not race on the cache
#   - the runtime engine loads naturally from the (offline-guarded) cache
# ══════════════════════════════════════════════════════════════════════

TTS_HF_REPO = "kyutai/pocket-tts-without-voice-cloning"


def _fake_pocket_tts_module(hub_root: Path, downloads: dict, fail: bool = False):
    """A fake pocket_tts package whose load_model "downloads" into the HF
    hub-cache layout exactly like the real package does (repo dir + refs +
    snapshot files). Carries a bundled language config referencing the repo
    (mirrors the real package's config/english_2026-04.yaml)."""
    import types

    pkg_dir = hub_root.parent / "site-packages" / "pocket_tts"
    cfg_dir = pkg_dir / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "english_2026-04.yaml").write_text(
        "weights_path: hf://kyutai/pocket-tts-without-voice-cloning/model.safetensors\n"
    )

    class TTSModel:
        sample_rate = 24000

        def __init__(self):
            if fail:
                raise RuntimeError("connection refused (simulated cold net)")

        @staticmethod
        def load_model(language=None, config=None):
            if fail:
                raise RuntimeError("connection refused (simulated cold net)")
            snap_root = hub_root / "models--kyutai--pocket-tts-without-voice-cloning"
            sha = "dead00"
            for rel, payload in downloads.items():
                target = snap_root / "snapshots" / sha / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            refs = snap_root / "refs"
            refs.mkdir(parents=True, exist_ok=True)
            (refs / "main").write_text(sha)
            return TTSModel()

        def get_state_for_audio_prompt(self, voice):
            # The pretrained embedding for the voice, cached in the same repo.
            snap = (
                hub_root
                / "models--kyutai--pocket-tts-without-voice-cloning"
                / "snapshots"
                / "dead00"
            )
            emb = (
                snap
                / "languages"
                / "english_2026-04"
                / "embeddings"
                / f"{voice}.safetensors"
            )
            emb.parent.mkdir(parents=True, exist_ok=True)
            if not emb.exists():
                emb.write_bytes(b"e" * 2048)
            return SimpleNamespace(name=voice)

    mod = types.ModuleType("pocket_tts")
    mod.__file__ = str(pkg_dir / "__init__.py")
    mod.TTSModel = TTSModel
    return mod


class TestTtsNaturalWarmup:
    """_install_tts warms the cache NATURALLY (package-driven download) and
    records the resulting hub repos/snapshots in the manifest."""

    @pytest.fixture(autouse=True)
    def vmi(self, tmp_path, monkeypatch):
        from app.services import voice_model_installer as vmi_module

        models_root = tmp_path / "models"
        monkeypatch.setattr(vmi_module, "_MODELS_DIR_OVERRIDE", models_root)
        monkeypatch.setattr(models_store, "MODELS_DIR", models_root)
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
        monkeypatch.setattr(models_store, "_resolved_voice_config", lambda: FAKE_CONFIG)
        # pocket_tts "installed" for the install flow (find_spec cannot see
        # sys.modules fakes — the probe is patched instead).
        monkeypatch.setattr(
            vmi_module, "_module_importable", lambda m: m == "pocket_tts"
        )
        monkeypatch.setattr(vmi_module, "_runtime_package_satisfied", lambda p: True)
        monkeypatch.setattr(models_store, "_pocket_tts_importable", lambda: True)

        async def fake_warmup(script, watch_dirs, timeout_s):
            # Subprocess boundary is unit-tested separately; these installer
            # tests exercise manifest/cache behavior with the in-process fake.
            vmi_module._warm_pocket_tts_sync(self._spec())
            yield "progress", sum(hf_cache.dir_size_bytes(Path(p)) for p in watch_dirs)
            yield "done", "pocket-tts-ready"

        monkeypatch.setattr(vmi_module, "_stream_warmup_subprocess", fake_warmup)
        # Faster timeouts for tests.
        monkeypatch.setattr(vmi_module, "_TTS_WARMUP_TIMEOUT_S", 10.0)
        yield vmi_module

    def _spec(self):
        from app.services.voice_model_installer import VoiceModelSpec

        return VoiceModelSpec(
            kind="tts",
            provider="pocket-tts",
            model="pocket-tts",
            language="english_2026-04",
            voice="mary",
        )

    def _cfg(self, spec):
        from app.services.voice_model_installer import VoiceConfig

        return VoiceConfig(asr=ASR_SPEC, tts=spec)

    async def _run(self, vmi, spec):
        events = []
        async for evt in vmi._install_tts(self._cfg(spec), spec, 0, 1):
            events.append(evt)
        return events

    @pytest.mark.asyncio
    async def test_natural_warm_records_manifest(self, tmp_path, monkeypatch, vmi):
        hub = tmp_path / "hf" / "hub"
        mod = _fake_pocket_tts_module(
            hub,
            {"model.safetensors": b"w" * 2_000_000, "tokenizer.model": b"t" * 512},
        )
        monkeypatch.setitem(sys.modules, "pocket_tts", mod)

        events = await self._run(vmi, self._spec())
        assert any(e["event"] == "pull_done" for e in events)
        assert not any(e["event"] == "pull_error" for e in events)

        manifest = vmi.read_manifest()
        entry = manifest["tts"]
        assert entry["store"] == "hf"
        assert TTS_HF_REPO in entry["hf_repos"]
        keys = list(entry["files"])
        assert any("model.safetensors" in k for k in keys)
        # The voice embedding was warmed too (get_state_for_audio_prompt).
        assert any("mary.safetensors" in k for k in keys)
        assert entry["complete"] is True

    @pytest.mark.asyncio
    async def test_natural_warm_failure_is_honest(self, tmp_path, monkeypatch, vmi):
        mod = _fake_pocket_tts_module(tmp_path / "hf" / "hub", {}, fail=True)
        monkeypatch.setitem(sys.modules, "pocket_tts", mod)

        events = await self._run(vmi, self._spec())
        errors = [e for e in events if e["event"] == "pull_error"]
        assert errors, "a failed warm-up must emit pull_error"
        assert "warm-up failed" in errors[0]["error"]
        # No manifest entry was written — voice stays honestly "not ready".
        assert "tts" not in vmi.read_manifest()

    @pytest.mark.asyncio
    async def test_package_backed_warmup_is_valid_without_hf_files(
        self, monkeypatch, vmi
    ):
        async def package_warmup(script, watch_dirs, timeout_s):
            yield "progress", 0
            yield "done", "pocket-tts-ready"

        monkeypatch.setattr(vmi, "_stream_warmup_subprocess", package_warmup)
        events = await self._run(vmi, self._spec())

        assert not any(e["event"] == "pull_error" for e in events)
        assert any(e["event"] == "pull_done" for e in events)
        entry = vmi.read_manifest()["tts"]
        assert entry["store"] == "package"
        assert entry["files"] == {}
        assert models_store.tts_ready() is True

    @pytest.mark.asyncio
    async def test_already_cached_warmup_still_records(
        self, tmp_path, monkeypatch, vmi
    ):
        hub = tmp_path / "hf" / "hub"
        # Pre-populate the cache BEFORE the "install" (no growth observed).
        mod = _fake_pocket_tts_module(hub, {"model.safetensors": b"w" * 4096})
        # First call materializes the cache without going through _install_tts.
        mod.TTSModel.load_model(language="english_2026-04")
        mod.TTSModel().get_state_for_audio_prompt("mary")
        monkeypatch.setitem(sys.modules, "pocket_tts", mod)

        events = await self._run(vmi, self._spec())
        assert any(e["event"] == "pull_done" for e in events)
        manifest = vmi.read_manifest()
        entry = manifest.get("tts")
        assert entry and entry["store"] == "hf"
        assert entry["hf_repos"].get(TTS_HF_REPO)


class TestPullLockAndRenameGuard:
    def test_pull_lock_serializes_concurrent_installs(self):
        from app.services import voice_model_installer as vmi

        lock = vmi._PULL_LOCK
        assert isinstance(lock, asyncio.Lock) or type(lock).__name__ == "Lock"

    @pytest.mark.asyncio
    async def test_second_install_waits_for_first(self, monkeypatch):
        from app.services import voice_model_installer as vmi

        events: list = []
        gate = asyncio.Event()

        async def fake_unlocked(profile, **kwargs):
            events.append("first_start")
            await gate.wait()
            events.append("first_end")
            yield {"event": "pull_done"}

        async def consume(gen):
            async for _ in gen:
                pass

        monkeypatch.setattr(vmi, "_stream_install_unlocked", fake_unlocked)
        t1 = asyncio.create_task(
            consume(vmi.stream_install_voice_dependencies("cpu_small"))
        )
        await asyncio.sleep(0.05)
        assert events == ["first_start"]
        # Second install must NOT start while the first holds the lock.
        t2 = asyncio.create_task(
            consume(vmi.stream_install_voice_dependencies("cpu_small"))
        )
        await asyncio.sleep(0.1)
        assert events == ["first_start"]
        gate.set()
        await asyncio.gather(t1, t2)
        # The first install ran to completion while the second WAITED; the
        # second then runs (idempotent re-check — no interleaving).
        assert events[:2] == ["first_start", "first_end"]
        assert events.count("first_start") == 2
        assert events == ["first_start", "first_end", "first_start", "first_end"]


class TestPocketEngineLocalLoad:
    """The runtime engine loads naturally from the persisted HF cache
    (offline-guarded) and refuses — never downloads — when the cache is
    cold. Legacy v2 local layouts still load offline from disk."""

    def _engine(self):
        from app.voice.tts.pocket import PocketTtsEngine

        spec = SimpleNamespace(
            provider="pocket-tts",
            model="pocket-tts",
            language="english_2026-04",
            voice="mary",
        )
        return PocketTtsEngine(spec)

    def _cold_cache_module(self, tmp_path):
        """A fake pocket_tts whose load_model fails like a cold offline
        cache (the offline guard makes hub misses raise immediately)."""
        import types

        class TTSModel:
            @staticmethod
            def load_model(language=None, config=None):
                raise OSError("entry not found in local cache (offline)")

        mod = types.ModuleType("pocket_tts")
        mod.__file__ = str(tmp_path / "fake" / "pocket_tts" / "__init__.py")
        mod.TTSModel = TTSModel
        return mod

    def test_load_refuses_without_local_assets(self, tmp_path, monkeypatch):
        from app.voice import models_store
        from app.voice.tts import TtsError

        monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path)
        monkeypatch.setattr(models_store, "_resolved_voice_config", lambda: None)
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: None)
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
        monkeypatch.setitem(
            sys.modules, "pocket_tts", self._cold_cache_module(tmp_path)
        )
        engine = self._engine()
        with pytest.raises(TtsError) as ei:
            engine._load_model()
        assert ei.value.code == "tts_not_ready"
        assert "setup wizard" in ei.value.message

    def test_load_natural_passes_language(self, tmp_path, monkeypatch):
        """The natural load path passes the profiles.yml language through —
        the exact released signature (verified against the user's working
        local pipeline: TTSModel.load_model(language=…))."""
        import types

        from app.voice import models_store

        monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path)
        monkeypatch.setattr(models_store, "tts_assets_dir", lambda: None)
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))

        seen: dict = {}

        class TTSModel:
            sample_rate = 24000

            @staticmethod
            def load_model(language=None, config=None):
                seen["language"] = language
                seen["offline"] = os.environ.get("HF_HUB_OFFLINE")
                return TTSModel()

        mod = types.ModuleType("pocket_tts")
        mod.__file__ = str(tmp_path / "fake" / "pocket_tts" / "__init__.py")
        mod.TTSModel = TTSModel
        monkeypatch.setitem(sys.modules, "pocket_tts", mod)

        engine = self._engine()
        model = engine._load_model()
        assert model is not None
        assert seen["language"] == "english_2026-04"
        # The natural load runs with the hub pinned OFFLINE (runtime loads
        # never touch the network — the setup wizard owns downloads).
        assert seen["offline"] == "1"
        # The engine captured the native output rate for resampling.
        assert engine._native_rate == 24000

    def test_load_passes_local_config_to_load_model(self, tmp_path, monkeypatch):
        import types

        from app.voice import models_store

        assets = tmp_path / "tts" / "pocket-tts"
        assets.mkdir(parents=True)
        (assets / "model.safetensors").write_bytes(b"w" * (2 * 1024 * 1024))
        (assets / "tokenizer.model").write_bytes(b"tok")
        (assets / "voice-mary.safetensors").write_bytes(b"voice")
        (assets / "local-config.yaml").write_text("weights_path: local\n")
        monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path)
        monkeypatch.setattr(
            models_store,
            "tts_assets_dir",
            lambda: assets,
        )

        seen: dict = {}

        class TTSModel:
            @staticmethod
            def load_model(language=None, config=None):
                seen["language"] = language
                seen["config"] = config
                return object()

        mod = types.ModuleType("pocket_tts")
        mod.__file__ = str(tmp_path / "fake" / "pocket_tts" / "__init__.py")
        mod.TTSModel = TTSModel
        monkeypatch.setitem(sys.modules, "pocket_tts", mod)

        engine = self._engine()
        model = engine._load_model()
        assert model is not None
        # The released signature: load_model(config=<local yaml path>).
        assert seen["config"] == str(assets / "local-config.yaml")
        assert seen["language"] is None

    def test_load_refuses_legacy_v1_layout(self, tmp_path, monkeypatch):
        """A legacy weights-only dir (no local-config.yaml) cannot load
        offline — the engine falls through to the natural hub path, which
        fails fast against a cold cache with a clear re-install message."""
        from app.voice import models_store
        from app.voice.tts import TtsError

        assets = tmp_path / "tts" / "pocket-tts"
        assets.mkdir(parents=True)
        (assets / "model.safetensors").write_bytes(b"w" * (2 * 1024 * 1024))
        monkeypatch.setattr(models_store, "MODELS_DIR", tmp_path)
        monkeypatch.setattr(
            models_store,
            "tts_assets_dir",
            lambda: assets,
        )
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
        monkeypatch.setitem(
            sys.modules, "pocket_tts", self._cold_cache_module(tmp_path)
        )

        engine = self._engine()
        with pytest.raises(TtsError) as ei:
            engine._load_model()
        assert ei.value.code == "tts_not_ready"
        assert "setup wizard" in ei.value.message

    @pytest.mark.asyncio
    async def test_warm_up_timeout_raises_clear_error(self, monkeypatch):
        from app.voice.tts import TtsError

        engine = self._engine()

        def slow_load():
            time.sleep(5)

        monkeypatch.setattr(engine, "_load_model", slow_load)
        monkeypatch.setattr(settings, "VOICE_TTS_LOAD_TIMEOUT_SEC", 1)
        import app.voice.tts.pocket as pocket_mod

        pocket_mod._MODEL_CACHE.clear()
        try:
            with pytest.raises(TtsError) as ei:
                await asyncio.wait_for(engine.warm_up(), timeout=10)
            assert ei.value.code == "tts_model_load_timeout"
        finally:
            pocket_mod._MODEL_CACHE.clear()
            engine._model = None

    @pytest.mark.asyncio
    async def test_enable_voice_cloning_replaces_catalog_fallback(self, monkeypatch):
        import app.voice.tts.pocket as pocket_mod

        engine = self._engine()
        catalog_model = SimpleNamespace(has_voice_cloning=False)
        cloning_model = SimpleNamespace(has_voice_cloning=True)

        async def warm():
            return catalog_model

        monkeypatch.setattr(engine, "warm_up", warm)
        monkeypatch.setattr(engine, "_load_cloning_model", lambda: cloning_model)
        pocket_mod._MODEL_CACHE.clear()
        pocket_mod._MODEL_CACHE[("pocket-tts", "english_2026-04")] = catalog_model
        try:
            result = await engine.enable_voice_cloning()
            assert result is cloning_model
            assert engine._model is cloning_model
            assert pocket_mod._MODEL_CACHE[("pocket-tts", "english_2026-04")] is cloning_model
        finally:
            pocket_mod._MODEL_CACHE.clear()
            engine._model = None

    @pytest.mark.asyncio
    async def test_concurrent_streams_hold_lock_for_full_iterator(self, monkeypatch):
        """Two sessions sharing a model must not interleave generation."""
        model = SimpleNamespace(sample_rate=24000)
        events = []

        def generate(_state, text):
            events.append(f"start:{text}")
            time.sleep(0.03)
            yield np.zeros(240, dtype=np.float32)
            time.sleep(0.03)
            events.append(f"end:{text}")

        model.generate_audio_stream = generate
        first, second = self._engine(), self._engine()

        async def warm():
            return model

        async def voice_state(_model):
            return object()

        for engine in (first, second):
            monkeypatch.setattr(engine, "warm_up", warm)
            monkeypatch.setattr(engine, "_voice_state", voice_state)

        async def consume(engine, text):
            return [chunk async for chunk in engine.synthesize(text)]

        await asyncio.gather(consume(first, "one"), consume(second, "two"))
        assert events in (
            ["start:one", "end:one", "start:two", "end:two"],
            ["start:two", "end:two", "start:one", "end:one"],
        )


# ══════════════════════════════════════════════════════════════════════
# Regression: TTS is voice-modality ONLY. Text chat (/api/chat/stream)
# must never trigger TTS synthesis — the assistant replies as plain
# streaming text. TTS requires an active voice session or an explicit
# read-aloud request to the voice REST endpoint.
# ══════════════════════════════════════════════════════════════════════


class TestTtsVoiceOnlyRegression:
    def test_text_chat_module_never_references_tts(self):
        """Static guarantee: the text-chat endpoint module contains no
        TTS usage — speech synthesis exists only in the voice pipeline."""
        import inspect

        from app.api import chat as chat_module

        source = inspect.getsource(chat_module)
        for forbidden in (
            "create_tts_engine",
            "PocketTtsEngine",
            "synthesize",
            "tts_chunk",
            "tts_start",
        ):
            assert forbidden not in source, (
                f"text chat module references TTS symbol {forbidden!r} — "
                "TTS must stay in explicitly requested voice playback paths"
            )

    def test_tts_engine_callers_are_voice_only(self):
        """Only voice calls, opt-in read aloud, and the TTS package may
        reference the factory. Ordinary text generation must never invoke TTS."""
        app_root = Path(__file__).resolve().parents[1] / "app"
        offenders = []
        for py in app_root.rglob("*.py"):
            try:
                text = py.read_text(encoding="utf-8")
            except OSError:
                continue
            if "create_tts_engine" in text:
                rel = py.relative_to(app_root).as_posix()
                if rel not in (
                    "voice/session.py",
                    "api/voice.py",  # POST /voice/speech: explicit read aloud
                    "voice/tts/__init__.py",
                    "voice/tts/pocket.py",
                ):
                    offenders.append(rel)
        assert (
            offenders == []
        ), f"create_tts_engine referenced outside the voice pipeline: {offenders}"


# ══════════════════════════════════════════════════════════════════════
# HF cache ownership (user directive: hub cache → the persisted data
# volume; engines load offline; setup downloads naturally)
# ══════════════════════════════════════════════════════════════════════


class TestHfCache:
    @pytest.fixture(autouse=True)
    def _pin_home(self, tmp_path, monkeypatch):
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
        monkeypatch.delenv("HF_HOME", raising=False)
        monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
        yield

    def test_hf_home_is_in_the_data_volume(self, tmp_path):
        assert hf_cache.hf_home() == tmp_path / "hf"
        assert hf_cache.hub_cache_dir() == tmp_path / "hf" / "hub"

    def test_ensure_hf_env_sets_home_and_creates_dir(self, tmp_path, monkeypatch):
        monkeypatch.delenv("HF_HOME", raising=False)
        home = hf_cache.ensure_hf_env()
        assert home == tmp_path / "hf"
        assert home.is_dir()
        assert os.environ.get("HF_HOME") == str(tmp_path / "hf")
        # An operator-set HF_HOME wins (docker-compose sets it explicitly).
        os.environ.pop("HF_HOME", None)

    def test_repo_cache_dir_layout(self):
        d = hf_cache.repo_cache_dir("Qwen/Qwen3-ASR-0.6B")
        assert d.name == "models--Qwen--Qwen3-ASR-0.6B"
        assert d.parent == hf_cache.hub_cache_dir()

    def test_snapshot_dir_resolves_via_refs(self, tmp_path):
        repo = hf_cache.repo_cache_dir("Qwen/Qwen3-ASR-0.6B")
        snap = repo / "snapshots" / "sha1"
        snap.mkdir(parents=True)
        (snap / "model.safetensors").write_bytes(b"w" * 16)
        (repo / "refs").mkdir(parents=True)
        (repo / "refs" / "main").write_text("sha1")
        resolved = hf_cache.snapshot_dir("Qwen/Qwen3-ASR-0.6B", "main")
        assert resolved == snap
        # Unknown revision → None (caller falls back to natural loading).
        assert hf_cache.snapshot_dir("Qwen/Qwen3-ASR-0.6B", "v2") is None

    def test_snapshot_files_lists_sizes(self, tmp_path):
        repo = hf_cache.repo_cache_dir("kyutai/pocket-tts-without-voice-cloning")
        snap = repo / "snapshots" / "sha2"
        (snap / "sub").mkdir(parents=True)
        (snap / "model.safetensors").write_bytes(b"w" * 100)
        (snap / "sub" / "tokenizer.model").write_bytes(b"t" * 10)
        (repo / "refs").mkdir(parents=True)
        (repo / "refs" / "main").write_text("sha2")
        files = hf_cache.snapshot_files("kyutai/pocket-tts-without-voice-cloning")
        assert files == {"model.safetensors": 100, "sub/tokenizer.model": 10}

    def test_dir_size_bytes_is_honest(self, tmp_path):
        d = tmp_path / "tree"
        d.mkdir()
        (d / "a.bin").write_bytes(b"x" * 100)
        (d / "nested").mkdir()
        (d / "nested" / "b.bin").write_bytes(b"x" * 50)
        assert hf_cache.dir_size_bytes(d) == 150
        assert hf_cache.dir_size_bytes(tmp_path / "missing") == 0

    def test_offline_hub_pins_and_restores(self):
        assert not hf_cache.is_offline()
        with hf_cache.offline_hub():
            assert hf_cache.is_offline()
        assert not hf_cache.is_offline()

    def test_offline_hub_restores_previous_value(self, monkeypatch):
        monkeypatch.setenv("HF_HUB_OFFLINE", "0")
        with hf_cache.offline_hub():
            assert os.environ["HF_HUB_OFFLINE"] == "1"
        assert os.environ["HF_HUB_OFFLINE"] == "0"

    @pytest.mark.asyncio
    async def test_run_monitored_reports_growth(self, tmp_path):
        watch = tmp_path / "repo"
        watch.mkdir()

        def grow() -> str:
            (watch / "blob").write_bytes(b"x" * 300)
            time.sleep(0.2)  # let the watcher observe the growth
            return "done"

        seen: list = []
        result = await hf_cache.run_monitored(grow, [watch], seen.append, 0.05)
        assert result == "done"
        assert seen and seen[-1] >= 300

    @pytest.mark.asyncio
    async def test_run_monitored_propagates_errors(self, tmp_path):
        def boom():
            raise RuntimeError("download failed")

        with pytest.raises(RuntimeError):
            await hf_cache.run_monitored(boom, [tmp_path], lambda n: None)


# ══════════════════════════════════════════════════════════════════════
# MLX streaming ASR engine — the REAL mlx_qwen3_asr surface
# (Session(model=…) + init_streaming/feed_audio/finish_streaming)
# ══════════════════════════════════════════════════════════════════════


class TestMlxStreamingAsrEngine:
    @pytest.fixture(autouse=True)
    def _reset(self, tmp_path, monkeypatch):
        from app.voice.asr import mlx_engine as mlx_mod

        mlx_mod._SESSION_CACHE.clear()
        mlx_mod._LOAD_FAILURES.clear()
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
        self.mlx_mod = mlx_mod
        self.calls: dict = {}
        yield
        mlx_mod._SESSION_CACHE.clear()
        mlx_mod._LOAD_FAILURES.clear()

    def _fake_mlx_module(self, tmp_path, seen):
        """A fake mlx_qwen3_asr with the REAL Session streaming API."""
        import types

        class StreamState:
            def __init__(self, text=""):
                self.text = text

        class Session:
            def __init__(self, model=None, revision=None):
                seen["model"] = model
                seen["offline"] = os.environ.get("HF_HUB_OFFLINE")
                self.fed: list = []

            def init_streaming(
                self, chunk_size_sec=2.0, max_context_sec=30.0, language=None
            ):
                seen["chunk_size_sec"] = chunk_size_sec
                seen["language"] = language
                return StreamState()

            def feed_audio(self, audio, state):
                self.fed.append(audio)
                return StreamState(text="hello wor")

            def finish_streaming(self, state):
                return StreamState(text="hello world")

        mod = types.ModuleType("mlx_qwen3_asr")
        mod.Session = Session
        mod.StreamState = StreamState
        return mod

    def _engine(self, language=None):
        from app.voice.asr.mlx_engine import MlxQwen3AsrEngine

        return MlxQwen3AsrEngine(
            SimpleNamespace(**{**vars(ASR_SPEC), "language": language})
        )

    @pytest.mark.asyncio
    async def test_streaming_flow_matches_real_api(self, tmp_path, monkeypatch):
        seen: dict = {}
        mod = self._fake_mlx_module(tmp_path, seen)
        monkeypatch.setitem(sys.modules, "mlx_qwen3_asr", mod)

        engine = self._engine(language="English")
        await engine.start_stream()
        assert seen["model"] == "Qwen/Qwen3-ASR-0.6B"
        assert seen["language"] == "English"
        # The runtime load runs with the hub pinned offline.
        assert seen["offline"] == "1"

        # 200 ms of silence-ish PCM, s16le mono 16k.
        pcm = (np.zeros(3200, dtype=np.float32) * 32767).astype("<i2").tobytes()
        await engine.feed_audio(pcm)
        partial = await engine.get_partial()
        assert partial == "hello wor"  # state.text IS the partial transcript

        final = await engine.finish_stream()
        assert final == "hello world"
        assert engine.status()["runtime"] == "mlx"

    @pytest.mark.asyncio
    async def test_in_place_streaming_api_feeds_each_chunk_once(
        self, tmp_path, monkeypatch
    ):
        """Compatibility with the user-validated API that mutates state."""
        import types

        class State:
            text = ""

        class Session:
            def __init__(self, model=None, **_kwargs):
                self.feed_count = 0

            def init_streaming(self, **_kwargs):
                return State()

            def feed_audio(self, audio, state):
                self.feed_count += 1
                state.text = "partial"
                return None

            def finish_streaming(self, state):
                return "final text"

        mod = types.ModuleType("mlx_qwen3_asr")
        mod.Session = Session
        monkeypatch.setitem(sys.modules, "mlx_qwen3_asr", mod)
        engine = self._engine()
        await engine.start_stream()
        await engine.feed_audio(np.zeros(160, dtype="<i2").tobytes())
        assert engine._session.feed_count == 1
        assert await engine.get_partial() == "partial"
        assert await engine.finish_stream() == "final text"

    @pytest.mark.asyncio
    async def test_session_load_refuses_cold_cache(self, tmp_path, monkeypatch):
        import types

        class Session:
            def __init__(self, model=None, revision=None):
                raise OSError("not in the local cache (offline mode)")

        mod = types.ModuleType("mlx_qwen3_asr")
        mod.Session = Session
        monkeypatch.setitem(sys.modules, "mlx_qwen3_asr", mod)

        from app.voice.asr import AsrError

        engine = self._engine()
        with pytest.raises(AsrError) as ei:
            await engine.start_stream()
        assert ei.value.code == "asr_not_ready"
        assert "setup wizard" in ei.value.message
        # Negative cache: an immediate retry is suppressed, not re-attempted.
        with pytest.raises(AsrError) as ei2:
            await engine.warm_up()
        assert ei2.value.code == "asr_load_failed_recently"

    @pytest.mark.asyncio
    async def test_shared_session_cache(self, tmp_path, monkeypatch):
        seen: dict = {}
        mod = self._fake_mlx_module(tmp_path, seen)
        monkeypatch.setitem(sys.modules, "mlx_qwen3_asr", mod)

        e1, e2 = self._engine(), self._engine()
        await e1.warm_up()
        await e2.warm_up()
        assert len(self.mlx_mod._SESSION_CACHE) == 1
        assert e1._session is e2._session


# ══════════════════════════════════════════════════════════════════════
# ASR engine factory — runtime selection (auto prefers native MLX)
# ══════════════════════════════════════════════════════════════════════


class TestAsrRuntimeFactory:
    def _spec(self, runtime=None):
        return SimpleNamespace(
            provider="qwen3-asr",
            model="Qwen/Qwen3-ASR-0.6B",
            revision="main",
            runtime=runtime,
        )

    def test_explicit_mlx(self):
        from app.voice.asr import create_asr_engine
        from app.voice.asr.mlx_engine import MlxQwen3AsrEngine

        engine = create_asr_engine(self._spec("mlx"))
        assert isinstance(engine, MlxQwen3AsrEngine)

    def test_explicit_transformers(self, monkeypatch):
        from app.voice.asr import create_asr_engine
        from app.voice.asr.qwen3 import Qwen3AsrEngine

        monkeypatch.setattr("app.voice.asr._prefer_mlx_runtime", lambda spec: False)
        engine = create_asr_engine(self._spec("transformers"))
        assert isinstance(engine, Qwen3AsrEngine)

    def test_auto_prefers_mlx_when_importable(self, monkeypatch):
        from app.voice.asr import _prefer_mlx_runtime

        monkeypatch.setattr(
            "importlib.util.find_spec",
            lambda name: object() if name == "mlx_qwen3_asr" else None,
        )
        assert _prefer_mlx_runtime(self._spec(None)) is True
        assert _prefer_mlx_runtime(self._spec("auto")) is True

    def test_auto_falls_back_without_mlx(self, monkeypatch):
        from app.voice.asr import _prefer_mlx_runtime

        monkeypatch.setattr("importlib.util.find_spec", lambda name: None)
        assert _prefer_mlx_runtime(self._spec(None)) is False
        assert _prefer_mlx_runtime(self._spec("auto")) is False


# ══════════════════════════════════════════════════════════════════════
# Pip persistence (LibreOffice parity): wheelhouse + offline replay
# ══════════════════════════════════════════════════════════════════════


class TestPipPersistence:
    @pytest.fixture(autouse=True)
    def _pin_dirs(self, tmp_path, monkeypatch):
        from app.services import pip_persistence as pp

        monkeypatch.setattr(pp, "_WHEELHOUSE_OVERRIDE", tmp_path / "wheels")
        monkeypatch.setattr(pp, "_PIP_CACHE_OVERRIDE", tmp_path / "cache")
        monkeypatch.setattr(pp, "_SITE_PACKAGES_OVERRIDE", tmp_path / "site")
        self.pp = pp
        yield

    def test_wheelhouse_has_canonical_match(self, tmp_path):
        pp = self.pp
        house = pp.wheelhouse_dir()
        house.mkdir(parents=True)
        assert pp.wheelhouse_has("pocket-tts") is False
        # pip artifacts use underscore spellings on PyPI.
        (house / "pocket_tts-1.0.0-py3-none-any.whl").write_bytes(b"w")
        assert pp.wheelhouse_has("pocket-tts") is True
        assert pp.wheelhouse_has("pocket_tts") is True
        assert pp.wheelhouse_has("Pocket.TTS") is True
        # A different package does not match (boundary required).
        assert pp.wheelhouse_has("pocket-tts-extra") is False
        (house / "webrtcvad_wheels-0.0.1.tar.gz").write_bytes(b"w")
        assert pp.wheelhouse_has("webrtcvad-wheels") is True

    @pytest.mark.asyncio
    async def test_wheelhouse_hit_installs_offline(self, monkeypatch):
        pp = self.pp
        house = pp.wheelhouse_dir()
        house.mkdir(parents=True)
        (house / "pocket_tts-1.0.0-py3-none-any.whl").write_bytes(b"w")

        commands: list = []

        async def fake_stream(cmd, timeout):
            commands.append(list(cmd))
            yield "offline install ok", 0

        monkeypatch.setattr(pp, "_stream_subprocess", fake_stream)
        lines = [ln async for ln in pp.pip_install_persistent("pocket-tts")]
        assert any("wheelhouse hit" in ln for ln in lines)
        # The only pip invocation is the offline install (no download).
        assert len(commands) == 1
        assert "--no-index" in commands[0]
        assert "--find-links" in commands[0]
        assert commands[0][commands[0].index("--target") + 1] == str(
            pp.persistent_site_packages_dir()
        )
        assert "--constraint" in commands[0]

    def test_exact_reinstall_removes_ambiguous_target_metadata(self, monkeypatch):
        pp = self.pp
        target = pp.persistent_site_packages_dir()
        for version in ("1.12.0", "1.15.0"):
            info = target / f"accelerate-{version}.dist-info"
            info.mkdir(parents=True)
            (info / "METADATA").write_text(
                f"Metadata-Version: 2.1\nName: accelerate\nVersion: {version}\n"
            )
        monkeypatch.setattr(pp, "module_importable", lambda module: True)

        assert pp.package_satisfied("accelerate==1.12.0", "accelerate") is False
        pp._prepare_exact_reinstall("accelerate==1.12.0")
        assert not list(target.glob("accelerate-*.dist-info"))

    @pytest.mark.asyncio
    async def test_wheelhouse_miss_downloads_then_installs(self, monkeypatch):
        pp = self.pp

        commands: list = []

        async def fake_stream(cmd, timeout):
            commands.append(list(cmd))
            if cmd[3] == "download":
                (pp.wheelhouse_dir()).mkdir(parents=True, exist_ok=True)
                (pp.wheelhouse_dir() / "pocket_tts-1.0.0-py3-none-any.whl").write_bytes(
                    b"w"
                )
                yield "saved wheel", 0
            else:
                yield "installed", 0

        monkeypatch.setattr(pp, "_stream_subprocess", fake_stream)
        lines = [ln async for ln in pp.pip_install_persistent("pocket-tts")]
        assert any("wheelhouse miss" in ln for ln in lines)
        assert any("installed from the persistent wheelhouse" in ln for ln in lines)
        # download → offline install (exactly one download).
        assert [c[3] for c in commands] == ["download", "install"]

    @pytest.mark.asyncio
    async def test_replay_skips_importable_packages(self, monkeypatch):
        pp = self.pp
        installed: list = []

        async def fake_install(name, extra=(), install_timeout_s=600.0):
            installed.append(name)
            yield "ok"

        monkeypatch.setattr(pp, "pip_install_persistent", fake_install)
        monkeypatch.setattr(
            pp, "package_satisfied", lambda name, mod: mod == "pocket_tts"
        )
        lines = await pp.replay_voice_runtime_on_startup(
            [
                {"pip_name": "pocket-tts", "module": "pocket_tts", "display": "Pocket"},
                {
                    "pip_name": "webrtcvad-wheels",
                    "module": "webrtcvad",
                    "display": "VAD",
                },
            ]
        )
        assert installed == ["webrtcvad-wheels"]
        assert any("already importable" in ln for ln in lines)

    @pytest.mark.asyncio
    async def test_replay_never_raises(self, monkeypatch):
        pp = self.pp

        async def failing_install(name, extra=(), install_timeout_s=600.0):
            raise RuntimeError("no network")
            yield "never"

        monkeypatch.setattr(pp, "pip_install_persistent", failing_install)
        monkeypatch.setattr(pp, "package_satisfied", lambda name, mod: False)
        lines = await pp.replay_voice_runtime_on_startup(
            [{"pip_name": "pocket-tts", "module": "pocket_tts", "display": "Pocket"}]
        )
        assert any("ERROR" in ln for ln in lines)


class TestWarmupSubprocess:
    @pytest.mark.asyncio
    async def test_timeout_terminates_child(self, tmp_path, monkeypatch):
        from app.services import pip_persistence as pp
        from app.services import voice_model_installer as vmi

        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
        monkeypatch.setattr(pp, "_SITE_PACKAGES_OVERRIDE", tmp_path / "site")
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="timed out"):
            async for _ in vmi._stream_warmup_subprocess(
                "import time; time.sleep(30)", [tmp_path / "hf"], 0.1
            ):
                pass
        assert time.monotonic() - started < 3.0


# ══════════════════════════════════════════════════════════════════════
# ASR natural install — snapshot_download into the persisted cache
# (+ legacy snapshot seeding)
# ══════════════════════════════════════════════════════════════════════


class TestAsrNaturalInstall:
    @pytest.fixture(autouse=True)
    def _pin(self, tmp_path, monkeypatch):
        from app.services import voice_model_installer as vmi

        models_root = tmp_path / "models"
        monkeypatch.setattr(vmi, "_MODELS_DIR_OVERRIDE", models_root)
        monkeypatch.setattr(models_store, "MODELS_DIR", models_root)
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
        monkeypatch.setattr(models_store, "_resolved_voice_config", lambda: FAKE_CONFIG)
        monkeypatch.setattr(vmi, "_ASR_WARMUP_TIMEOUT_S", 10.0)

        async def fake_warmup(script, watch_dirs, timeout_s):
            mod = sys.modules["huggingface_hub"]
            path = mod.snapshot_download("Qwen/Qwen3-ASR-0.6B", revision="main")
            yield "progress", sum(hf_cache.dir_size_bytes(Path(p)) for p in watch_dirs)
            yield "done", path

        monkeypatch.setattr(vmi, "_stream_warmup_subprocess", fake_warmup)
        self.vmi = vmi
        self.hub = tmp_path / "hf" / "hub"
        yield vmi

    def _spec(self):
        return self.vmi.VoiceModelSpec(
            kind="asr",
            provider="qwen3-asr",
            model="Qwen/Qwen3-ASR-0.6B",
            revision="main",
        )

    def _cfg(self):
        return self.vmi.VoiceConfig(asr=self._spec(), tts=TTS_SPEC)

    def _fake_hub_module(self):
        """A fake huggingface_hub: model_info listing + snapshot_download
        that materializes the repo into the pinned cache layout."""
        import types

        hub = self.hub

        class Sibling:
            def __init__(self, rfilename, size):
                self.rfilename = rfilename
                self.size = size

        class Info:
            sha = "feed42"
            siblings = [
                Sibling("config.json", 731),
                Sibling("model.safetensors", 1024),
            ]

        class HfApi:
            def model_info(self, repo_id, revision=None, files_metadata=False):
                return Info()

        def snapshot_download(repo_id, revision=None):
            root = hub / "models--Qwen--Qwen3-ASR-0.6B"
            snap = root / "snapshots" / "feed42"
            snap.mkdir(parents=True, exist_ok=True)
            (snap / "config.json").write_bytes(b"c" * 731)
            (snap / "model.safetensors").write_bytes(b"w" * 1024)
            (root / "refs").mkdir(parents=True, exist_ok=True)
            (root / "refs" / "main").write_text("feed42")
            return str(snap)

        mod = types.ModuleType("huggingface_hub")
        mod.HfApi = HfApi
        mod.snapshot_download = snapshot_download
        return mod

    @pytest.mark.asyncio
    async def test_snapshot_download_records_manifest(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "huggingface_hub", self._fake_hub_module())
        events = []
        async for evt in self.vmi._install_asr_model(self._cfg(), self._spec(), 0, 1):
            events.append(evt)
        assert not any(e["event"] == "pull_error" for e in events), events
        progresses = [e for e in events if e["event"] == "pull_progress"]
        assert progresses and progresses[-1]["percent"] == 100

        entry = self.vmi.read_manifest()["asr"]
        assert entry["store"] == "hf"
        assert entry["hf_repos"]["Qwen/Qwen3-ASR-0.6B"] == "snapshots/feed42"
        assert entry["files"]["Qwen/Qwen3-ASR-0.6B::model.safetensors"] == 1024
        # The models_store gate agrees the install is valid.
        assert models_store.asr_ready() is True

    @pytest.mark.asyncio
    async def test_listing_failure_is_honest(self, monkeypatch):
        import types

        mod = types.ModuleType("huggingface_hub")

        class HfApi:
            def model_info(self, *a, **k):
                raise RuntimeError("no network")

        mod.HfApi = HfApi
        monkeypatch.setitem(sys.modules, "huggingface_hub", mod)
        events = []
        async for evt in self.vmi._install_asr_model(self._cfg(), self._spec(), 0, 1):
            events.append(evt)
        errors = [e for e in events if e["event"] == "pull_error"]
        assert errors and "Failed to list model files" in errors[0]["error"]
        assert "asr" not in self.vmi.read_manifest()

    def test_seeding_registers_legacy_files(self, tmp_path):
        legacy = tmp_path / "models" / "asr" / "Qwen3-ASR-0.6B"
        legacy.mkdir(parents=True)
        (legacy / "config.json").write_bytes(b"c" * 731)
        (legacy / "model.safetensors").write_bytes(b"w" * 1024)
        # The seeding needs hf metadata (blob ids) → fake hub module.
        import types

        class Sibling:
            def __init__(self, rfilename, size):
                self.rfilename = rfilename
                self.size = size
                self.blob_id = "b" + rfilename
                self.lfs = None

        class Info:
            sha = "feed42"
            siblings = [
                Sibling("config.json", 731),
                Sibling("model.safetensors", 1024),
            ]

        class HfApi:
            def model_info(self, repo_id, revision=None, files_metadata=False):
                return Info()

        mod = types.ModuleType("huggingface_hub")
        mod.HfApi = HfApi
        old = sys.modules.get("huggingface_hub")
        sys.modules["huggingface_hub"] = mod
        try:
            seeded = self.vmi._seed_legacy_asr_into_cache(self._spec())
            assert seeded == 2
            root = hf_cache.repo_cache_dir("Qwen/Qwen3-ASR-0.6B")
            assert (root / "refs" / "main").read_text() == "feed42"
            snap = root / "snapshots" / "feed42"
            assert (snap / "model.safetensors").stat().st_size == 1024
            # Blobs registered for both files.
            assert (root / "blobs" / "bmodel.safetensors").exists()
            assert (root / "blobs" / "bconfig.json").exists()
        finally:
            if old is not None:
                sys.modules["huggingface_hub"] = old
            else:
                sys.modules.pop("huggingface_hub", None)

    def test_runtime_manifest_recording(self, monkeypatch):
        monkeypatch.setattr(self.vmi, "_runtime_package_satisfied", lambda pkg: True)
        self.vmi._record_runtime_manifest(self._cfg())
        entry = self.vmi.read_manifest().get("runtime")
        assert entry and entry["packages"]
        pip_names = {p["pip_name"] for p in entry["packages"]}
        # Cross-platform set includes the ASR runtime stack.
        assert any(name.startswith("huggingface-hub==") for name in pip_names)
        assert any(name.startswith("pocket-tts==") for name in pip_names)
        assert any(name.startswith("soundfile==") for name in pip_names)


# ══════════════════════════════════════════════════════════════════════
# Cross-cutting regression: runtime loads NEVER touch the network
# (offline_hub pins HF_HUB_OFFLINE around every engine model load).
# ══════════════════════════════════════════════════════════════════════


class TestOfflineLoadRegression:
    def test_mlx_and_transformers_loads_are_offline_guarded(self):
        """Static guarantee: both ASR engines + the TTS engine wrap their
        model loads in offline_hub (the user's hang bug: a cold cache
        started a mid-conversation download instead of failing fast)."""
        import inspect

        from app.voice.asr import mlx_engine as mlx_mod
        from app.voice.asr import qwen3 as qwen_mod
        from app.voice.tts import pocket as pocket_mod

        for module, owner_name, probe in (
            (mlx_mod, "MlxQwen3AsrEngine", "_load_session_sync"),
            (qwen_mod, "Qwen3AsrEngine", "_load_model_sync"),
            (pocket_mod, "PocketTtsEngine", "_construct_model_natural"),
        ):
            method = getattr(getattr(module, owner_name), probe)
            src = inspect.getsource(method)
            assert "offline_hub" in src, (
                f"{module.__name__}.{owner_name}.{probe} must run inside " "offline_hub"
            )


# ══════════════════════════════════════════════════════════════════════
# FULL installer pipeline (the user's setup flow, end to end):
# pip runtime (wheelhouse) → ASR snapshot_download → TTS natural warm-up,
# all through stream_install_voice_dependencies with fakes — then the
# manifest must gate as READY for the voice session.
# ══════════════════════════════════════════════════════════════════════


class TestFullInstallPipeline:
    @pytest.fixture(autouse=True)
    def _pin(self, tmp_path, monkeypatch):
        from app.services import pip_persistence as pp
        from app.services import voice_model_installer as vmi

        models_root = tmp_path / "models"
        monkeypatch.setattr(vmi, "_MODELS_DIR_OVERRIDE", models_root)
        monkeypatch.setattr(models_store, "MODELS_DIR", models_root)
        monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", str(tmp_path / "hf"))
        monkeypatch.setattr(pp, "_WHEELHOUSE_OVERRIDE", tmp_path / "wheels")
        monkeypatch.setattr(pp, "_PIP_CACHE_OVERRIDE", tmp_path / "cache")
        monkeypatch.setattr(pp, "_SITE_PACKAGES_OVERRIDE", tmp_path / "site")
        monkeypatch.setattr(models_store, "_resolved_voice_config", lambda: FAKE_CONFIG)
        monkeypatch.setattr(models_store, "_pocket_tts_importable", lambda: True)
        # Nothing importable → runtime packages must be "installed".
        monkeypatch.setattr(vmi, "_module_importable", lambda m: False)
        monkeypatch.setattr(vmi, "_TTS_WARMUP_TIMEOUT_S", 10.0)
        monkeypatch.setattr(vmi, "_ASR_WARMUP_TIMEOUT_S", 10.0)

        async def fake_warmup(script, watch_dirs, timeout_s):
            if "snapshot_download" in script:
                path = sys.modules["huggingface_hub"].snapshot_download(
                    "Qwen/Qwen3-ASR-0.6B", revision="main"
                )
                yield "progress", sum(
                    hf_cache.dir_size_bytes(Path(p)) for p in watch_dirs
                )
                yield "done", path
            else:
                vmi._warm_pocket_tts_sync(TTS_SPEC)
                yield "progress", sum(
                    hf_cache.dir_size_bytes(Path(p)) for p in watch_dirs
                )
                yield "done", "pocket-tts-ready"

        monkeypatch.setattr(vmi, "_stream_warmup_subprocess", fake_warmup)
        self.vmi = vmi
        self.pp = pp
        self.hub = tmp_path / "hf" / "hub"
        self.events: list = []
        yield

    def _fake_pip(self, monkeypatch):
        pp = self.pp
        commands: list = []
        state = {"installed": False}

        def fake_importable(module: str) -> bool:
            return state["installed"]

        async def fake_install(name, extra=(), install_timeout_s=600.0):
            commands.append((name, tuple(extra)))
            state["installed"] = True
            yield f"installed {name} (fake wheelhouse)"

        monkeypatch.setattr(pp, "pip_install_persistent", fake_install)
        # Nothing importable at plan time → all runtime packages install;
        # the first fake pip call flips the switch (like a real install).
        monkeypatch.setattr(
            self.vmi, "_runtime_package_satisfied", lambda p: state["installed"]
        )
        return commands

    def _fake_hub(self):
        import types

        hub = self.hub

        class Sibling:
            def __init__(self, rfilename, size):
                self.rfilename = rfilename
                self.size = size

        class Info:
            sha = "feed42"
            siblings = [Sibling("config.json", 731), Sibling("model.safetensors", 1024)]

        class HfApi:
            def model_info(self, repo_id, revision=None, files_metadata=False):
                return Info()

        def snapshot_download(repo_id, revision=None):
            root = hub / "models--Qwen--Qwen3-ASR-0.6B"
            snap = root / "snapshots" / "feed42"
            snap.mkdir(parents=True, exist_ok=True)
            (snap / "config.json").write_bytes(b"c" * 731)
            (snap / "model.safetensors").write_bytes(b"w" * 1024)
            (root / "refs").mkdir(parents=True, exist_ok=True)
            (root / "refs" / "main").write_text("feed42")
            return str(snap)

        mod = types.ModuleType("huggingface_hub")
        mod.HfApi = HfApi
        mod.snapshot_download = snapshot_download
        return mod

    def _fake_tts(self):
        return _fake_pocket_tts_module(self.hub, {"model.safetensors": b"w" * 8192})

    @pytest.mark.asyncio
    async def test_setup_flow_makes_voice_ready(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "huggingface_hub", self._fake_hub())
        monkeypatch.setitem(sys.modules, "pocket_tts", self._fake_tts())
        self._fake_pip(monkeypatch)

        events = []
        async for evt in self.vmi.stream_install_voice_dependencies(
            profile=None, only=None
        ):
            events.append(evt)
        kinds = [(e["event"], e.get("kind"), e.get("model")) for e in events]
        assert any(k[0] == "pull_done" and k[1] == "voice_runtime" for k in kinds)
        assert any(k[0] == "pull_done" and k[1] == "voice_model" for k in kinds)
        assert not any(e["event"] == "pull_error" for e in events), events

        # The manifest gates the voice session READY.
        status = models_store.voice_dependency_status()
        assert status["ready"] is True, status
        assert status["asr"]["store"] == "hf"
        assert status["tts"]["store"] == "hf"
        # The pip replay manifest recorded the runtime packages.
        runtime = models_store.runtime_manifest_entry()
        assert runtime
        pip_names = {p["pip_name"] for p in runtime["packages"]}
        assert any(name.startswith("pocket-tts==") for name in pip_names)
        assert any(name.startswith("huggingface-hub==") for name in pip_names)

    @pytest.mark.asyncio
    async def test_setup_flow_is_idempotent(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "huggingface_hub", self._fake_hub())
        monkeypatch.setitem(sys.modules, "pocket_tts", self._fake_tts())
        self._fake_pip(monkeypatch)

        first = []
        async for evt in self.vmi.stream_install_voice_dependencies(profile=None):
            first.append(evt)
        second = []
        async for evt in self.vmi.stream_install_voice_dependencies(profile=None):
            second.append(evt)
        skips = [
            e
            for e in second
            if e["event"] == "pull_done" and e.get("already_installed")
        ]
        assert len(skips) == 2, "both models should be idempotently skipped"
