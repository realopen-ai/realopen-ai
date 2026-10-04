"""Tests for app/services/conversation_memory.py.

What is tested:
  - _store_summary: missing conversation, summary + embedding persisted,
    embedding-unavailable path.
  - maybe_summarize_conversation: missing conversation, cooldown skip,
    below min_messages, successful summarization, summarizer returning
    None / empty, exception swallow.
  - get_recent_summaries: dict shape, "Untitled" fallback, epoch seconds,
    exception → [].
  - get_relevant_summaries: vector path (row mapping, exclusion of the
    current conversation, top_k cap), embedding-unavailable fallback to
    keyword search, vector failure fallback.
  - _keyword_summary_search: no significant keywords, result mapping and
    limit, exception → [].
  - build_cross_session_context: no summaries at all, relevant summaries
    formatting, recent-summaries fallback, 80-word truncation.

What is mocked:
  - The AsyncSession is replaced by FakeDB (select() results + raw SQL
    rows) — no real Postgres connection.
  - app.services.conversation_memory.get_embedding is patched so nothing
    reaches Ollama; summarize_conversation and the module-internal search
    helpers are patched where the unit under test is the orchestrator.
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import Select, TextClause

import app.services.conversation_memory as cm
from app.config import settings
from app.db.models import Conversation, Message
from app.services.conversation_memory import (
    _keyword_summary_search,
    _log,
    _store_summary,
    build_cross_session_context,
    get_recent_summaries,
    get_relevant_summaries,
    maybe_summarize_conversation,
)


# ══════════════════════════════════════════════════════════════════════
# Test doubles
# ══════════════════════════════════════════════════════════════════════


class _ScalarsResult:
    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return self

    def all(self):
        return list(self._items)


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class FakeDB:
    """AsyncSession double: Select → _ScalarsResult, TextClause → _RowsResult."""

    def __init__(self, select_results=None, text_rows=None):
        self._select_results = [list(r) for r in (select_results or [])]
        self._text_rows = text_rows or []
        self.flush_count = 0
        self.executed = []

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        if isinstance(stmt, Select):
            result = self._select_results.pop(0) if self._select_results else []
            return _ScalarsResult(result)
        if isinstance(stmt, TextClause):
            return _RowsResult(self._text_rows)
        raise AssertionError(f"unexpected statement type: {type(stmt)!r}")

    async def get(self, model, key):
        raise AssertionError("unexpected db.get() call")

    async def flush(self):
        self.flush_count += 1


def make_conversation(**kwargs) -> Conversation:
    defaults = dict(
        id=uuid.uuid4(),
        title="Trip planning",
        model="qwen3:4b",
        summary=None,
        summary_embedding=None,
        summary_at=None,
    )
    defaults.update(kwargs)
    return Conversation(**defaults)


@pytest.fixture
def no_embedding():
    with patch.object(cm, "get_embedding", new=AsyncMock(return_value=None)):
        yield


@pytest.fixture
def some_embedding():
    with patch.object(
        cm, "get_embedding", new=AsyncMock(return_value=[0.1, 0.2, 0.3])
    ):
        yield


# ══════════════════════════════════════════════════════════════════════
# 1. _log
# ══════════════════════════════════════════════════════════════════════


def test_log_formats_message(capsys):
    _log("hello %s", "world")
    assert "[conversation_memory] hello world" in capsys.readouterr().out


def test_log_handles_bad_format_string(capsys):
    _log("bad %d", "notanumber")
    assert "bad" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════
# 2. _store_summary
# ══════════════════════════════════════════════════════════════════════


class _GetDB(FakeDB):
    """FakeDB that also supports db.get(Model, pk)."""

    def __init__(self, get_map=None, **kwargs):
        super().__init__(**kwargs)
        self._get_map = dict(get_map or {})

    async def get(self, model, key):
        return self._get_map.get((model, key))


@pytest.mark.asyncio
async def test_store_summary_missing_conversation():
    db = _GetDB()
    assert await _store_summary(db, uuid.uuid4(), "summary text") is False


@pytest.mark.asyncio
async def test_store_summary_persists_summary_and_embedding(some_embedding):
    conv = make_conversation()
    db = _GetDB(get_map={(Conversation, conv.id): conv})
    assert await _store_summary(db, conv.id, "A great summary") is True
    assert conv.summary == "A great summary"
    assert conv.summary_at is not None
    assert conv.summary_embedding == [0.1, 0.2, 0.3]
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_store_summary_without_embedding_still_stores_text(no_embedding):
    conv = make_conversation()
    db = _GetDB(get_map={(Conversation, conv.id): conv})
    assert await _store_summary(db, conv.id, "Plain summary") is True
    assert conv.summary == "Plain summary"
    assert conv.summary_embedding is None


@pytest.mark.asyncio
async def test_store_summary_exception_returns_false():
    class _Boom:
        async def get(self, model, key):
            raise RuntimeError("db down")

    assert await _store_summary(_Boom(), uuid.uuid4(), "s") is False


# ══════════════════════════════════════════════════════════════════════
# 3. maybe_summarize_conversation
# ══════════════════════════════════════════════════════════════════════


def _messages(conv_id, n):
    return [
        Message(id=uuid.uuid4(), conversation_id=conv_id, role="user", content=f"m{i}")
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_maybe_summarize_missing_conversation():
    db = _GetDB()
    assert await maybe_summarize_conversation(db, uuid.uuid4(), "m") is False


@pytest.mark.asyncio
async def test_maybe_summarize_skips_when_summary_recent():
    conv = make_conversation(
        summary="old summary", summary_at=datetime.utcnow()
    )
    db = _GetDB(get_map={(Conversation, conv.id): conv})
    assert await maybe_summarize_conversation(db, conv.id, "model") is False


@pytest.mark.asyncio
async def test_maybe_summarize_runs_after_cooldown_expired():
    conv = make_conversation(
        summary="old summary",
        summary_at=datetime.utcnow() - timedelta(seconds=10 * 3600),
    )
    msgs = _messages(conv.id, settings.CONVERSATION_SUMMARY_MIN_MESSAGES)
    db = _GetDB(
        get_map={(Conversation, conv.id): conv},
        select_results=[msgs],
    )
    with (
        patch.object(
            cm, "summarize_conversation", new=AsyncMock(return_value="fresh summary")
        ) as summ,
        patch.object(cm, "get_embedding", new=AsyncMock(return_value=None)),
    ):
        assert await maybe_summarize_conversation(db, conv.id, "model") is True
    summ.assert_awaited_once()
    # message_dicts were built with role/content
    passed_msgs = summ.call_args[0][0]
    assert passed_msgs[0] == {"role": "user", "content": "m0"}


@pytest.mark.asyncio
async def test_maybe_summarize_below_min_messages():
    conv = make_conversation()
    msgs = _messages(conv.id, settings.CONVERSATION_SUMMARY_MIN_MESSAGES - 1)
    db = _GetDB(get_map={(Conversation, conv.id): conv}, select_results=[msgs])
    with patch.object(
        cm, "summarize_conversation", new=AsyncMock()
    ) as summ:
        assert await maybe_summarize_conversation(db, conv.id, "model") is False
    summ.assert_not_awaited()


@pytest.mark.asyncio
async def test_maybe_summarize_custom_min_messages():
    conv = make_conversation()
    msgs = _messages(conv.id, 2)
    db = _GetDB(get_map={(Conversation, conv.id): conv}, select_results=[msgs])
    with patch.object(
        cm, "summarize_conversation", new=AsyncMock(return_value=None)
    ) as summ:
        assert await maybe_summarize_conversation(
            db, conv.id, "model", min_messages=2
        ) is False  # summarizer returned None
    summ.assert_awaited_once()


@pytest.mark.asyncio
async def test_maybe_summarize_empty_summary_string_not_stored():
    conv = make_conversation()
    msgs = _messages(conv.id, settings.CONVERSATION_SUMMARY_MIN_MESSAGES)
    db = _GetDB(get_map={(Conversation, conv.id): conv}, select_results=[msgs])
    with patch.object(cm, "summarize_conversation", new=AsyncMock(return_value="")):
        assert await maybe_summarize_conversation(db, conv.id, "model") is False
    assert conv.summary is None


@pytest.mark.asyncio
async def test_maybe_summarize_exception_swallowed():
    conv = make_conversation()
    msgs = _messages(conv.id, settings.CONVERSATION_SUMMARY_MIN_MESSAGES)
    db = _GetDB(get_map={(Conversation, conv.id): conv}, select_results=[msgs])
    with patch.object(
        cm, "summarize_conversation", new=AsyncMock(side_effect=RuntimeError("boom"))
    ):
        assert await maybe_summarize_conversation(db, conv.id, "model") is False


# ══════════════════════════════════════════════════════════════════════
# 4. get_recent_summaries
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_recent_summaries_maps_rows():
    now = datetime.utcnow()
    convs = [
        make_conversation(title=None, summary="s1", summary_at=now, model="m1"),
        make_conversation(title="Titled", summary="s2", summary_at=now, model=None),
    ]
    db = FakeDB(select_results=[convs])
    out = await get_recent_summaries(db, limit=10)
    assert len(out) == 2
    assert out[0]["title"] == "Untitled"  # None title → fallback
    assert out[0]["summary"] == "s1"
    assert out[0]["summary_at"] == int(now.timestamp())
    assert out[1]["title"] == "Titled"
    assert out[1]["model"] is None
    assert all("id" in entry for entry in out)


@pytest.mark.asyncio
async def test_get_recent_summaries_no_summary_at_maps_zero():
    conv = make_conversation(summary="s", summary_at=None)
    db = FakeDB(select_results=[[conv]])
    out = await get_recent_summaries(db)
    assert out[0]["summary_at"] == 0


@pytest.mark.asyncio
async def test_get_recent_summaries_excludes_conversation():
    convs = [make_conversation(summary="s", summary_at=datetime.utcnow())]
    db = FakeDB(select_results=[convs])
    exclude = uuid.uuid4()
    out = await get_recent_summaries(db, limit=5, exclude_conversation_id=exclude)
    assert len(out) == 1
    # the exclusion was compiled into the statement's WHERE clause
    assert "conversations.id !=" in str(db.executed[0])


@pytest.mark.asyncio
async def test_get_recent_summaries_exception_returns_empty():
    class _Boom(FakeDB):
        async def execute(self, stmt, *a, **kw):
            raise RuntimeError("db down")

    assert await get_recent_summaries(_Boom()) == []


# ══════════════════════════════════════════════════════════════════════
# 5. get_relevant_summaries (vector + fallback)
# ══════════════════════════════════════════════════════════════════════


def _vec_row(cid, title, summary, epoch, model, sim):
    return (str(cid), title, summary, epoch, model, sim)


@pytest.mark.asyncio
async def test_get_relevant_summaries_vector_path(some_embedding):
    c1, c2 = uuid.uuid4(), uuid.uuid4()
    rows = [
        _vec_row(c1, "T1", "s1", 1700000000, "m1", 0.9),
        _vec_row(c2, "T2", "s2", 1700000001, "m2", 0.5),
    ]
    db = FakeDB(text_rows=rows)
    out = await get_relevant_summaries(db, "query", top_k=5)
    assert len(out) == 2
    assert out[0]["id"] == str(c1)
    assert out[0]["score"] == 0.9
    assert out[0]["summary_at"] == 1700000000
    assert out[1]["title"] == "T2"


@pytest.mark.asyncio
async def test_get_relevant_summaries_excludes_current_conversation(some_embedding):
    current = uuid.uuid4()
    other = uuid.uuid4()
    rows = [
        _vec_row(current, "current", "own summary", 1, "m", 0.99),
        _vec_row(other, "other", "past summary", 2, "m", 0.8),
    ]
    db = FakeDB(text_rows=rows)
    out = await get_relevant_summaries(
        db, "query", exclude_conversation_id=current, top_k=3
    )
    assert [entry["id"] for entry in out] == [str(other)]


@pytest.mark.asyncio
async def test_get_relevant_summaries_respects_top_k(some_embedding):
    rows = [
        _vec_row(uuid.uuid4(), f"T{i}", f"s{i}", i, "m", 0.9 - i * 0.1)
        for i in range(5)
    ]
    db = FakeDB(text_rows=rows)
    out = await get_relevant_summaries(db, "query", top_k=2)
    assert len(out) == 2
    # highest similarity first
    assert out[0]["score"] > out[1]["score"]


@pytest.mark.asyncio
async def test_get_relevant_summaries_no_embedding_uses_keyword_fallback(no_embedding):
    keyword_result = [{"id": "x", "title": "t", "summary": "s"}]
    db = FakeDB()
    with patch.object(
        cm, "_keyword_summary_search", new=AsyncMock(return_value=keyword_result)
    ) as kw:
        out = await get_relevant_summaries(db, "query", top_k=3)
    assert out == keyword_result
    kw.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_relevant_summaries_vector_failure_falls_back(some_embedding):
    class _Boom(FakeDB):
        async def execute(self, stmt, *a, **kw):
            raise RuntimeError("pgvector missing")

    keyword_result = []
    with patch.object(
        cm, "_keyword_summary_search", new=AsyncMock(return_value=keyword_result)
    ) as kw:
        out = await get_relevant_summaries(_Boom(), "query", top_k=3)
    assert out == []
    kw.assert_awaited_once()


# ══════════════════════════════════════════════════════════════════════
# 6. _keyword_summary_search
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_keyword_search_no_significant_keywords():
    assert await _keyword_summary_search(FakeDB(), "a b cd") == []


@pytest.mark.asyncio
async def test_keyword_search_maps_conversations():
    now = datetime.utcnow()
    convs = [
        make_conversation(title="Alpha", summary="about paris", summary_at=now),
        make_conversation(title=None, summary="about rome", summary_at=now),
    ]
    db = FakeDB(select_results=[convs])
    out = await _keyword_summary_search(db, "paris rome", limit=5)
    assert len(out) == 2
    assert out[0]["title"] == "Alpha"
    assert out[0]["score"] == 0.0
    assert out[1]["title"] == "Untitled"


@pytest.mark.asyncio
async def test_keyword_search_respects_limit():
    convs = [make_conversation(summary=f"s{i}", summary_at=datetime.utcnow()) for i in range(5)]
    db = FakeDB(select_results=[convs])
    out = await _keyword_summary_search(db, "paris", limit=2)
    assert len(out) == 2


@pytest.mark.asyncio
async def test_keyword_search_excludes_conversation():
    convs = [make_conversation(summary="about paris", summary_at=datetime.utcnow())]
    db = FakeDB(select_results=[convs])
    exclude = uuid.uuid4()
    out = await _keyword_summary_search(
        db, "paris", exclude_conversation_id=exclude, limit=5
    )
    assert len(out) == 1
    assert "conversations.id !=" in str(db.executed[0])


@pytest.mark.asyncio
async def test_keyword_search_exception_returns_empty():
    class _Boom(FakeDB):
        async def execute(self, stmt, *a, **kw):
            raise RuntimeError("db down")

    assert await _keyword_summary_search(_Boom(), "paris") == []


# ══════════════════════════════════════════════════════════════════════
# 7. build_cross_session_context
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_build_context_empty_when_no_summaries():
    db = FakeDB()
    with (
        patch.object(cm, "get_relevant_summaries", new=AsyncMock(return_value=[])),
        patch.object(cm, "get_recent_summaries", new=AsyncMock(return_value=[])),
    ):
        ctx = await build_cross_session_context(db, "hello")
    assert ctx == ""


@pytest.mark.asyncio
async def test_build_context_uses_relevant_summaries():
    summaries = [
        {"title": "Trip", "summary": "We planned a trip to Japan.", "summary_at": 1},
        {"title": "Work", "summary": "Discussed the Acme project.", "summary_at": 2},
    ]
    db = FakeDB()
    with (
        patch.object(
            cm, "get_relevant_summaries", new=AsyncMock(return_value=summaries)
        ) as rel,
        patch.object(cm, "get_recent_summaries", new=AsyncMock()) as recent,
    ):
        ctx = await build_cross_session_context(db, "japan trip")
    assert "## Previous Conversations (for context)" in ctx
    assert "- [Trip]: We planned a trip to Japan." in ctx
    assert "- [Work]: Discussed the Acme project." in ctx
    rel.assert_awaited_once()
    recent.assert_not_awaited()  # relevant found → no recent fallback


@pytest.mark.asyncio
async def test_build_context_falls_back_to_recent_summaries():
    recent = [
        {"title": "Recent", "summary": "Recent chat about tea.", "summary_at": 3},
        {"title": "Older", "summary": "Older chat about coffee.", "summary_at": 2},
        {"title": "Oldest", "summary": "Should be dropped.", "summary_at": 1},
    ]
    db = FakeDB()
    with (
        patch.object(cm, "get_relevant_summaries", new=AsyncMock(return_value=[])),
        patch.object(cm, "get_recent_summaries", new=AsyncMock(return_value=recent)),
    ):
        ctx = await build_cross_session_context(db, "hello", max_summaries=2)
    assert "Recent chat about tea." in ctx
    assert "Older chat about coffee." in ctx
    assert "Should be dropped." not in ctx  # trimmed to max_summaries


@pytest.mark.asyncio
async def test_build_context_truncates_long_summaries():
    long_summary = " ".join(f"word{i}" for i in range(200))
    db = FakeDB()
    with (
        patch.object(
            cm,
            "get_relevant_summaries",
            new=AsyncMock(
                return_value=[{"title": "Long", "summary": long_summary}]
            ),
        ),
        patch.object(cm, "get_recent_summaries", new=AsyncMock()),
    ):
        ctx = await build_cross_session_context(db, "hello")
    assert ctx.count("word") == 80
    assert ctx.rstrip().endswith("...")


@pytest.mark.asyncio
async def test_build_context_converts_conversation_id_to_uuid():
    conv_id = uuid.uuid4()
    db = FakeDB()
    with (
        patch.object(
            cm,
            "get_relevant_summaries",
            new=AsyncMock(return_value=[]),
        ) as rel,
        patch.object(cm, "get_recent_summaries", new=AsyncMock(return_value=[])),
    ):
        await build_cross_session_context(db, "hello", conversation_id=conv_id)
    _, kwargs = rel.call_args
    assert kwargs["exclude_conversation_id"] == conv_id
