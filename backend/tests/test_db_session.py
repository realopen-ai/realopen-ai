"""
Tests for the async DB session layer (app/db/session.py).

Scope:
- get_db() dependency: yields the session, commits on success, closes always.
- get_db() error path: rolls back (no commit), re-raises, still closes.
- Module-level engine configuration: pool sizing (10 + 20 overflow),
  pre-ping, asyncpg driver, echo bound to settings.DEBUG.
- async_session_factory binds the module engine with expire_on_commit=False.
- engine.dispose() is safe with an empty pool (no connection attempt).

Mocking: `app.db.session.async_session_factory` is replaced by a fake
session factory — NO real database connection is ever made (per the
project testing conventions). The placeholder DATABASE_URL set by
conftest.py keeps the import-time create_async_engine() happy; the engine
itself is lazy and never connects.
"""

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import session as db_session


class FakeAsyncSession:
    """Minimal async session double recording commit/rollback/close calls."""

    def __init__(self):
        self.commit_calls = 0
        self.rollback_calls = 0
        self.close_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        self.rollback_calls += 1

    async def close(self):
        self.close_calls += 1


# ── get_db: success path ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_db_yields_session_commits_and_closes(monkeypatch):
    fake = FakeAsyncSession()
    monkeypatch.setattr(db_session, "async_session_factory", lambda: fake)

    gen = db_session.get_db()
    session = await gen.__anext__()
    assert session is fake

    # Consumer returns normally -> generator finishes
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()

    assert fake.commit_calls == 1
    assert fake.rollback_calls == 0
    assert fake.close_calls == 1


@pytest.mark.asyncio
async def test_get_db_commits_only_after_consumer_finishes(monkeypatch):
    """The commit must happen AFTER the consumer is done with the session."""
    fake = FakeAsyncSession()
    monkeypatch.setattr(db_session, "async_session_factory", lambda: fake)

    gen = db_session.get_db()
    await gen.__anext__()
    # Still inside the consumer — nothing committed or closed yet
    assert fake.commit_calls == 0
    assert fake.close_calls == 0
    with pytest.raises(StopAsyncIteration):
        await gen.__anext__()
    assert fake.commit_calls == 1


# ── get_db: error path ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_db_rolls_back_and_reraises_on_error(monkeypatch):
    fake = FakeAsyncSession()
    monkeypatch.setattr(db_session, "async_session_factory", lambda: fake)

    gen = db_session.get_db()
    await gen.__anext__()

    with pytest.raises(ValueError, match="consumer failed"):
        await gen.athrow(ValueError("consumer failed"))

    assert fake.rollback_calls == 1
    assert fake.commit_calls == 0  # never commit a failed transaction
    assert fake.close_calls == 1  # but ALWAYS close


@pytest.mark.asyncio
async def test_get_db_reraises_original_exception_type(monkeypatch):
    fake = FakeAsyncSession()
    monkeypatch.setattr(db_session, "async_session_factory", lambda: fake)

    gen = db_session.get_db()
    await gen.__anext__()

    with pytest.raises(KeyError, match="missing"):
        await gen.athrow(KeyError("missing"))
    assert fake.rollback_calls == 1


@pytest.mark.asyncio
async def test_get_db_factory_isolation_between_calls(monkeypatch):
    """Each dependency invocation gets a fresh session from the factory."""
    sessions = [FakeAsyncSession(), FakeAsyncSession()]
    calls = []

    def factory():
        calls.append(1)
        return sessions[len(calls) - 1]

    monkeypatch.setattr(db_session, "async_session_factory", factory)

    for expected in sessions:
        gen = db_session.get_db()
        assert await gen.__anext__() is expected
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()

    assert len(calls) == 2
    assert all(s.commit_calls == 1 and s.close_calls == 1 for s in sessions)


# ── Engine configuration (no connection is opened) ─────────────────────


def test_engine_pool_configuration():
    """Pool sized for digestion concurrency: 10 base + 20 overflow, pre-ping."""
    pool = db_session.engine.pool
    assert pool.size() == 10
    assert pool._max_overflow == 20  # noqa: SL001 — pinned behaviour
    assert pool._pre_ping is True  # noqa: SL001 — pinned behaviour


def test_engine_uses_asyncpg_driver_and_configured_url():
    assert db_session.engine.url.drivername == "postgresql+asyncpg"
    assert db_session.engine.url.database == settings.DATABASE_URL.rsplit("/", 1)[-1]


def test_engine_echo_follows_debug_setting():
    assert db_session.engine.echo == bool(settings.DEBUG)


def test_session_factory_binds_module_engine():
    kw = db_session.async_session_factory.kw
    assert kw["bind"] is db_session.engine
    assert kw["expire_on_commit"] is False
    # sessionmaker stores the session class as an attribute, not in kw
    assert db_session.async_session_factory.class_ is AsyncSession


@pytest.mark.asyncio
async def test_engine_dispose_with_empty_pool_is_safe():
    """dispose() on a never-used engine must not attempt any connection."""
    await db_session.engine.dispose()
    # A fresh pool is installed, still correctly configured
    assert db_session.engine.pool.size() == 10
