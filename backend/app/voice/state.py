"""Deterministic voice session state machine.

States (server-authoritative — the client mirrors them via `state` frames):

    IDLE ──start──▶ LISTENING ──VAD end-of-speech──▶ PROCESSING
                        ▲                              │
                        │                    first TTS chunk
                        │                              ▼
                        │ barge-in                 SPEAKING
                        │  (cancel + persist          │
                        │   partial)              barge-in
                        │                              ▼
                        └──── INTERRUPTING ◀──────────┘
                        │  (also from PROCESSING on barge-in)
    any state ──stop──▶ STOPPING ──▶ closed
    any state ──error──▶ ERROR ──recoverable──▶ LISTENING
                          └─fatal──▶ closed

Transitions are validated: an invalid transition raises
:class:`InvalidTransition` (a programming error — the session code must
never attempt one). Every accepted transition records the frame that the
session should emit to the client (`{"type":"state", ...}`).
"""

from __future__ import annotations

import time
from enum import Enum
from typing import List, Optional


class VoiceState(str, Enum):
    IDLE = "IDLE"
    LISTENING = "LISTENING"
    PROCESSING = "PROCESSING"
    SPEAKING = "SPEAKING"
    INTERRUPTING = "INTERRUPTING"
    STOPPING = "STOPPING"
    ERROR = "ERROR"


class InvalidTransition(Exception):
    """Raised when a transition is not allowed by the state machine."""


# The complete, deterministic transition table.
TRANSITIONS = {
    VoiceState.IDLE: {
        VoiceState.LISTENING,  # start frame accepted
        VoiceState.STOPPING,   # stop before ready (never happens, but legal)
        VoiceState.ERROR,      # handshake failure
    },
    VoiceState.LISTENING: {
        VoiceState.PROCESSING,  # VAD end-of-speech with a final transcript
        VoiceState.INTERRUPTING,  # cannot happen (no turn), but kept legal
        VoiceState.STOPPING,     # user stop
        VoiceState.ERROR,        # recoverable error (→ back to LISTENING)
    },
    VoiceState.PROCESSING: {
        VoiceState.SPEAKING,      # first TTS chunk
        VoiceState.LISTENING,     # turn finished without TTS (or ASR empty)
        VoiceState.INTERRUPTING,  # barge-in before first audio
        VoiceState.STOPPING,
        VoiceState.ERROR,
    },
    VoiceState.SPEAKING: {
        VoiceState.INTERRUPTING,  # barge-in during TTS
        VoiceState.LISTENING,     # TTS finished normally
        VoiceState.STOPPING,
        VoiceState.ERROR,         # e.g. TTS chunk timeout (recoverable)
    },
    VoiceState.INTERRUPTING: {
        VoiceState.LISTENING,   # cancel + partial-persist done
        VoiceState.STOPPING,    # user stop lands mid-interrupt
        VoiceState.ERROR,
    },
    VoiceState.STOPPING: set(),  # terminal — the WS closes right after
    VoiceState.ERROR: {
        VoiceState.LISTENING,  # recoverable error (fatal=false)
        VoiceState.STOPPING,   # user/fatal error → close
        VoiceState.ERROR,      # another error before recovery
    },
}

# States from which an interruption may begin.
INTERRUPTIBLE_STATES = {VoiceState.PROCESSING, VoiceState.SPEAKING}

# States the session may recover to LISTENING from.
RECOVERABLE_STATES = {
    VoiceState.PROCESSING,
    VoiceState.SPEAKING,
    VoiceState.INTERRUPTING,
    VoiceState.ERROR,
}


class StateTransition:
    """One recorded transition (frame payload + metadata)."""

    __slots__ = ("from_state", "to_state", "reason", "generation_id", "at")

    def __init__(
        self,
        from_state: VoiceState,
        to_state: VoiceState,
        reason: str = "",
        generation_id: Optional[str] = None,
    ):
        self.from_state = from_state
        self.to_state = to_state
        self.reason = reason
        self.generation_id = generation_id
        self.at = time.monotonic()

    def frame(self) -> dict:
        """The `state` JSON frame to send to the client."""
        payload = {
            "type": "state",
            "state": self.to_state.value,
            "reason": self.reason,
        }
        if self.generation_id:
            payload["generation_id"] = self.generation_id
        return payload


class VoiceStateMachine:
    """Server-authoritative, deterministic state machine.

    ``transition()`` validates against :data:`TRANSITIONS`, records the
    transition, and returns the state frame to emit. Transitions from a
    terminal state (STOPPING) always raise. The session is responsible for
    closing the socket after STOPPING/terminal errors.
    """

    def __init__(self, initial: VoiceState = VoiceState.IDLE):
        self._state = initial
        self._history: List[StateTransition] = []

    @property
    def state(self) -> VoiceState:
        return self._state

    @property
    def history(self) -> List[StateTransition]:
        return list(self._history)

    def can_transition(self, new_state: VoiceState) -> bool:
        return new_state in TRANSITIONS[self._state]

    def transition(
        self,
        new_state: VoiceState,
        reason: str = "",
        generation_id: Optional[str] = None,
    ) -> StateTransition:
        """Apply a transition. Raises InvalidTransition when disallowed."""
        if not isinstance(new_state, VoiceState):
            raise InvalidTransition(
                f"target must be a VoiceState, got {new_state!r}"
            )
        if new_state not in TRANSITIONS[self._state]:
            raise InvalidTransition(
                f"illegal transition {self._state.value} -> {new_state.value} "
                f"(reason={reason!r})"
            )
        record = StateTransition(
            self._state, new_state, reason=reason, generation_id=generation_id
        )
        self._state = new_state
        self._history.append(record)
        return record

    def recover_to_listening(self, reason: str = "recover") -> StateTransition:
        """Best-effort recovery to LISTENING from any recoverable state.

        From LISTENING itself this is a no-op returning the current state's
        (re-assert) transition — used by error paths so the session never
        gets stuck. Raises InvalidTransition only from STOPPING (terminal).
        """
        if self._state == VoiceState.LISTENING:
            return StateTransition(
                self._state, VoiceState.LISTENING, reason=reason
            )
        if self._state == VoiceState.STOPPING:
            raise InvalidTransition("cannot recover from terminal STOPPING state")
        return self.transition(VoiceState.LISTENING, reason=reason)

    def in_state(self, *states: VoiceState) -> bool:
        return self._state in states

    def snapshot(self) -> dict:
        return {
            "state": self._state.value,
            "transitions": [
                {
                    "from": t.from_state.value,
                    "to": t.to_state.value,
                    "reason": t.reason,
                }
                for t in self._history
            ],
        }
