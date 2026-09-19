"""Voice session registry — one session per conversation.

The manager enforces the single-session-per-conversation invariant: a
second WebSocket for a conversation that already has a LIVE session is
REJECTED (error frame ``session_exists``, fatal) so the old connection
keeps streaming — no silent takeover.

The global :data:`voice_manager` singleton is used by the ``/ws/voice``
endpoint in ``app/main.py``. Session removal happens in the endpoint's
``finally`` block after ``session.handle()`` returns (``session.py``
never imports this module — no import cycle).
"""

from __future__ import annotations

import asyncio
import logging
import uuid as uuid_mod
from typing import Dict, Optional

from fastapi import WebSocket

from app.voice.session import VoiceSession

logger = logging.getLogger(__name__)


class VoiceSessionManager:
    """Registry of live voice sessions keyed by conversation id."""

    def __init__(self) -> None:
        self._sessions: Dict[str, VoiceSession] = {}
        self._lock = asyncio.Lock()

    async def get_or_create(
        self, conversation_id: str, ws: WebSocket
    ) -> Optional[VoiceSession]:
        """Return the session for this conversation, creating it when none
        is live. Returns None when the request must be rejected (a live
        session exists, or the conversation id is not a valid UUID) — in
        that case the rejection frame has already been sent to ``ws`` and
        the socket is closed."""
        key = str(conversation_id)
        async with self._lock:
            existing = self._sessions.get(key)
            if existing is not None and not existing.closed:
                logger.info(
                    "voice: rejecting second session for conversation %s",
                    key,
                )
                await self._reject(ws, key)
                return None
            try:
                conv_uuid = uuid_mod.UUID(key)
            except (ValueError, TypeError, AttributeError):
                await self._reject(ws, key, code="invalid_conversation_id")
                return None
            session = VoiceSession(conv_uuid, ws)
            self._sessions[key] = session
            logger.info(
                "voice: session created for conversation %s (active=%d)",
                key,
                len(self._sessions),
            )
            return session

    @staticmethod
    async def _reject(ws: WebSocket, key: str, code: str = "session_exists") -> None:
        """Send the fatal rejection frame and close the new socket."""
        try:
            await ws.send_json(
                {
                    "type": "error",
                    "code": code,
                    "message": (
                        "A voice session is already active for this "
                        "conversation."
                        if code == "session_exists"
                        else f"conversation_id is not a valid UUID: {key}"
                    ),
                    "fatal": True,
                }
            )
        except Exception:  # noqa: BLE001 — socket already gone
            pass
        try:
            await ws.close(code=1008)
        except Exception:  # noqa: BLE001 — best-effort close
            pass

    def get(self, conversation_id: str) -> Optional[VoiceSession]:
        """The live session for a conversation (None when absent/dead)."""
        session = self._sessions.get(str(conversation_id))
        if session is not None and session.closed:
            # Opportunistic cleanup of dead entries.
            self._sessions.pop(str(conversation_id), None)
            return None
        return session

    def remove(self, conversation_id: str) -> None:
        """Drop the registry entry (endpoint finally-block)."""
        key = str(conversation_id)
        removed = self._sessions.pop(key, None)
        if removed is not None:
            logger.info(
                "voice: session removed for conversation %s (active=%d)",
                key,
                len(self._sessions),
            )

    @property
    def active_count(self) -> int:
        return sum(
            1 for s in self._sessions.values() if not s.closed
        )


# Global singleton wired into app/main.py's /ws/voice endpoint.
voice_manager = VoiceSessionManager()
