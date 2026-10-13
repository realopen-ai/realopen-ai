"""Database-backed conversation-agent preferences and request-local snapshots."""

import functools
import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db.models import AppState

logger = logging.getLogger(__name__)
STATE_KEY = "ai.agent_preferences"
_overrides = {}
_snapshot = ContextVar("agent_preferences_snapshot", default=None)


class AgentPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context_window: int = Field(ge=2048, le=262144, strict=True)
    generation_limit: int = Field(ge=128, le=65536, strict=True)
    history_compaction: StrictBool
    compaction_threshold: float = Field(ge=0.5, le=0.95)
    recent_turns: int = Field(ge=0, le=32, strict=True)
    tool_calling: Literal["auto", "text"]
    repair_attempts: int = Field(ge=0, le=5, strict=True)

    @model_validator(mode="after")
    def check_budget(self):
        windows = [self.context_window, *settings.AGENT_CONTEXT_WINDOWS.values()]
        if self.generation_limit + settings.AGENT_CONTEXT_MARGIN_TOKENS >= min(windows):
            raise ValueError(
                "Generation limit plus the safety margin must be smaller than every configured context window"
            )
        return self


def defaults():
    return {
        "context_window": settings.AGENT_CONTEXT_WINDOW,
        "generation_limit": settings.AGENT_OUTPUT_RESERVE_TOKENS,
        "history_compaction": settings.CONTEXT_COMPACT_ENABLED,
        "compaction_threshold": settings.CONTEXT_COMPACT_THRESHOLD,
        "recent_turns": settings.CONTEXT_COMPACT_PRESERVE_TURNS,
        "tool_calling": settings.AGENT_TOOL_CALLING_MODE,
        "repair_attempts": settings.AGENT_TOOL_VALIDATION_MAX_FAILURES - 1,
    }


def effective():
    snapshot = _snapshot.get()
    if snapshot is not None:
        return snapshot
    # Environment defaults retain existing server-config semantics. Validate
    # user/database overrides separately, without mutating the Settings object.
    return AgentPreferences.model_construct(**{**defaults(), **_overrides})


def describe():
    return {
        "settings": effective().model_dump(),
        "defaults": defaults(),
        "model_context_overrides": dict(settings.AGENT_CONTEXT_WINDOWS),
        "safety_margin": settings.AGENT_CONTEXT_MARGIN_TOKENS,
    }


async def load(db):
    global _overrides
    row = await db.get(AppState, STATE_KEY)
    values = {}
    if row and row.value:
        try:
            stored = json.loads(row.value)
            if stored.get("schema_version") != 1:
                raise ValueError("Unsupported preferences version")
            values = AgentPreferences.model_validate(stored["settings"]).model_dump()
        except (ValueError, TypeError, KeyError, AttributeError):
            logger.warning("Invalid saved agent preferences; using server defaults")
    _overrides = values
    return describe()


async def save(db, preferences):
    global _overrides
    values = preferences.model_dump()
    encoded = json.dumps({"schema_version": 1, "settings": values})
    updated = datetime.now(timezone.utc)
    statement = insert(AppState).values(
        key=STATE_KEY, value=encoded, updated_at=updated
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[AppState.key],
            set_={"value": encoded, "updated_at": updated},
        )
    )
    await db.commit()
    # A failed write must never change the live configuration.
    _overrides = values
    return describe()


async def reset(db):
    global _overrides
    await db.execute(delete(AppState).where(AppState.key == STATE_KEY))
    await db.commit()
    _overrides = {}
    return describe()


def with_snapshot(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        preferences = effective().model_copy(deep=True)
        generator = function(*args, **kwargs)
        try:
            while True:
                token = _snapshot.set(preferences)
                try:
                    event = await anext(generator)
                except StopAsyncIteration:
                    return
                finally:
                    _snapshot.reset(token)
                yield event
        finally:
            token = _snapshot.set(preferences)
            try:
                await generator.aclose()
            finally:
                _snapshot.reset(token)

    return wrapped
