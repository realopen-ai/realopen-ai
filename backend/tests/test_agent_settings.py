"""Persistent advanced settings; no real model, network or PostgreSQL required."""

import asyncio
import json
from unittest.mock import AsyncMock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.models import router
from app.config import settings
from app.db.models import AppState
from app.db.session import get_db
from app.services import agent_settings, context_compactor, tool_capabilities


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
