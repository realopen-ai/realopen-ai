"""Protocol-level tests for the voice WebSocket session (app/voice/session.py).

Strategy (no external services, no real WS server):
- ``FakeWebSocket`` — an asyncio-Queue-based stand-in compatible with the
  subset of starlette's WebSocket API the session uses (receive / send_json
  / send_bytes / close).
- ``ScriptedAsr`` / ``ScriptedTts`` — deterministic engines substituted via
  monkeypatched factories.
- ``run_agent_stream`` is monkeypatched to yield scripted SSE strings — the
  test asserts the session CALLS it and forwards the parsed event dicts
  VERBATIM as ``agent_event`` frames (no duplicated agent logic).
- persistence / memory extraction / stream markers are monkeypatched with
  recorders (DB optional).

Coverage:
- full happy-path frame sequence (ready → asr_partial → asr_final →
  user_message → agent_event(s) → tts_start → tts_chunk+0x03 (adjacency!) →
  tts_end → assistant_message → state LISTENING) and persistence calls with
  modality="voice";
- barge-in from SPEAKING: ``interrupted`` frame, TTS generation cancelled
  (no further 0x03 after it), state → LISTENING, new utterance captured
  including pre-roll samples, stale generation audio never re-sent;
- client ``interrupt`` JSON frame;
- frame validation (mic before start, oversize payload, bad prefix, ping);
- not-ready / disabled handshake failures;
- stop and idle-timeout shutdown;
- manager: one session per conversation, second connection rejected.
"""

import asyncio
import json
import sys
import time
import uuid as uuid_mod
from pathlib import Path
from types import SimpleNamespace
from typing import Any, List, Optional, Tuple

import numpy as np
import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.voice.session as vs  # noqa: E402
from app.config import settings  # noqa: E402
from app.voice import models_store  # noqa: E402
from app.voice.aec import PassThroughAec  # noqa: E402
from app.voice.manager import VoiceSessionManager  # noqa: E402
from app.voice.session import VoiceSession  # noqa: E402

# ══════════════════════════════════════════════════════════════════════
# Fakes
# ══════════════════════════════════════════════════════════════════════


class FakeWebSocket:
    """Asyncio-Queue WebSocket double speaking the session's API subset.

    Client helpers (client_text / client_binary / client_disconnect) feed
    the inbox; everything the session sends is appended to ``sent`` in
    order (so adjacency assertions are possible).
    """

    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.sent: List[Tuple[str, Any]] = []
        self.accepted = False
        self.closed = False
        self.close_code: Optional[int] = None

    # ── server-side API ──────────────────────────────────────────
    async def accept(self) -> None:
        self.accepted = True

    async def send_json(self, data: dict) -> None:
        self.sent.append(("json", data))

    async def send_bytes(self, data: bytes) -> None:
        self.sent.append(("bytes", bytes(data)))

    async def receive(self) -> dict:
        item = await self.inbox.get()
        if item is None:
            return {"type": "websocket.disconnect", "code": 1000}
        if isinstance(item, str):
            return {"type": "websocket.receive", "text": item}
        return {"type": "websocket.receive", "bytes": bytes(item)}

    async def close(self, code: int = 1000) -> None:
        if self.closed:
            return
        self.closed = True
        self.close_code = code
        # Wake any pending receive so the session observes the disconnect.
        self.inbox.put_nowait(None)

    # ── client-side helpers ──────────────────────────────────────
    def client_text(self, obj: Any) -> None:
        self.inbox.put_nowait(obj if isinstance(obj, str) else json.dumps(obj))

    def client_json(self, obj: dict) -> None:
        self.inbox.put_nowait(json.dumps(obj))

    def client_binary(self, prefix: int, payload: bytes) -> None:
        self.inbox.put_nowait(bytes([prefix]) + payload)

    def client_disconnect(self) -> None:
        self.inbox.put_nowait(None)

    # ── assertions helpers ───────────────────────────────────────
    def json_frames(self) -> List[dict]:
        return [d for kind, d in self.sent if kind == "json"]

    def frame_types(self) -> List[str]:
        return [d.get("type") for d in self.json_frames()]

    def binary_frames(self) -> List[bytes]:
        return [d for kind, d in self.sent if kind == "bytes"]

    def frames_of(self, ftype: str) -> List[dict]:
        return [d for d in self.json_frames() if d.get("type") == ftype]


class ScriptedAsr:
    """Deterministic ASR double; records every fed byte per stream."""

    def __init__(self, final: str = "hello world", partial: str = "hello wor"):
        self.final = final
        self.partial = partial
        self.streams: List[bytes] = []
        self.current: Optional[bytearray] = None
        self.cancel_count = 0
        self.start_count = 0

    async def start_stream(self) -> None:
        self.start_count += 1
        self.current = bytearray()
        self.streams.append(self.current)

    async def feed_audio(self, pcm: bytes) -> None:
        if self.current is not None:
            self.current += pcm

    async def get_partial(self) -> str:
        return self.partial

    async def finish_stream(self) -> str:
        self.current = None
        return self.final

    async def cancel(self) -> None:
        self.cancel_count += 1
        self.current = None

    def status(self) -> dict:
        return {"provider": "scripted", "model": "scripted-asr"}


class ScriptedTts:
    """Deterministic TTS double; yields slowly so cancellation is testable."""

    def __init__(self, chunks: int = 3, delay: float = 0.01):
        self.chunks = chunks
        self.delay = delay
        self.synthesized: List[str] = []
        self.warm_count = 0
        self.cancel_count = 0

    async def warm_up(self) -> None:
        self.warm_count += 1

    async def synthesize(self, text: str):
        self.synthesized.append(text)
        for i in range(self.chunks):
            if self.cancel_count > 0:
                return
            await asyncio.sleep(self.delay)
            yield b"PCM" + bytes([i])

    async def cancel(self) -> None:
        self.cancel_count += 1

    def status(self) -> dict:
        return {"provider": "scripted", "model": "scripted-tts"}


def make_agent_stream(events: List[dict], delay: float = 0.0, record=None):
    """Build a run_agent_stream stand-in yielding scripted SSE strings."""

    async def fake_run_agent_stream(
        messages=None,
        model="default",
        images=None,
        conversation_id=None,
        on_tool_call_start=None,
        on_tool_call_update=None,
    ):
        if record is not None:
            record.append(
                {
                    "messages": messages,
                    "model": model,
                    "conversation_id": conversation_id,
                }
            )
        try:
            for ev in events:
                if delay:
                    await asyncio.sleep(delay)
                yield "data: " + json.dumps(ev) + "\n\n"
        except (asyncio.CancelledError, GeneratorExit):
            if record is not None:
                record.append({"cancelled": True})
            raise

    return fake_run_agent_stream


# ── audio helpers ─────────────────────────────────────────────────────


def speech_pcm(ms: int, amp: int = 9000) -> bytes:
    n = 16000 * ms // 1000
    t = np.arange(n) / 16000.0
    wave = (np.sin(2 * np.pi * 440.0 * t) * amp).astype("<i2")
    return wave.tobytes()


def silence_pcm(ms: int) -> bytes:
    return b"\x00" * (16000 * ms // 1000 * 2)


def mic_frame(pcm: bytes) -> None:
    raise NotImplementedError  # (helper clarity — use ws.client_binary)


async def wait_until(predicate, timeout: float = 8.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# ══════════════════════════════════════════════════════════════════════
# Fixture: a fully scripted environment
# ══════════════════════════════════════════════════════════════════════

ASSISTANT_TEXT = (
    "Hello! This is the first spoken sentence from the assistant voice. "
    "And here is a second sentence so the chunker emits two chunks."
)


def assistant_events() -> List[dict]:
    words = ASSISTANT_TEXT.split(" ")
    events = [
        {"event": "thinking_start"},
        {"event": "thinking", "thinking": "user said hello"},
        {"event": "thinking_done", "thinkingDuration": 1},
    ]
    for w in words:
        events.append({"event": "message", "message": {"content": w + " "}})
    events.append({"event": "generation_done", "generationDuration": 3})
    events.append({"event": "done"})
    return events


@pytest.fixture()
def env(monkeypatch):
    """Patch every external of the voice session with a recording fake."""
    conv_id = uuid_mod.uuid4()
    scripted_asr = ScriptedAsr()
    scripted_tts = ScriptedTts()
    agent_calls: List[dict] = []
    agent_stream = make_agent_stream(assistant_events(), record=agent_calls)
    persist_calls: List[dict] = []
    memory_calls: List[dict] = []
    stream_marks: List[str] = []

    async def fake_persist(conv_id_, role, content, model=None, **kwargs):
        call = {"conv_id": conv_id_, "role": role, "content": content}
        call.update({"model": model})
        call.update(kwargs)
        persist_calls.append(call)
        return uuid_mod.uuid4() if content else None

    async def fake_memory(conv_id_, content, messages):
        memory_calls.append(
            {"conv_id": conv_id_, "content": content, "messages": messages}
        )
        return (0, False, False)

    async def fake_active(cid: str) -> None:
        stream_marks.append(f"active:{cid}")

    async def fake_idle(cid: str) -> None:
        stream_marks.append(f"idle:{cid}")

    async def fake_model_label() -> str:
        return "test-model"

    monkeypatch.setattr(vs, "create_asr_engine", lambda spec: scripted_asr)
    monkeypatch.setattr(vs, "create_tts_engine", lambda spec: scripted_tts)
    monkeypatch.setattr(vs, "run_agent_stream", agent_stream)
    monkeypatch.setattr(vs, "persist_message_standalone", fake_persist)
    monkeypatch.setattr(vs, "maybe_run_memory_extraction", fake_memory)
    monkeypatch.setattr(vs, "mark_stream_active", fake_active)
    monkeypatch.setattr(vs, "mark_stream_idle", fake_idle)
    monkeypatch.setattr(vs, "_resolve_model_label", fake_model_label)
    monkeypatch.setattr(vs, "create_aec", lambda mode=None: PassThroughAec())
    monkeypatch.setattr(models_store, "asr_ready", lambda: True)
    monkeypatch.setattr(models_store, "tts_ready", lambda: True)
    monkeypatch.setattr(vs, "ASR_PARTIAL_INTERVAL_S", 0)
    # Fast VAD silence (90 ms → 3 frames) for quick utterance ends.
    monkeypatch.setattr(settings, "VOICE_VAD_SILENCE_MS", 90)

    return SimpleNamespace(
        conv_id=conv_id,
        asr=scripted_asr,
        tts=scripted_tts,
        agent_calls=agent_calls,
        persist_calls=persist_calls,
        memory_calls=memory_calls,
        stream_marks=stream_marks,
    )


async def start_session(env, ws: FakeWebSocket) -> asyncio.Task:
    """Create a session on ws, run handle() as a task, do the handshake."""
    session = VoiceSession(env.conv_id, ws)
    task = asyncio.create_task(session.handle(ws))
    ws.client_json(
        {
            "type": "start",
            "conversation_id": str(env.conv_id),
            "sample_rate": 16000,
            "channels": 1,
            "format": "pcm_s16le",
        }
    )
    ok = await wait_until(lambda: "ready" in ws.frame_types(), timeout=5.0)
    assert ok, f"no ready frame; sent={ws.frame_types()}"
    return task


def feed_speech(ws: FakeWebSocket, messages: int = 4) -> None:
    for _ in range(messages):
        ws.client_binary(0x01, speech_pcm(100))


def feed_silence(ws: FakeWebSocket, messages: int = 2) -> None:
    for _ in range(messages):
        ws.client_binary(0x01, silence_pcm(100))


async def finish(task: asyncio.Task, ws: FakeWebSocket, timeout: float = 5.0) -> None:
    ws.client_disconnect()
    await asyncio.wait_for(task, timeout=timeout)


# ══════════════════════════════════════════════════════════════════════
# Full protocol
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_full_voice_turn_happy_path(env):
    ws = FakeWebSocket()
    task = await start_session(env, ws)

    # ready frame shape
    ready = ws.frames_of("ready")[0]
    assert ready["session_id"]
    assert ready["state"] == "LISTENING"
    assert ready["asr"]["provider"] == "scripted"
    assert ready["tts"]["provider"] == "scripted"
    # the start also emits the LISTENING state frame
    assert "LISTENING" in [f["state"] for f in ws.frames_of("state")]

    # 1. speak: speech → asr_partial(s)
    feed_speech(ws, messages=6)
    ok = await wait_until(lambda: ws.frames_of("asr_partial"))
    assert ok, f"no asr_partial; sent={ws.frame_types()}"
    partial = ws.frames_of("asr_partial")[0]
    assert partial["utterance_id"]
    assert partial["text"] == "hello wor"
    assert env.asr.start_count == 1

    # 2. silence → end of utterance → asr_final → user_message → turn
    feed_silence(ws, messages=2)
    ok = await wait_until(lambda: ws.frames_of("assistant_message"))
    assert ok, f"turn did not complete; sent={ws.frame_types()}"

    # ── frame sequence (subsequence must appear in this order) ──
    types = ws.frame_types()
    expected_order = [
        "ready",
        "asr_partial",
        "asr_final",
        "user_message",
        "agent_event",
        "tts_start",
        "tts_chunk",
        "tts_end",
        "assistant_message",
    ]
    pos = -1
    for ftype in expected_order:
        assert ftype in types, f"missing frame {ftype} in {types}"
        pos = types.index(ftype, pos + 1)
    # …and the turn ends with LISTENING (after assistant_message).
    states = [f["state"] for f in ws.frames_of("state")]
    assert states[-1] == "LISTENING"
    assert "PROCESSING" in states and "SPEAKING" in states
    assert (
        states.index("PROCESSING")
        < states.index("SPEAKING")
        < states.index("LISTENING", states.index("SPEAKING"))
    )

    # ── asr_final / user_message content ──
    final = ws.frames_of("asr_final")[0]
    assert final["text"] == "hello world"
    user_msg = ws.frames_of("user_message")[0]["message"]
    assert user_msg["role"] == "user"
    assert user_msg["content"] == "hello world"
    assert user_msg["modality"] == "voice"

    # ── persistence: user message with voice modality ──
    assert env.persist_calls[0]["role"] == "user"
    assert env.persist_calls[0]["content"] == "hello world"
    assert env.persist_calls[0]["conv_id"] == env.conv_id
    assert env.persist_calls[0]["modality"] == "voice"

    # ── agent integration: run_agent_stream CALLED, events forwarded
    #    verbatim as agent_event frames ──
    assert len(env.agent_calls) == 1
    call = env.agent_calls[0]
    assert call["conversation_id"] == str(env.conv_id)
    assert call["messages"][-1] == {"role": "user", "content": "hello world"}
    forwarded = [f["event"] for f in ws.frames_of("agent_event")]
    for ev in assistant_events():
        assert ev in forwarded, f"agent event not forwarded: {ev}"
    assert forwarded == assistant_events()

    # ── assistant message persisted + announced ──
    assistant_persist = [c for c in env.persist_calls if c["role"] == "assistant"]
    assert len(assistant_persist) == 1
    ap = assistant_persist[0]
    assert ap["conv_id"] == env.conv_id
    assert ap["modality"] == "voice"
    assert ap["model"] == "test-model"
    assert ap["generation_duration"] == 3
    assert any(b["type"] == "text" for b in ap["blocks"])
    assert any(b["type"] == "thinking" for b in ap["blocks"])
    am = ws.frames_of("assistant_message")[0]["message"]
    assert am["role"] == "assistant"
    assert am["modality"] == "voice"
    assert am["model"] == "test-model"
    assert am["generationDuration"] == 3
    assert ASSISTANT_TEXT.strip() == am["content"].strip()
    assert am["blocks"] == ap["blocks"]

    # ── TTS: announce immediately precedes its 0x03 binary frame ──
    tts_starts = ws.frames_of("tts_start")
    assert len(tts_starts) == 1
    gid = tts_starts[0]["generation_id"]
    assert tts_starts[0]["sample_rate"] == settings.VOICE_TTS_SAMPLE_RATE
    binaries = ws.binary_frames()
    assert len(binaries) >= 2  # two sentences × 3 chunks
    for i, (kind, payload) in enumerate(ws.sent):
        if kind == "bytes":
            assert payload[0] == 0x03
            prev = ws.sent[i - 1]
            assert prev[0] == "json" and prev[1]["type"] == "tts_chunk", (
                f"0x03 frame at index {i} not immediately preceded by a "
                f"tts_chunk announce: {prev}"
            )
            assert prev[1]["generation_id"] == gid
    for chunk in ws.frames_of("tts_chunk"):
        assert chunk["generation_id"] == gid
        assert chunk["seq"] >= 1
    for end in ws.frames_of("tts_end"):
        assert end["generation_id"] == gid
    # sequential seq numbers
    seqs = [c["seq"] for c in ws.frames_of("tts_chunk")]
    assert seqs == sorted(seqs)

    # TTS spoke the cleaned sentences (markdown-free)
    assert len(env.tts.synthesized) >= 1
    assert all("`" not in s and "*" not in s for s in env.tts.synthesized)

    # ── post-turn bookkeeping ──
    assert env.stream_marks[0] == f"active:{env.conv_id}"
    assert f"idle:{env.conv_id}" in env.stream_marks
    assert env.stream_marks.index(f"active:{env.conv_id}") < env.stream_marks.index(
        f"idle:{env.conv_id}"
    )
    assert len(env.memory_calls) == 1
    assert env.memory_calls[0]["messages"][-1]["content"] == "hello world"

    await finish(task, ws)


# ══════════════════════════════════════════════════════════════════════
# Barge-in
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_barge_in_cancels_tts_and_captures_new_utterance(env, monkeypatch):
    # A slow agent (long turn) + a slow, chunky TTS still streaming.
    monkeypatch.setattr(
        vs,
        "run_agent_stream",
        make_agent_stream(
            assistant_events()
            + [
                {"event": "message", "message": {"content": " trailing words "}}
                for _ in range(20)
            ],
            delay=0.02,
            record=env.agent_calls,
        ),
    )
    monkeypatch.setattr(env.tts, "chunks", 40)

    ws = FakeWebSocket()
    task = await start_session(env, ws)

    # First turn: speech → silence → agent turn starts.
    feed_speech(ws, messages=4)
    feed_silence(ws, messages=2)
    ok = await wait_until(
        lambda: "SPEAKING" in [f["state"] for f in ws.frames_of("state")]
    )
    assert ok, f"never reached SPEAKING; sent={ws.frame_types()}"
    gid = ws.frames_of("tts_start")[0]["generation_id"]

    # Barge-in: speak loudly over the TTS (server VAD path).
    binary_before = len(ws.binary_frames())
    assert binary_before >= 1
    feed_speech(ws, messages=3)

    ok = await wait_until(lambda: ws.frames_of("interrupted"))
    assert ok, f"no interrupted frame; sent={ws.frame_types()}"
    interrupted = ws.frames_of("interrupted")[0]
    assert interrupted["generation_id"] == gid

    # TTS generation cancelled: engine cancel called + no further 0x03
    # frames AFTER the interrupted frame (stale-generation audio dropped).
    assert env.tts.cancel_count >= 1
    interrupted_idx = next(
        i
        for i, (kind, d) in enumerate(ws.sent)
        if kind == "json" and d.get("type") == "interrupted"
    )
    await asyncio.sleep(0.25)  # give any uncancelled worker time to misbehave
    stale = [d for kind, d in ws.sent[interrupted_idx + 1 :] if kind == "bytes"]
    assert stale == [], f"stale 0x03 frames after interruption: {stale}"
    # No tts_* frames for the OLD generation after the interruption either.
    stale_announce = [d for d in ws.frames_of("tts_chunk") if d["generation_id"] == gid]
    for d in stale_announce:
        idx = ws.sent.index(("json", d))
        assert idx < interrupted_idx

    # The agent stream was cancelled mid-flight.
    assert any("cancelled" in c for c in env.agent_calls)

    # State returned to LISTENING (via INTERRUPTING).
    states = [f["state"] for f in ws.frames_of("state")]
    assert "INTERRUPTING" in states
    assert states[-1] == "LISTENING"

    # Partial assistant content persisted — only when meaningful (≥1 block
    # of text existed), marked as interrupted.
    assistant_persist = [c for c in env.persist_calls if c["role"] == "assistant"]
    assert len(assistant_persist) == 1
    assert any(
        b.get("type") == "error" and "interrupted" in b.get("content", "").lower()
        for b in assistant_persist[0]["blocks"]
    )

    # The user's NEW utterance is already being captured — the pre-roll
    # (250 ms = 8000 bytes) was seeded into a fresh ASR stream.
    assert env.asr.start_count == 2
    second_stream = env.asr.streams[1]
    assert (
        len(second_stream) >= 8000
    ), f"new utterance missing pre-roll samples: {len(second_stream)} bytes"
    # The pre-roll ends with the speech that triggered the barge-in.
    assert bytes(second_stream[-1600:]) != b"\x00" * 1600

    await finish(task, ws)


@pytest.mark.asyncio
async def test_client_interrupt_frame(env):
    ws = FakeWebSocket()
    task = await start_session(env, ws)
    feed_speech(ws, 4)
    feed_silence(ws, 2)
    ok = await wait_until(
        lambda: "SPEAKING" in [f["state"] for f in ws.frames_of("state")]
    )
    assert ok
    gid = ws.frames_of("tts_start")[0]["generation_id"]

    ws.client_json({"type": "interrupt"})
    ok = await wait_until(lambda: ws.frames_of("interrupted"))
    assert ok
    assert ws.frames_of("interrupted")[0]["generation_id"] == gid
    states = [f["state"] for f in ws.frames_of("state")]
    assert states[-1] == "LISTENING"
    await finish(task, ws)


# ══════════════════════════════════════════════════════════════════════
# Frame validation + handshake failures
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mic_frame_rejected_before_start(env):
    ws = FakeWebSocket()
    session = VoiceSession(env.conv_id, ws)
    task = asyncio.create_task(session.handle(ws))
    await asyncio.sleep(0.05)
    ws.client_binary(0x01, speech_pcm(100))
    ok = await wait_until(lambda: ws.frames_of("error"))
    assert ok
    assert ws.frames_of("error")[0]["code"] == "not_started"
    assert ws.frames_of("error")[0]["fatal"] is False
    await finish(task, ws)


@pytest.mark.asyncio
async def test_oversize_and_bad_prefix_frames(env):
    ws = FakeWebSocket()
    task = await start_session(env, ws)
    await asyncio.sleep(0.02)

    ws.client_binary(0x01, b"\x00" * 5000)  # > 4096 payload
    ok = await wait_until(
        lambda: any(e["code"] == "frame_too_large" for e in ws.frames_of("error"))
    )
    assert ok

    ws.client_binary(0x07, b"\x00" * 100)  # bad prefix
    ok = await wait_until(
        lambda: any(e["code"] == "bad_binary_frame" for e in ws.frames_of("error"))
    )
    assert ok

    # ping → pong
    ws.client_json({"type": "ping"})
    ok = await wait_until(lambda: "pong" in ws.frame_types())
    assert ok

    # the session is still alive (start handshake succeeded earlier and no
    # fatal error closed it)
    assert not ws.closed
    await finish(task, ws)


@pytest.mark.asyncio
async def test_not_ready_rejects_start(env, monkeypatch):
    monkeypatch.setattr(models_store, "asr_ready", lambda: False)
    ws = FakeWebSocket()
    session = VoiceSession(env.conv_id, ws)
    task = asyncio.create_task(session.handle(ws))
    ws.client_json(
        {
            "type": "start",
            "conversation_id": str(env.conv_id),
            "sample_rate": 16000,
            "channels": 1,
            "format": "pcm_s16le",
        }
    )
    ok = await wait_until(lambda: ws.frames_of("error"))
    assert ok
    err = ws.frames_of("error")[0]
    assert err["code"] == "voice_not_ready"
    assert err["fatal"] is True
    ok = await wait_until(lambda: ws.closed)
    assert ok
    states = [f["state"] for f in ws.frames_of("state")]
    assert "ERROR" in states and "STOPPING" in states
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_invalid_conversation_id_rejected(env):
    ws = FakeWebSocket()
    session = VoiceSession(env.conv_id, ws)
    task = asyncio.create_task(session.handle(ws))
    ws.client_json(
        {
            "type": "start",
            "conversation_id": "not-a-uuid",
            "sample_rate": 16000,
        }
    )
    ok = await wait_until(lambda: ws.frames_of("error"))
    assert ok
    assert ws.frames_of("error")[0]["code"] == "invalid_conversation_id"
    assert ws.frames_of("error")[0]["fatal"] is True
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_disabled_voice_rejects_start(env, monkeypatch):
    monkeypatch.setattr(settings, "VOICE_ENABLED", False)
    ws = FakeWebSocket()
    session = VoiceSession(env.conv_id, ws)
    task = asyncio.create_task(session.handle(ws))
    ws.client_json({"type": "start", "conversation_id": str(env.conv_id)})
    ok = await wait_until(lambda: ws.frames_of("error"))
    assert ok
    assert ws.frames_of("error")[0]["code"] == "voice_disabled"
    assert ws.frames_of("error")[0]["fatal"] is True
    await asyncio.wait_for(task, timeout=5)


# ══════════════════════════════════════════════════════════════════════
# Stop / idle timeout / disconnect cleanup
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stop_sends_stopped_and_closes(env):
    ws = FakeWebSocket()
    task = await start_session(env, ws)
    ws.client_json({"type": "stop"})
    ok = await wait_until(lambda: "stopped" in ws.frame_types() and ws.closed)
    assert ok
    states = [f["state"] for f in ws.frames_of("state")]
    assert states[-1] == "STOPPING"
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_idle_timeout_closes_session(env, monkeypatch):
    monkeypatch.setattr(settings, "VOICE_SESSION_IDLE_SEC", 1)
    ws = FakeWebSocket()
    task = await start_session(env, ws)
    # No frames at all → the watchdog must fire after ~1s.
    ok = await wait_until(
        lambda: "stopped" in ws.frame_types() and ws.closed, timeout=6.0
    )
    assert ok
    await asyncio.wait_for(task, timeout=5)


@pytest.mark.asyncio
async def test_disconnect_cancels_tasks(env, monkeypatch):
    monkeypatch.setattr(
        vs,
        "run_agent_stream",
        make_agent_stream(assistant_events(), delay=0.05, record=env.agent_calls),
    )
    ws = FakeWebSocket()
    task = await start_session(env, ws)
    feed_speech(ws, 4)
    feed_silence(ws, 2)
    ok = await wait_until(lambda: env.agent_calls)
    assert ok  # turn is in flight
    ws.client_disconnect()
    await asyncio.wait_for(task, timeout=5)
    # Cleanup: engines cancelled + stream marker released.
    await asyncio.sleep(0.05)
    assert f"idle:{env.conv_id}" in env.stream_marks
    # No further persistence after the disconnect.
    n_persist = len(env.persist_calls)
    await asyncio.sleep(0.1)
    assert len(env.persist_calls) == n_persist


# ══════════════════════════════════════════════════════════════════════
# Manager
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_manager_one_session_per_conversation():
    manager = VoiceSessionManager()
    conv = str(uuid_mod.uuid4())
    ws1, ws2 = FakeWebSocket(), FakeWebSocket()

    s1 = await manager.get_or_create(conv, ws1)
    assert s1 is not None
    assert manager.active_count == 1
    assert manager.get(conv) is s1

    # Second connection for the SAME conversation → rejected.
    s2 = await manager.get_or_create(conv, ws2)
    assert s2 is None
    errs = ws2.json_frames()
    assert errs and errs[0]["type"] == "error"
    assert errs[0]["code"] == "session_exists"
    assert errs[0]["fatal"] is True
    assert ws2.closed
    assert manager.active_count == 1

    # Removal allows a new session.
    manager.remove(conv)
    assert manager.active_count == 0
    assert manager.get(conv) is None
    s3 = await manager.get_or_create(conv, FakeWebSocket())
    assert s3 is not None
    manager.remove(conv)


@pytest.mark.asyncio
async def test_manager_rejects_invalid_conversation_id():
    manager = VoiceSessionManager()
    ws = FakeWebSocket()
    session = await manager.get_or_create("not-a-uuid", ws)
    assert session is None
    errs = ws.json_frames()
    assert errs[0]["code"] == "invalid_conversation_id"
    assert ws.closed
