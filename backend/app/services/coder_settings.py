"""Database-backed coder inference preferences and independent context budgets."""

import functools
import json
import logging
from contextvars import ContextVar
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from app.config import settings
from app.db.models import AppState
from app.services import agent_settings

logger = logging.getLogger(__name__)
STATE_KEY = "ai.coder_preferences"
_overrides = {}
_snapshot = ContextVar("coder_preferences_snapshot", default=None)
DEFAULT_THINKING = True
DEFAULT_TEMPERATURE = 0.6


class CoderPreferences(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_window: int = Field(ge=2048, le=524288, strict=True)
    generation_limit: int = Field(ge=128, le=65536, strict=True)
    max_rounds: int = Field(ge=4, le=96, strict=True)
    thinking: bool = Field(default=DEFAULT_THINKING, strict=True)
    temperature: float = Field(
        default=DEFAULT_TEMPERATURE, ge=0, le=2, strict=True, allow_inf_nan=False
    )
    handoff_tokens: int = Field(default=2048, ge=128, le=8192, strict=True)
    source_context_tokens: int = Field(default=4096, ge=0, le=65536, strict=True)
    recent_context_tokens: int = Field(default=4096, ge=0, le=65536, strict=True)
    tool_output_tokens: int = Field(default=1536, ge=128, le=16384, strict=True)

    @model_validator(mode="after")
    def check_budget(self):
        if self.generation_limit + settings.AGENT_CONTEXT_MARGIN_TOKENS >= min(
            [self.context_window, *settings.AGENT_CONTEXT_WINDOWS.values()]
        ):
            raise ValueError(
                "Generation limit plus safety margin must be smaller than every configured coder context window"
            )
        return self


def defaults():
    return {
        "context_window": min(524288, settings.AGENT_CONTEXT_WINDOW * 2),
        "generation_limit": min(65536, settings.AGENT_OUTPUT_RESERVE_TOKENS * 2),
        "max_rounds": min(96, max(4, settings.AGENT_MAX_ROUNDS * 2)),
        "thinking": DEFAULT_THINKING,
        "temperature": DEFAULT_TEMPERATURE,
        "handoff_tokens": 2048,
        "source_context_tokens": 4096,
        "recent_context_tokens": 4096,
        "tool_output_tokens": 1536,
    }


def effective():
    return _snapshot.get() or CoderPreferences.model_construct(
        **{**defaults(), **_overrides}
    )


def describe():
    return {
        "settings": effective().model_dump(),
        "defaults": defaults(),
        "safety_margin": settings.AGENT_CONTEXT_MARGIN_TOKENS,
        "model_context_overrides": dict(settings.AGENT_CONTEXT_WINDOWS),
    }


async def load(db):
    global _overrides
    row = await db.get(AppState, STATE_KEY)
    values = {}
    if row and row.value:
        try:
            data = json.loads(row.value)
            if data.get("schema_version") != 1:
                raise ValueError("Unsupported coder preferences version")
            values = CoderPreferences.model_validate(data["settings"]).model_dump()
        except (ValueError, TypeError, KeyError, AttributeError):
            logger.warning("Invalid coder preferences; using server defaults")
    _overrides = values
    return describe()


async def save(db, preferences):
    global _overrides
    values = preferences.model_dump()
    updated = datetime.now(timezone.utc).replace(tzinfo=None)
    encoded = json.dumps({"schema_version": 1, "settings": values})
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
        token = _snapshot.set(preferences)
        try:
            shared = agent_settings.effective().model_copy(
                update={
                    "context_window": preferences.context_window,
                    "generation_limit": preferences.generation_limit,
                },
                deep=True,
            )
            with agent_settings.preference_snapshot(shared):
                return await function(*args, **kwargs)
        finally:
            _snapshot.reset(token)

    return wrapped
