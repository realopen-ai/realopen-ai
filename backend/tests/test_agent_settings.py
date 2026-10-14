"""Persistent advanced settings; no real model, network or PostgreSQL required."""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.models import router
from app.config import settings
from app.db.models import AppState
from app.db.session import get_db
from app.services import (
    agent_settings,
    coder_settings,
    context_compactor,
    tool_capabilities,
)


class Adapter:
    def __init__(self, session):
        self.session = session

    async def get(self, model, key):
        return self.session.get(model, key)

    async def execute(self, statement):
        return self.session.execute(statement)

    async def commit(self):
        self.session.commit()


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr(agent_settings, "_overrides", {})
    monkeypatch.setattr(coder_settings, "_overrides", {})
    monkeypatch.setattr(settings, "AGENT_CONTEXT_WINDOW", 8192)
    monkeypatch.setattr(settings, "AGENT_CONTEXT_WINDOWS", {})
    monkeypatch.setattr(settings, "AGENT_OUTPUT_RESERVE_TOKENS", 2048)
    monkeypatch.setattr(settings, "AGENT_CONTEXT_MARGIN_TOKENS", 256)
    monkeypatch.setattr(settings, "CONTEXT_COMPACT_ENABLED", True)
    monkeypatch.setattr(settings, "AGENT_TOOL_CALLING_MODE", "auto")


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    AppState.__table__.create(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def client(database):
    app = FastAPI()
    app.include_router(router, prefix="/api")

    async def dependency():
        with Session(database) as session:
            yield Adapter(session)

    app.dependency_overrides[get_db] = dependency
    with TestClient(app) as result:
        yield result


def preferences(**overrides):
    return {**agent_settings.defaults(), **overrides}


def test_preferences_persist_and_reload_after_runtime_cache_reset(
    client, database, monkeypatch
):
    initial = client.get("/api/agent/settings")
    assert initial.status_code == 200
    assert initial.json()["settings"] == agent_settings.defaults()
    values = preferences(
        context_window=16384,
        generation_limit=4096,
        tool_calling="text",
        recent_turns=4,
        repair_attempts=1,
    )
    saved = client.put("/api/agent/settings", json=values)
    assert saved.status_code == 200, saved.text
    assert saved.json()["settings"] == values
    with Session(database) as session:
        row = session.get(AppState, agent_settings.STATE_KEY)
        assert json.loads(row.value)["settings"] == values
    monkeypatch.setattr(agent_settings, "_overrides", {})
    assert client.get("/api/agent/settings").json()["settings"] == values
    assert agent_settings.effective().generation_limit == 4096
    assert context_compactor.context_budget("local").window == 16384
    assert context_compactor.context_budget("local").output == 4096
    assert not tool_capabilities.should_try_native("local")


def test_reset_removes_database_override_and_restores_environment(client, database):
    client.put("/api/agent/settings", json=preferences(tool_calling="text"))
    result = client.delete("/api/agent/settings")
    assert result.status_code == 200
    assert result.json()["settings"] == agent_settings.defaults()
    with Session(database) as session:
        assert session.get(AppState, agent_settings.STATE_KEY) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"context_window": 1024},
        {"generation_limit": 8192},
        {"generation_limit": True},
        {"generation_limit": "4096"},
        {"history_compaction": "false"},
        {"compaction_threshold": 1},
        {"recent_turns": -1},
        {"repair_attempts": 6},
        {"tool_calling": "unsupported"},
        {"unexpected": "field"},
    ],
)
def test_invalid_settings_are_rejected_without_changing_saved_values(client, overrides):
    before = client.get("/api/agent/settings").json()["settings"]
    result = client.put("/api/agent/settings", json=preferences(**overrides))
    assert result.status_code == 422
    assert client.get("/api/agent/settings").json()["settings"] == before


def test_model_context_overrides_are_honored_and_limit_output(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_CONTEXT_WINDOWS", {"small": 4096})
    assert context_compactor.context_budget("small").window == 4096
    with pytest.raises(ValidationError, match="every configured context window"):
        agent_settings.AgentPreferences(
            **preferences(context_window=16384, generation_limit=4096)
        )


@pytest.mark.asyncio
async def test_failed_database_commit_does_not_change_live_preferences():
    before = agent_settings.effective().model_dump()
    db = SimpleNamespace(
        execute=AsyncMock(),
        commit=AsyncMock(side_effect=RuntimeError("database unavailable")),
    )
    with pytest.raises(RuntimeError):
        await agent_settings.save(
            db, agent_settings.AgentPreferences(**preferences(tool_calling="text"))
        )
    assert agent_settings.effective().model_dump() == before


@pytest.mark.asyncio
async def test_upsert_binds_naive_utc_for_postgres_insert_and_update(monkeypatch):
    instant = datetime(2026, 10, 13, 20, 44, 47, tzinfo=timezone.utc)

    class Clock:
        @staticmethod
        def now(tz):
            assert tz is timezone.utc
            return instant

    monkeypatch.setattr(agent_settings, "datetime", Clock)
    db = SimpleNamespace(execute=AsyncMock(), commit=AsyncMock())
    await agent_settings.save(db, agent_settings.AgentPreferences(**preferences()))
    statement = db.execute.call_args.args[0]
    compiled = statement.compile(dialect=postgresql.dialect())
    assert not AppState.__table__.c.updated_at.type.timezone
    timestamps = [
        value for value in compiled.params.values() if isinstance(value, datetime)
    ]
    assert len(timestamps) == 2  # Both INSERT and ON CONFLICT UPDATE paths.
    assert all(value.tzinfo is None for value in timestamps)
    assert all(value == instant.replace(tzinfo=None) for value in timestamps)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value",
    [
        "not JSON",
        '{"schema_version":999}',
        '{"schema_version":1,"settings":{"context_window":0}}',
    ],
)
async def test_corrupt_or_unknown_saved_settings_fall_back_without_overwriting(value):
    db = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(value=value)))
    assert (await agent_settings.load(db))["settings"] == agent_settings.defaults()


@pytest.mark.asyncio
async def test_disabled_compaction_still_enforces_hard_input_budget(monkeypatch):
    monkeypatch.setattr(agent_settings, "_overrides", {"history_compaction": False})
    summarize = AsyncMock()
    monkeypatch.setattr(context_compactor, "_llm_summarize", summarize)
    small = [{"role": "user", "content": "Hello"}]
    assert await context_compactor.compact_conversation(small, "local") == (
        small,
        False,
    )
    large = [{"role": "user", "content": "x" * 30000}]
    with pytest.raises(
        context_compactor.ContextBudgetExceeded, match="compaction is disabled"
    ):
        await context_compactor.compact_conversation(large, "local")
    summarize.assert_not_awaited()


@pytest.mark.asyncio
async def test_active_request_keeps_snapshot_while_new_request_sees_saved_changes(
    monkeypatch,
):
    @agent_settings.with_snapshot
    async def generation():
        yield agent_settings.effective().generation_limit
        await asyncio.sleep(0)
        yield agent_settings.effective().generation_limit

    running = generation()
    assert await anext(running) == 2048
    monkeypatch.setattr(agent_settings, "_overrides", {"generation_limit": 1024})
    assert agent_settings.effective().generation_limit == 1024
    assert await anext(running) == 2048
    new = generation()
    assert await anext(new) == 1024
    await new.aclose()
    await running.aclose()
    assert agent_settings.effective().generation_limit == 1024


def test_coder_preferences_persist_reset_and_do_not_change_general_settings(
    client, database, monkeypatch
):
    defaults = coder_settings.defaults()
    assert defaults["context_window"] == 16384
    assert defaults["generation_limit"] == 4096
    before = client.get("/api/agent/settings").json()["settings"]
    values = {**defaults, "generation_limit": 6144, "max_rounds": 20}
    result = client.put("/api/coder/settings", json=values)
    assert result.status_code == 200, result.text
    monkeypatch.setattr(coder_settings, "_overrides", {})
    assert client.get("/api/coder/settings").json()["settings"] == values
    assert client.get("/api/agent/settings").json()["settings"] == before
    assert client.delete("/api/coder/settings").json()["settings"] == defaults
    with Session(database) as session:
        assert session.get(AppState, coder_settings.STATE_KEY) is None


@pytest.mark.parametrize(
    "change",
    [
        {"context_window": 1024},
        {"context_window": True},
        {"generation_limit": 16384},
        {"max_rounds": 97},
        {"max_rounds": 0},
        {"unknown": 1},
    ],
)
def test_invalid_coder_preferences_do_not_change_saved_values(client, change):
    before = client.get("/api/coder/settings").json()["settings"]
    assert (
        client.put("/api/coder/settings", json={**before, **change}).status_code == 422
    )
    assert client.get("/api/coder/settings").json()["settings"] == before


@pytest.mark.asyncio
async def test_coder_snapshot_is_frozen_and_isolated_from_parallel_general_requests(
    monkeypatch,
):
    entered, release = asyncio.Event(), asyncio.Event()

    @coder_settings.with_snapshot
    async def coder():
        entered.set()
        await release.wait()
        return (
            coder_settings.effective().generation_limit,
            context_compactor.context_budget("local").window,
        )

    running = asyncio.create_task(coder())
    await entered.wait()
    assert context_compactor.context_budget("local").window == 8192
    monkeypatch.setattr(coder_settings, "_overrides", {"generation_limit": 6000})
    release.set()
    assert await running == (4096, 16384)
    assert coder_settings.effective().generation_limit == 6000
    assert agent_settings.effective().generation_limit == 2048


@pytest.mark.asyncio
async def test_failed_coder_commit_does_not_publish_preferences():
    before = coder_settings.effective().model_dump()
    db = SimpleNamespace(
        execute=AsyncMock(), commit=AsyncMock(side_effect=RuntimeError("DB failed"))
    )
    with pytest.raises(RuntimeError):
        await coder_settings.save(
            db, coder_settings.CoderPreferences(**{**before, "max_rounds": 10})
        )
    assert coder_settings.effective().model_dump() == before
    timestamps = [
        value
        for value in db.execute.call_args.args[0]
        .compile(dialect=postgresql.dialect())
        .params.values()
        if isinstance(value, datetime)
    ]
    assert len(timestamps) == 2 and all(value.tzinfo is None for value in timestamps)
