"""Protocol-v1 WebSocket voice session — the real-time pipeline core.

One :class:`VoiceSession` per WebSocket connection. The frame contract is
EXACTLY the one implemented by the frontend client
(``frontend/src/voice/VoiceSessionClient.ts``) — see
``app/voice/__init__.py`` for the binary frame prefixes.

Per mic frame (0x01, s16le 16 kHz mono, ~100 ms):

    mic PCM ──▶ 10 ms AEC frames (320 B)  ──▶ AEC.process (far-end 0x02
                frames are pushed into AEC.push_reference as they arrive)
             ──▶ RollingBuffer.push       (pre-roll ALWAYS — even while
                                           the assistant is SPEAKING)
             ──▶ UtteranceTracker.feed_pcm (30 ms VAD frames internally)
             ──▶ utterance audio → streaming ASR → asr_partial/asr_final

End of utterance (VAD silence ≥ VOICE_VAD_SILENCE_MS, or
VOICE_MAX_UTTERANCE_SEC) → persist the user message (voice modality, the
SAME conversation) → agent turn via the EXISTING ``run_agent_stream``
(forwarded verbatim as ``agent_event`` frames) → sentence-chunked
streaming TTS (``tts_chunk`` announce immediately preceding each 0x03
binary frame) → persist the assistant message (BlockBuilder, same blocks
structure as text chat) → LISTENING.

Barge-in: server VAD speech onset while PROCESSING/SPEAKING (or a client
``interrupt`` frame) → INTERRUPTING → cancel agent + TTS → emit
``interrupted{generation_id}`` → persist partial content → LISTENING with
the new utterance already captured (the pre-roll retained its first
syllables).

Voice is a modality of the EXISTING assistant: no agent logic is
duplicated here — ``run_agent_stream``, ``persist_message_standalone``,
``BlockBuilder`` and ``maybe_run_memory_extraction`` are the exact
services the text-chat path uses.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid as uuid_mod
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import WebSocket, WebSocketDisconnect

from app.agent.service import run_agent_stream
from app.services import model_prefs, voice_settings
from app.config import settings
from app.services.background_queue import mark_stream_active, mark_stream_idle
from app.services.blocks import BlockBuilder
from app.services.conversations import persist_message_standalone
from app.services.memory_extractor import maybe_run_memory_extraction
from app.voice import FRAME_FAR, FRAME_MIC, FRAME_TTS, models_store
from app.voice.aec import create_aec
from app.voice.asr import AsrError, AsrProvider, create_asr_engine
from app.voice.audio import RollingBuffer, split_even_frames
from app.voice.state import (
    INTERRUPTIBLE_STATES,
    InvalidTransition,
    VoiceState,
    VoiceStateMachine,
)
from app.voice.tts import (
    SentenceChunker,
    TtsError,
    TtsProvider,
    clean_for_tts,
    create_tts_engine,
)
from app.voice.vad import UtteranceTracker, create_vad

logger = logging.getLogger(__name__)

# 10 ms of s16le @16 kHz — the AEC frame size (webrtcvad wants 30 ms; the
# UtteranceTracker frames that itself via feed_pcm).
MIC_FRAME_BYTES = 320

# Binary frame payload cap (protocol v1) — oversize frames are rejected.
MAX_BINARY_PAYLOAD = 4096

# Session-level asr_partial throttle (the ASR engine throttles inference
# itself; this bounds frame frequency). Tests pin this to 0.
ASR_PARTIAL_INTERVAL_S = 0.7

# Upper bound for a single final ASR transcription.
ASR_FINAL_TIMEOUT_S = 30.0

# Drop mic frames briefly after final ASR returns. Those frames were captured
# while inference blocked this session's receive loop and are stale tail/noise,
# not a new user utterance. Without this quarantine they can form a 90 ms VAD
# onset and Qwen commonly hallucinates a one-word turn such as "The.".
POST_FINAL_MIC_QUARANTINE_S = 0.75

# How long the TTS worker waits on the sentence queue before re-checking
# the chunker flush timeout.
TTS_QUEUE_POLL_S = 0.2

# Recent conversation history loaded for each agent turn (bounded).
VOICE_HISTORY_LIMIT = 50

# Codes for frame validation errors (all non-fatal).
_ERR_BAD_PREFIX = "bad_binary_frame"
_ERR_FRAME_TOO_LARGE = "frame_too_large"
_ERR_NOT_STARTED = "not_started"


def _parse_sse(chunk: str) -> Optional[dict]:
    """Parse one agent SSE string (``data: {json}\\n\\n``) → event dict."""
    if not chunk:
        return None
    data = chunk.strip()
    if not data.startswith("data:"):
        return None
    data = data[5:].strip()
    if not data or data == "[DONE]":
        return None
    try:
        parsed = json.loads(data)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


async def _resolve_model_label() -> str:
    """Model label for persistence — resolved exactly like chat.py
    (model_prefs.resolve_chat_request_model('default')), with a safe
    fallback when the model registry is unreachable."""
    try:
        from app.services import model_prefs

        return await model_prefs.resolve_chat_request_model("default")
    except Exception:
        return "default"


async def _suppress(task: "asyncio.Task") -> None:
    """Await a task, swallowing cancellation and already-reported errors."""
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:  # noqa: BLE001 — task errors are logged elsewhere
        pass


def _apply_event_to_builder(parsed: dict, builder: BlockBuilder) -> None:
    """Feed one agent SSE event into the BlockBuilder.

    Identical to the consumption pattern in chat.py ``generate()`` — the
    blocks persisted for voice turns match text-chat turns exactly.
    """
    event_type = parsed.get("event")

    if event_type == "thinking_start":
        builder.on_thinking_start()

    if event_type == "thinking" and parsed.get("thinking"):
        builder.on_thinking_token(parsed["thinking"])

    if event_type == "thinking_done" and parsed.get("thinkingDuration") is not None:
        builder.on_thinking_done(parsed["thinkingDuration"])

    if event_type == "generation_done" and parsed.get("generationDuration") is not None:
        builder.on_generation_done(parsed["generationDuration"])

    if event_type == "message" and (parsed.get("message") or {}).get("content"):
        builder.on_message_token(parsed["message"]["content"])

    if event_type == "tool_call" and parsed.get("tool_call"):
        tc = parsed["tool_call"]
        if tc.get("status") == "running":
            builder.on_tool_call_start(tc)
        else:
            updates = {k: v for k, v in tc.items() if k != "id"}
            builder.on_tool_call_update(tc.get("id", ""), updates)

    if event_type == "rag_sources" and parsed.get("sources"):
        builder.on_rag_sources(parsed.get("tool_call_id", ""), parsed["sources"])

    if event_type == "error" and parsed.get("error"):
        builder.on_error(str(parsed["error"]))


class VoiceSession:
    """One protocol-v1 voice WebSocket session (one conversation)."""

    def __init__(self, conversation_id: uuid_mod.UUID, ws: Optional[WebSocket] = None):
        self.conversation_id = conversation_id
        self.session_id = uuid_mod.uuid4().hex
        self._ws = ws

        self.state_machine = VoiceStateMachine()

        # Pipeline components — created on `start` (after validation).
        self.started = False
        self.aec: Optional[Any] = None
        self.vad: Optional[Any] = None
        self.tracker: Optional[UtteranceTracker] = None
        self.preroll: Optional[RollingBuffer] = None
        self.chunker: Optional[SentenceChunker] = None
        self.asr: Optional[AsrProvider] = None
        self.tts: Optional[TtsProvider] = None

        # Utterance state (ASR side).
        self._utterance_active = False
        self._utterance_id: Optional[str] = None
        self._mic_leftover = b""  # sub-10ms mic tail carried between frames
        self._last_partial_emit = 0.0
        self._last_partial_text = ""
        self._ignore_mic_until = 0.0

        # Agent-turn state.
        self.current_agent_task: Optional[asyncio.Task] = None
        self._tts_task: Optional[asyncio.Task] = None
        self._tts_queue: Optional[asyncio.Queue] = None
        self._agent_builder: Optional[BlockBuilder] = None
        self.generation_id: Optional[str] = None
        self._tts_interrupted = False
        self._model_label = "default"
        self._current_transcript = ""
        self._history_messages: List[Dict[str, str]] = []
        self._stream_marked = False
        self._client_playback_ack = False
        self._playback_done = asyncio.Event()

        # Housekeeping.
        self._send_lock = asyncio.Lock()
        self._last_activity = time.monotonic()
        self._closed = False
        self._cleaned = False
        self._tasks: List[asyncio.Task] = []

    # ── properties ───────────────────────────────────────────────────

    @property
    def closed(self) -> bool:
        """True once the session is shut down (manager dedupe check)."""
        return self._closed or self._cleaned

    @property
    def state(self) -> VoiceState:
        return self.state_machine.state

    # ── main loop ────────────────────────────────────────────────────

    async def handle(self, ws: WebSocket) -> None:
        """Receive/dispatch loop for the lifetime of the connection."""
        self._ws = ws
        self._last_activity = time.monotonic()
        watchdog = asyncio.create_task(self._idle_watchdog())
        self._tasks.append(watchdog)
        try:
            async for message in self._iter_messages():
                if self._closed:
                    break
                if isinstance(message, str):
                    await self._handle_text(message)
                elif isinstance(message, (bytes, bytearray, memoryview)):
                    await self._handle_binary(bytes(message))
        except WebSocketDisconnect as e:
            logger.info(
                "voice session %s: client disconnected (code=%s)",
                self.session_id,
                e.code,
            )
        except Exception as e:  # noqa: BLE001 — never leak a raw crash
            logger.exception("voice session %s: handler error", self.session_id)
            try:
                await self._send_error("session_error", str(e), fatal=True)
            except Exception:
                pass
        finally:
            await self._cleanup()

    async def _iter_messages(self) -> AsyncIterator[Any]:
        """Yield inbound text/bytes; raise WebSocketDisconnect at the end.

        Starlette's WebSocket has no ``__aiter__`` — receive() returns raw
        ASGI messages, which this adapter normalizes.
        """
        while not self._closed:
            msg = await self._ws.receive()
            if not isinstance(msg, dict):
                continue
            mtype = msg.get("type")
            if mtype == "websocket.disconnect":
                raise WebSocketDisconnect(code=int(msg.get("code") or 1000))
            if mtype == "websocket.receive":
                text = msg.get("text")
                if isinstance(text, str):
                    yield text
                    continue
                data = msg.get("bytes")
                if data is not None:
                    yield bytes(data)

    # ── client → server text frames ──────────────────────────────────

    async def _handle_text(self, raw: str) -> None:
        self._touch()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug("voice: ignoring malformed JSON frame")
            return
        if not isinstance(payload, dict):
            return
        mtype = payload.get("type")
        if mtype == "start":
            await self._handle_start(payload)
        elif mtype == "stop":
            await self._handle_stop()
        elif mtype == "interrupt":
            await self._handle_barge_in()
        elif mtype == "playback_end":
            if str(payload.get("generation_id") or "") == str(self.generation_id or ""):
                self._playback_done.set()
        elif mtype == "ping":
            await self._send_json({"type": "pong"})
        else:
            logger.debug("voice: ignoring unknown frame type %r", mtype)

    async def _handle_start(self, payload: dict) -> None:
        """Handshake: validate, build the pipeline, reply `ready`."""
        if self.started:
            return  # duplicate start is idempotent
        if not settings.VOICE_ENABLED:
            await self._fatal_close(
                "voice_disabled",
                "Voice chat is disabled on this server (VOICE_ENABLED=false).",
            )
            return
        # conversation_id must be a valid UUID matching this socket.
        conv_raw = payload.get("conversation_id")
        try:
            conv_uuid = uuid_mod.UUID(str(conv_raw))
        except (ValueError, TypeError, AttributeError):
            await self._fatal_close(
                "invalid_conversation_id",
                f"start.conversation_id is not a valid UUID: {conv_raw!r}",
            )
            return
        if conv_uuid != self.conversation_id:
            await self._fatal_close(
                "conversation_mismatch",
                "start.conversation_id does not match this connection.",
            )
            return
        # Protocol v1 fixes the input format.
        declared_rate = payload.get("sample_rate") or settings.VOICE_SAMPLE_RATE
        try:
            if int(declared_rate) != settings.VOICE_SAMPLE_RATE:
                await self._fatal_close(
                    "unsupported_sample_rate",
                    f"voice protocol v1 requires {settings.VOICE_SAMPLE_RATE} Hz "
                    f"input (got {declared_rate}).",
                )
                return
        except (TypeError, ValueError):
            pass
        if payload.get("channels") not in (None, 1):
            logger.warning(
                "voice: client declared channels=%r (protocol v1 is mono)",
                payload.get("channels"),
            )
        if payload.get("format") not in (None, "pcm_s16le"):
            logger.warning(
                "voice: client declared format=%r (protocol v1 is pcm_s16le)",
                payload.get("format"),
            )
        self._client_playback_ack = payload.get("playback_ack") is True

        # Voice model selection — profiles.yml only (settings resolver).
        config = settings.get_voice_config()
        if not config.configured:
            await self._fatal_close(
                "voice_not_configured",
                "Voice models are not configured (profiles.yml `voice:` section).",
            )
            return
        # One canonical readiness gate: persisted packages + HF cache for both
        # models. This deliberately rejects legacy custom-download manifests.
        dependency_status = models_store.voice_dependency_status()
        use_host_runtime = bool(settings.VOICE_RUNTIME_URL)
        if not use_host_runtime and not dependency_status["asr"]["valid"]:
            await self._fatal_close(
                "voice_not_ready",
                "Voice ASR model is not installed or does not match the "
                "configured selection — run the setup wizard.",
            )
            return
        if not use_host_runtime and not dependency_status["tts"]["valid"]:
            await self._fatal_close(
                "voice_not_ready",
                "Voice TTS model is not installed or does not match the "
                "configured selection — run the setup wizard.",
            )
            return
        if not use_host_runtime and dependency_status.get("runtime"):
            missing = ", ".join(
                str(item.get("description") or item.get("id"))
                for item in dependency_status["runtime"]
            )
            await self._fatal_close(
                "voice_not_ready",
                f"Voice runtime packages are missing or outdated: {missing}. "
                "Run the setup wizard.",
            )
            return

        try:
            self.asr = create_asr_engine(config.asr)
            self.tts = create_tts_engine(config.tts)
        except (AsrError, TtsError) as e:
            await self._fatal_close(e.code, e.message)
            return
        self.aec = create_aec(settings.VOICE_AEC)
        self.vad = create_vad()
        self.tracker = UtteranceTracker(
            self.vad,
            silence_ms=settings.VOICE_VAD_SILENCE_MS,
            max_utterance_ms=settings.VOICE_MAX_UTTERANCE_SEC * 1000,
        )
        self.preroll = RollingBuffer(settings.VOICE_PREROLL_MS)
        self.chunker = SentenceChunker()
        self.started = True

        await self._send_json(
            {
                "type": "ready",
                "session_id": self.session_id,
                "state": VoiceState.LISTENING.value,
                "asr": self.asr.status(),
                "tts": self.tts.status(),
            }
        )
        await self._apply_state(VoiceState.LISTENING, "start")

        # Eager background warm-up: load the ASR/TTS models NOW (mic-click
        # time) instead of on the first turn. Loads are disk-bound (the
        # setup wizard installed the assets), bounded by the TTS load
        # timeout, and failures surface as error frames immediately —
        # never as a mid-turn hang. The client is already usable (ready
        # was sent); the first utterance simply benefits from a warm cache.
        if settings.VOICE_EAGER_WARMUP:
            warm_task = asyncio.create_task(self._warm_up_engines())
            self._tasks.append(warm_task)

    async def _warm_up_engines(self) -> None:
        """Background model preloading (session start). Best-effort —
        recoverable failures emit an error frame but keep the session.
        Duck-typed: engines without a warm_up are skipped (the base
        AsrProvider default is a no-op; test doubles may omit it)."""
        # ASR first (the user will speak before TTS is needed).
        if self.asr is not None:
            warm = getattr(self.asr, "warm_up", None)
            if callable(warm):
                try:
                    await warm()
                except asyncio.CancelledError:
                    raise
                except AsrError as e:
                    await self._send_error(e.code, e.message, fatal=e.fatal)
                except Exception as e:  # noqa: BLE001 — surfaced to the client
                    await self._send_error("asr_model_error", str(e), fatal=False)
        if self.tts is not None:
            # On unified-memory hosts, loading Torch/Pocket TTS alongside MLX
            # before the user speaks makes ASR several times slower. Remote
            # TTS loads lazily while the agent begins generating, hiding most
            # of its one-time cost without penalizing speech recognition.
            if settings.VOICE_RUNTIME_URL:
                return
            warm = getattr(self.tts, "warm_up", None)
            if callable(warm):
                try:
                    await warm()
                except asyncio.CancelledError:
                    raise
                except TtsError as e:
                    await self._send_error(e.code, e.message, fatal=e.fatal)
                except Exception as e:  # noqa: BLE001 — surfaced to the client
                    await self._send_error("tts_model_error", str(e), fatal=False)

    async def _handle_stop(self) -> None:
        """User toggled voice off — wind down and close."""
        self._utterance_active = False
        if self.tracker is not None:
            self.tracker.force_end()
        await self._shutdown(send_stopped=True, reason="stop", close=True)

    # ── client → server binary frames ────────────────────────────────

    async def _handle_binary(self, data: bytes) -> None:
        self._touch()
        if len(data) < 1:
            return
        prefix = data[0]
        payload = data[1:]
        if prefix not in (FRAME_MIC, FRAME_FAR):
            await self._send_error(
                _ERR_BAD_PREFIX,
                f"unknown binary frame prefix 0x{prefix:02x} "
                f"(expected 0x01 mic / 0x02 far-end)",
                fatal=False,
            )
            return
        if len(payload) > MAX_BINARY_PAYLOAD:
            await self._send_error(
                _ERR_FRAME_TOO_LARGE,
                f"binary frame payload {len(payload)} bytes exceeds the "
                f"{MAX_BINARY_PAYLOAD}-byte protocol limit",
                fatal=False,
            )
            return
        if prefix == FRAME_FAR:
            # Exact played audio — the AEC cancellation reference. Pushed
            # into the AEC as the frames arrive (client-echoed 0x03→16k).
            if self.aec is not None:
                self.aec.push_reference(payload)
            return
        # FRAME_MIC
        if not self.started:
            await self._send_error(
                _ERR_NOT_STARTED,
                "mic frames received before the start handshake completed",
                fatal=False,
            )
            return
        await self._process_mic_pcm(payload)

    async def _process_mic_pcm(self, pcm: bytes) -> None:
        """The per-mic-frame pipeline (see module docstring)."""
        if self.aec is None or self.preroll is None or self.tracker is None:
            return
        if (
            time.monotonic() < self._ignore_mic_until
            and self.state_machine.state != VoiceState.SPEAKING
        ):
            # Clear rather than retain stale sub-frame data or pre-roll.
            self._mic_leftover = b""
            return
        data = self._mic_leftover + pcm
        frames, self._mic_leftover = split_even_frames(data, MIC_FRAME_BYTES)
        if not frames:
            return
        cleaned_chunks: List[bytes] = []
        for frame in frames:
            # AEC (the mic is NEVER dropped — see app/voice/aec.py).
            cleaned = self.aec.process(frame)
            # Pre-roll ALWAYS — including while the assistant speaks, so
            # barge-in keeps its first syllables.
            self.preroll.push(cleaned)
            cleaned_chunks.append(cleaned)
        cleaned_all = b"".join(cleaned_chunks)

        # Frames that arrived after the utterance started are appended to
        # the utterance + fed to ASR (the frames before the onset are
        # already inside the pre-roll snapshot that seeded the utterance).
        if self._utterance_active:
            try:
                await self.asr.feed_audio(cleaned_all)
            except AsrError as e:
                await self._send_error(e.code, e.message, fatal=e.fatal)
                if e.fatal:
                    await self._fatal_close(e.code, e.message)
                    return

        events = self.tracker.feed_pcm(cleaned_all)
        for event in events:
            if event == "start":
                await self._on_utterance_start()
            elif event in ("end", "max_length"):
                await self._on_utterance_end(reason=event)
            if self._closed:
                return

        # Give VAD end/max-length priority over an expensive full-buffer
        # partial decode. In particular, a trailing-silence chunk must
        # finalize the utterance immediately instead of waiting behind a
        # partial transcription of audio that is already complete.
        if self._utterance_active:
            await self._maybe_emit_partial()

    # ── utterance lifecycle ──────────────────────────────────────────

    async def _on_utterance_start(self) -> None:
        """VAD confirmed speech onset."""
        if self.state_machine.state == VoiceState.SPEAKING:
            # Speaker echo is not a trustworthy barge-in signal. The browser
            # has hardware AEC plus its own VAD and sends an explicit
            # ``interrupt`` for real user speech. Keep the pre-roll so those
            # first syllables seed the utterance immediately after interrupt.
            self.tracker.reset()
            self._utterance_active = False
            return
        # Speech onset while the assistant is busy = barge-in (the
        # interrupt cancels the turn first; the new utterance — already
        # captured in the pre-roll — is seeded right after).
        if self.state_machine.in_state(*INTERRUPTIBLE_STATES):
            await self._handle_barge_in()
        if self._closed:
            return
        self._utterance_active = True
        self._utterance_id = uuid_mod.uuid4().hex
        preroll = self.preroll.snapshot()
        self._last_partial_text = ""
        self._last_partial_emit = time.monotonic()
        try:
            await self.asr.start_stream()
            if preroll:
                await self.asr.feed_audio(preroll)
        except AsrError as e:
            self._utterance_active = False
            self._utterance_id = None
            await self._send_error(e.code, e.message, fatal=e.fatal)
            if e.fatal:
                await self._fatal_close(e.code, e.message)
            return

    async def _maybe_emit_partial(self) -> None:
        """Throttled asr_partial emission (state stays LISTENING)."""
        now = time.monotonic()
        if now - self._last_partial_emit < ASR_PARTIAL_INTERVAL_S:
            return
        self._last_partial_emit = now
        try:
            text = (await self.asr.get_partial() or "").strip()
        except AsrError as e:
            await self._send_error(e.code, e.message, fatal=e.fatal)
            if e.fatal:
                await self._fatal_close(e.code, e.message)
            return
        if text and text != self._last_partial_text:
            self._last_partial_text = text
            await self._send_json(
                {
                    "type": "asr_partial",
                    "utterance_id": self._utterance_id,
                    "text": text,
                }
            )

    async def _on_utterance_end(self, reason: str) -> None:
        """VAD silence / max length → final transcript → agent turn."""
        self._utterance_active = False
        utterance_id = self._utterance_id
        self._utterance_id = None
        transcript = ""
        try:
            transcript = (
                await asyncio.wait_for(
                    self.asr.finish_stream(), timeout=ASR_FINAL_TIMEOUT_S
                )
                or ""
            ).strip()
        except asyncio.TimeoutError:
            await self._send_error(
                "asr_timeout", "ASR final transcription timed out", fatal=False
            )
        except AsrError as e:
            await self._send_error(e.code, e.message, fatal=e.fatal)
            if e.fatal:
                await self._fatal_close(e.code, e.message)
                return
        # ASR is a blocking turn boundary for this receive loop. Reset all
        # capture state and quarantine the frames that accumulated behind it;
        # otherwise the old tail can immediately seed a phantom utterance.
        self.tracker.reset()
        self.preroll.clear()
        self._mic_leftover = b""
        self._ignore_mic_until = time.monotonic() + POST_FINAL_MIC_QUARANTINE_S
        if not transcript:
            # Nothing intelligible was said — stay LISTENING.
            return
        await self._send_json(
            {"type": "asr_final", "utterance_id": utterance_id, "text": transcript}
        )
        # Persist the user message in the SAME conversation (text turns
        # may precede/follow) with the voice modality marker.
        msg_id: Optional[uuid_mod.UUID] = None
        try:
            msg_id = await persist_message_standalone(
                self.conversation_id, "user", transcript, modality="voice"
            )
        except Exception as e:  # noqa: BLE001 — DB errors are non-fatal
            logger.error("voice: user message persist failed: %s", e)
        if msg_id is None:
            await self._send_error(
                "persist_error", "failed to persist the user message", fatal=False
            )
        else:
            await self._send_json(
                {
                    "type": "user_message",
                    "message": {
                        "id": str(msg_id),
                        "role": "user",
                        "content": transcript,
                        "modality": "voice",
                    },
                }
            )
        # Start the agent turn (LISTENING → PROCESSING).
        if not await self._apply_state(VoiceState.PROCESSING, "asr_final"):
            return  # a barge-in raced us — drop this turn
        await self._start_agent_turn(transcript)

    # ── agent turn ───────────────────────────────────────────────────

    async def _start_agent_turn(self, transcript: str) -> None:
        """Spawn the agent-turn task (one at a time — barge-in must cancel
        the previous one before a new turn can start)."""
        if self.current_agent_task is not None and not self.current_agent_task.done():
            logger.warning("voice: agent turn already active — ignoring new turn")
            return
        self.generation_id = uuid_mod.uuid4().hex
        self._current_transcript = transcript
        self._history_messages = []
        self._model_label = await _resolve_model_label()
        if self.chunker is not None:
            self.chunker.reset()
        task = asyncio.create_task(self._agent_turn_worker())
        self.current_agent_task = task
        self._tasks.append(task)

    async def _load_history(self) -> List[Dict[str, str]]:
        """Recent messages of this conversation from the DB.

        The voice transcript joins the SAME conversation as text chat —
        text turns may precede/follow voice turns.
        """
        from app.db.session import async_session_factory
        from app.services import conversations as conv_service

        messages: List[Dict[str, str]] = []
        try:
            async with async_session_factory() as db:
                rows = await conv_service.get_messages(
                    db, self.conversation_id, limit=VOICE_HISTORY_LIMIT
                )
                for m in rows:
                    role = m.role if m.role in ("user", "assistant") else None
                    content = (m.content or "").strip()
                    if not role or not content:
                        continue
                    messages.append({"role": role, "content": content})
        except Exception as e:  # noqa: BLE001 — fall back to transcript-only
            logger.warning("voice: history load failed (%s) — transcript only", e)
            messages = []
        # Guarantee the current transcript is the last user message even
        # when the persist above failed or history is unavailable.
        if messages and messages[-1]["role"] == "user":
            messages[-1]["content"] = self._current_transcript
        else:
            messages.append({"role": "user", "content": self._current_transcript})
        self._history_messages = messages
        return messages

    async def _agent_turn_worker(self) -> None:
        """Consume run_agent_stream: forward events, feed the TTS queue,
        persist, and hand the turn back to LISTENING."""
        builder = BlockBuilder()
        self._agent_builder = builder
        conv_id_str = str(self.conversation_id)
        tts_task: Optional[asyncio.Task] = None
        try:
            # KV-cache protection — the same markers the text stream uses.
            await mark_stream_active(conv_id_str)
            self._stream_marked = True
            messages = await self._load_history()
            self._tts_queue = asyncio.Queue()
            tts_task = asyncio.create_task(self._tts_worker())
            self._tts_task = tts_task
            self._tasks.append(tts_task)

            async for sse in run_agent_stream(
                messages=messages,
                model=await model_prefs.resolve_task_model("voice"),
                images=None,
                conversation_id=conv_id_str,
                # Same default agent/tool loop, with spoken-response behavior
                # and no hidden reasoning delay. Length is prompt-adaptive,
                # not hard-truncated: detailed questions can stay detailed.
                think=False,
                interaction_mode="voice",
                interaction_instructions=voice_settings.persona_prompt(),
            ):
                parsed = _parse_sse(sse)
                if parsed is None:
                    continue
                # Forward the agent event VERBATIM (identical dicts to the
                # text-chat SSE stream — no duplicated agent logic).
                await self._send_json({"type": "agent_event", "event": parsed})
                _apply_event_to_builder(parsed, builder)
                if parsed.get("event") == "message":
                    token = (parsed.get("message") or {}).get("content") or ""
                    if token:
                        assert self.chunker is not None
                        for sentence in self.chunker.feed(token):
                            self._enqueue_tts(sentence)

            # Generation done — flush remaining sentence text + sentinel.
            assert self.chunker is not None
            for sentence in self.chunker.flush():
                self._enqueue_tts(sentence)
            if self._tts_queue is not None:
                self._tts_queue.put_nowait(None)
            # Let TTS finish playing (tts_end) before finalizing the turn.
            if tts_task is not None:
                await _suppress(tts_task)
            await self._finish_turn(builder)
        except asyncio.CancelledError:
            # Barge-in — the interrupt handler owns cancel/persist.
            if tts_task is not None and not tts_task.done():
                tts_task.cancel()
            raise
        except (AsrError, TtsError) as e:
            await self._send_error(e.code, e.message, fatal=e.fatal)
            if tts_task is not None and not tts_task.done():
                tts_task.cancel()
            if e.fatal:
                await self._fatal_close(e.code, e.message)
            else:
                await self._recover_to_listening(e.code)
        except Exception as e:  # noqa: BLE001 — surfaced to the client
            logger.exception("voice: agent turn failed")
            await self._send_error("agent_error", str(e), fatal=False)
            if tts_task is not None and not tts_task.done():
                tts_task.cancel()
            await self._recover_to_listening("agent_error")
        finally:
            self.current_agent_task = None
            self._tts_task = None
            if self._stream_marked:
                try:
                    await mark_stream_idle(conv_id_str)
                except Exception:  # noqa: BLE001 — best-effort marker
                    pass
                self._stream_marked = False

    async def _finish_turn(self, builder: BlockBuilder) -> None:
        """Persist the assistant message, notify, extract memory, LISTEN."""
        await self._persist_assistant(builder, interrupted=False)
        content = builder.get_text_content()
        if content:
            # Fire-and-forget pattern from chat.py generate(): the inline
            # watermark check is fast; the LLM extraction itself is
            # enqueued to the background queue (waits for stream idle).
            try:
                await maybe_run_memory_extraction(
                    self.conversation_id, content, self._history_messages
                )
            except Exception as e:  # noqa: BLE001 — non-fatal
                logger.warning("voice: memory extraction failed: %s", e)
        if self.state_machine.in_state(VoiceState.PROCESSING, VoiceState.SPEAKING):
            await self._apply_state(VoiceState.LISTENING, "turn_complete")
        self._agent_builder = None
        self.generation_id = None
        self._current_transcript = ""
        self._history_messages = []

    async def _persist_assistant(
        self, builder: BlockBuilder, interrupted: bool
    ) -> None:
        """Persist (and announce) the assistant message — shared by the
        normal end-of-turn and the barge-in partial path."""
        content = builder.get_text_content()
        if not builder.blocks:  # no meaningful text — nothing to persist
            return
        blocks = list(builder.to_db_blocks())
        msg_id: Optional[uuid_mod.UUID] = None
        try:
            msg_id = await persist_message_standalone(
                self.conversation_id,
                "assistant",
                content,
                model=self._model_label,
                blocks=blocks,
                generation_duration=(builder.generation_duration or None),
                deliverables=(builder.deliverables or None),
                modality="voice",
                completion_status="interrupted" if interrupted else "completed",
            )
        except Exception as e:  # noqa: BLE001 — DB errors are non-fatal
            logger.error("voice: assistant message persist failed: %s", e)
        if msg_id is None:
            await self._send_error(
                "persist_error",
                "failed to persist the assistant message",
                fatal=False,
            )
            return
        await self._send_json(
            {
                "type": "assistant_message",
                "message": {
                    "id": str(msg_id),
                    "role": "assistant",
                    "content": content,
                    "blocks": blocks,
                    "deliverables": builder.deliverables or None,
                    "modality": "voice",
                    "model": self._model_label,
                    "generationDuration": builder.generation_duration or None,
                    "completionStatus": (
                        "interrupted" if interrupted else "completed"
                    ),
                },
            }
        )

    # ── TTS worker (one per turn; sequential — never two streams) ────

    def _enqueue_tts(self, sentence: str) -> None:
        """Queue one sentence for synthesis (markdown/code stripped)."""
        cleaned = clean_for_tts(sentence)
        if not cleaned:
            return
        if self._tts_queue is not None:
            self._tts_queue.put_nowait(cleaned)

    async def _tts_worker(self) -> None:
        """Synthesize queued sentences sequentially; stream 0x03 frames.

        Every ``tts_chunk`` announce is IMMEDIATELY followed by its binary
        frame (atomic under the send lock) — the client drops binary audio
        that arrives without its announce.
        """
        generation_id = self.generation_id or uuid_mod.uuid4().hex
        sample_rate = settings.VOICE_TTS_SAMPLE_RATE
        seq = 0
        try:
            self._playback_done.clear()
            await self.tts.warm_up()
            await self._send_json(
                {
                    "type": "tts_start",
                    "generation_id": generation_id,
                    "sample_rate": sample_rate,
                    "speed": voice_settings.get()["speed"],
                }
            )
            assert self._tts_queue is not None
            while True:
                try:
                    item = await asyncio.wait_for(
                        self._tts_queue.get(), timeout=TTS_QUEUE_POLL_S
                    )
                except asyncio.TimeoutError:
                    # No sentence yet — give the chunker a flush-timeout
                    # tick (it may hold a complete-but-short tail).
                    if self.chunker is not None:
                        for sentence in self.chunker.feed(""):
                            self._enqueue_tts(sentence)
                    continue
                if item is None:
                    break
                async for pcm in self.tts.synthesize(str(item)):
                    if self._tts_interrupted:
                        return
                    seq += 1
                    await self._send_tts_chunk(generation_id, seq, pcm, sample_rate)
                    # First audio → SPEAKING.
                    if self.state_machine.state == VoiceState.PROCESSING:
                        await self._apply_state(
                            VoiceState.SPEAKING, "first_tts_audio", generation_id
                        )
                if self._tts_interrupted:
                    return
            await self._send_json(
                {
                    "type": "tts_end",
                    "generation_id": generation_id,
                    "sample_rate": sample_rate,
                }
            )
            if self._client_playback_ack and seq:
                try:
                    await asyncio.wait_for(self._playback_done.wait(), timeout=120.0)
                except asyncio.TimeoutError:
                    logger.warning(
                        "voice: browser playback acknowledgement timed out "
                        "for generation %s",
                        generation_id,
                    )
        except asyncio.CancelledError:
            raise
        except TtsError as e:
            await self._send_error(e.code, e.message, fatal=e.fatal)
            if e.fatal:
                await self._fatal_close(e.code, e.message)
                return
            # Recoverable (e.g. runtime missing): close the generation
            # cleanly so the client is not left waiting for tts_end.
            await self._send_tts_end_safe(generation_id, sample_rate)
        except Exception as e:  # noqa: BLE001 — surfaced to the client
            logger.exception("voice: TTS worker failed")
            await self._send_error("tts_error", str(e), fatal=False)
            await self._send_tts_end_safe(generation_id, sample_rate)

    async def _send_tts_end_safe(self, generation_id: str, sample_rate: int) -> None:
        try:
            await self._send_json(
                {
                    "type": "tts_end",
                    "generation_id": generation_id,
                    "sample_rate": sample_rate,
                }
            )
        except Exception:  # noqa: BLE001 — socket may already be gone
            pass

    async def _send_tts_chunk(
        self, generation_id: str, seq: int, pcm: bytes, sample_rate: int
    ) -> None:
        """tts_chunk announce + 0x03 binary — atomic (one lock hold)."""
        async with self._send_lock:
            await self._ws.send_json(
                {
                    "type": "tts_chunk",
                    "generation_id": generation_id,
                    "seq": seq,
                    "sample_rate": sample_rate,
                }
            )
            await self._ws.send_bytes(bytes([FRAME_TTS]) + pcm)
        self._touch()

    # ── barge-in / interruption ──────────────────────────────────────

    async def _handle_barge_in(self) -> None:
        """User speech onset (server VAD) or client ``interrupt`` while
        PROCESSING/SPEAKING: cancel everything, emit ``interrupted``, and
        return to LISTENING with the new utterance already captured."""
        if not self.state_machine.in_state(*INTERRUPTIBLE_STATES):
            return  # nothing to interrupt
        # An explicit browser interrupt is a trusted new-speech signal and
        # overrides the post-ASR stale-frame quarantine.
        self._ignore_mic_until = 0.0
        generation_id = self.generation_id
        await self._apply_state(VoiceState.INTERRUPTING, "barge_in", generation_id)
        # Snapshot mutable turn state BEFORE cancelling (the tasks' own
        # finally blocks reset their references).
        builder = self._agent_builder
        agent_task = self.current_agent_task
        tts_task = self._tts_task

        # (a) cancel the agent task.
        if agent_task is not None and not agent_task.done():
            agent_task.cancel()
            await _suppress(agent_task)
        # (b) cancel the TTS worker, (c) any pending TTS generation.
        self._tts_interrupted = True
        if self.tts is not None:
            try:
                await self.tts.cancel()
            except Exception:  # noqa: BLE001 — best-effort stop
                pass
        if tts_task is not None and not tts_task.done():
            tts_task.cancel()
            await _suppress(tts_task)
        # Drain any not-yet-synthesized sentences.
        if self._tts_queue is not None:
            while True:
                try:
                    self._tts_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

        # Emit interrupted AFTER all audio stopped — the client drops any
        # stale 0x03 frames of this generation from here on.
        if generation_id:
            await self._send_json(
                {"type": "interrupted", "generation_id": generation_id}
            )
        # Persist partial assistant content — only when meaningful.
        if builder is not None and builder.blocks:
            try:
                await self._persist_assistant(builder, interrupted=True)
            except Exception as e:  # noqa: BLE001 — non-fatal
                logger.error("voice: partial persist failed: %s", e)
        # Reset for the new utterance.
        if self.chunker is not None:
            self.chunker.reset()
        try:
            await self.asr.cancel()
        except Exception:  # noqa: BLE001 — best-effort reset
            pass
        self._tts_interrupted = False
        self._agent_builder = None
        self.generation_id = None
        self._current_transcript = ""
        self._history_messages = []
        await self._apply_state(VoiceState.LISTENING, "barge_in_complete")

    async def _cancel_active_turn(self) -> None:
        """Cancel the agent + TTS tasks without persisting (stop path)."""
        agent_task = self.current_agent_task
        tts_task = self._tts_task
        self._tts_interrupted = True
        if self.tts is not None:
            try:
                await self.tts.cancel()
            except Exception:  # noqa: BLE001 — best-effort stop
                pass
        if tts_task is not None and not tts_task.done():
            tts_task.cancel()
            await _suppress(tts_task)
        if agent_task is not None and not agent_task.done():
            agent_task.cancel()
            await _suppress(agent_task)
        self._tts_interrupted = False

    # ── errors / shutdown / cleanup ──────────────────────────────────

    def _touch(self) -> None:
        self._last_activity = time.monotonic()

    async def _send_json(self, payload: dict) -> None:
        """Serialized outbound JSON (single send lock — guarantees the
        tts_chunk announce is never separated from its binary frame)."""
        async with self._send_lock:
            await self._ws.send_json(payload)
        self._touch()

    async def _send_error(self, code: str, message: str, fatal: bool = False) -> None:
        try:
            await self._send_json(
                {"type": "error", "code": code, "message": message, "fatal": fatal}
            )
        except Exception:  # noqa: BLE001 — socket may already be gone
            pass

    async def _apply_state(
        self,
        new_state: VoiceState,
        reason: str = "",
        generation_id: Optional[str] = None,
    ) -> bool:
        """Transition + emit the state frame. Returns False when the
        transition is illegal from the current state (a race — logged and
        skipped, never raised)."""
        if not self.state_machine.can_transition(new_state):
            logger.warning(
                "voice: skipped illegal transition %s → %s (%s)",
                self.state_machine.state.value,
                new_state.value,
                reason,
            )
            return False
        record = self.state_machine.transition(new_state, reason, generation_id)
        await self._send_json(record.frame())
        return True

    async def _recover_to_listening(self, reason: str) -> None:
        """Best-effort recovery to LISTENING after a recoverable error."""
        try:
            record = self.state_machine.recover_to_listening(reason)
            await self._send_json(record.frame())
        except InvalidTransition as e:
            logger.warning("voice: recovery failed: %s", e)

    async def _fatal_close(self, code: str, message: str) -> None:
        """Fatal error: error frame → ERROR → STOPPING → close."""
        await self._send_error(code, message, fatal=True)
        if self.state_machine.can_transition(VoiceState.ERROR):
            await self._apply_state(VoiceState.ERROR, message)
        await self._shutdown(send_stopped=False, reason=f"fatal:{code}", close=True)

    async def _shutdown(
        self, send_stopped: bool, reason: str, close: bool = True
    ) -> None:
        """Wind the session down (optionally with a final `stopped`)."""
        if self._closed:
            return
        await self._cancel_active_turn()
        if self.state_machine.can_transition(VoiceState.STOPPING):
            await self._apply_state(VoiceState.STOPPING, reason)
        if send_stopped:
            try:
                await self._send_json({"type": "stopped"})
            except Exception:  # noqa: BLE001 — socket may already be gone
                pass
        self._closed = True
        if close and self._ws is not None:
            try:
                await self._ws.close()
            except Exception:  # noqa: BLE001 — best-effort close
                pass

    async def _idle_watchdog(self) -> None:
        """Close the session after VOICE_SESSION_IDLE_SEC without frames."""
        idle_sec = max(1.0, float(settings.VOICE_SESSION_IDLE_SEC))
        poll = max(1.0, min(10.0, idle_sec / 4.0))
        while not self._closed:
            await asyncio.sleep(poll)
            if self._closed:
                return
            if time.monotonic() - self._last_activity > idle_sec:
                logger.info(
                    "voice session %s idle >%.0fs — closing",
                    self.session_id,
                    idle_sec,
                )
                try:
                    await self._shutdown(
                        send_stopped=True, reason="idle_timeout", close=True
                    )
                except Exception:  # noqa: BLE001
                    logger.exception("voice: idle shutdown failed")
                return

    async def _cleanup(self) -> None:
        """Final teardown on disconnect: cancel everything, release
        markers. The manager registry entry is removed by the WS endpoint
        after handle() returns (session.py never imports manager — no
        import cycle)."""
        if self._cleaned:
            return
        self._cleaned = True
        self._closed = True
        # Cancel every background task (agent turn, TTS worker, watchdog).
        pending = [t for t in self._tasks if not t.done()]
        for task in pending:
            task.cancel()
        for task in pending:
            await _suppress(task)
        self._tasks.clear()
        # Close engines (never leave a loaded runtime mid-generation).
        for cancel in (
            getattr(self.asr, "cancel", None),
            getattr(self.tts, "cancel", None),
        ):
            if callable(cancel):
                try:
                    await cancel()
                except Exception:  # noqa: BLE001 — teardown best effort
                    pass
        # KV-cache marker release (idempotent).
        try:
            await mark_stream_idle(str(self.conversation_id))
        except Exception:  # noqa: BLE001 — teardown best effort
            pass
        logger.info("voice session %s cleaned up", self.session_id)
