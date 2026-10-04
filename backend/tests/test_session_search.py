"""Tests for app/services/session_search.py (past-transcript keyword search).

What is tested:
  - _sanitize_tsquery: tokenization, minimum length, empty input, quoting.
  - search_past_messages: empty tsquery short-circuit, row → dict mapping,
    rank/epoch coercion, exclude_conversation_id (valid + invalid UUID) and
    its SQL bindparams, tsvector failure → ILIKE fallback hand-off.
  - _ilike_fallback: no significant keywords, row mapping with rank 0.0,
    exclude uuid handling, exception → [].
  - format_results_for_llm: empty result message, multi-result formatting,
    rank formatting, 300-char snippet truncation.

What is mocked:
  - The AsyncSession is replaced by FakeRowDB, which answers raw text()
    statements with canned rows — no real Postgres, no tsvector.
  - app.services.session_search._ilike_fallback is patched when testing
    the hand-off from the primary path.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import TextClause

import app.services.session_search as ss
from app.services.session_search import (
    _ilike_fallback,
    _sanitize_tsquery,
    format_results_for_llm,
    search_past_messages,
)


# ══════════════════════════════════════════════════════════════════════
# Test doubles
# ══════════════════════════════════════════════════════════════════════


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)

    def __iter__(self):
        return iter(self._rows)


class FakeRowDB:
    """AsyncSession double returning canned rows for raw SQL statements."""

    def __init__(self, rows=None, fail=False):
        self._rows = list(rows or [])
        self._fail = fail
        self.executed = []

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        assert isinstance(stmt, TextClause), f"unexpected statement {stmt!r}"
        if self._fail:
            raise RuntimeError("tsvector index missing")
        return _RowsResult(self._rows)


def _row(msg_id=None, conv_id=None, title="Trip", role="user", snippet="we went to Kyoto"):
    return (
        str(msg_id or uuid.uuid4()),
        str(conv_id or uuid.uuid4()),
        title,
        role,
        snippet,
        snippet + " (full)",
        0.42,
        1700000000,
    )


# ══════════════════════════════════════════════════════════════════════
# 1. _sanitize_tsquery
# ══════════════════════════════════════════════════════════════════════


def test_sanitize_tsquery_joins_with_or():
    assert _sanitize_tsquery("Kyoto trip") == "'kyoto' | 'trip'"


def test_log_handles_bad_format_string(capsys):
    # a bad %-format tuple must not raise — falls back to raw concatenation
    ss._log("bad %d", "oops")
    assert "bad" in capsys.readouterr().out


def test_sanitize_tsquery_drops_short_tokens():
    assert _sanitize_tsquery("a trip! Kyoto") == "'trip' | 'kyoto'"


def test_sanitize_tsquery_empty_and_punctuation():
    assert _sanitize_tsquery("") == ""
    assert _sanitize_tsquery("a! ? .") == ""


def test_sanitize_tsquery_lowercases():
    assert _sanitize_tsquery("PARIS") == "'paris'"


# ══════════════════════════════════════════════════════════════════════
# 2. search_past_messages
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_search_empty_tsquery_short_circuits():
    db = FakeRowDB()
    out = await search_past_messages(db, "a b!")
    assert out == []
    assert db.executed == []  # never touched the DB


@pytest.mark.asyncio
async def test_search_maps_rows_to_dicts():
    row = _row()
    db = FakeRowDB(rows=[row])
    out = await search_past_messages(db, "kyoto trip")
    assert len(out) == 1
    entry = out[0]
    assert entry["message_id"] == row[0]
    assert entry["conversation_id"] == row[1]
    assert entry["conversation_title"] == "Trip"
    assert entry["role"] == "user"
    assert entry["content_snippet"] == "we went to Kyoto"
    assert entry["content_full"] == "we went to Kyoto (full)"
    assert entry["rank"] == 0.42
    assert entry["created_at"] == 1700000000


@pytest.mark.asyncio
async def test_search_coerces_none_rank_and_epoch():
    row = _row()
    row = (*row[:6], None, None)
    db = FakeRowDB(rows=[row])
    out = await search_past_messages(db, "kyoto")
    assert out[0]["rank"] == 0.0
    assert out[0]["created_at"] == 0


@pytest.mark.asyncio
async def test_search_excludes_conversation_with_valid_uuid():
    exc = uuid.uuid4()
    row = _row(conv_id=exc)
    db = FakeRowDB(rows=[row])
    await search_past_messages(db, "kyoto", exclude_conversation_id=str(exc))
    stmt = db.executed[0]
    # the exclude clause was bound as a parameter
    assert "exc" in stmt._bindparams
    assert stmt._bindparams["exc"].value == str(exc)
    assert "m.conversation_id != CAST(:exc AS uuid)" in str(stmt)


@pytest.mark.asyncio
async def test_search_ignores_invalid_exclude_uuid():
    row = _row()
    db = FakeRowDB(rows=[row])
    await search_past_messages(db, "kyoto", exclude_conversation_id="not-a-uuid")
    stmt = db.executed[0]
    assert "exc" not in stmt._bindparams  # no exclude clause
    assert "m.conversation_id != CAST(:exc AS uuid)" not in str(stmt)


@pytest.mark.asyncio
async def test_search_falls_back_to_ilike_on_tsvector_failure():
    fallback_result = [{"message_id": "x"}]
    db = FakeRowDB(fail=True)
    with patch.object(
        ss, "_ilike_fallback", new=AsyncMock(return_value=fallback_result)
    ) as fb:
        out = await search_past_messages(db, "kyoto")
    assert out == fallback_result
    fb.assert_awaited_once_with(db, "kyoto", 10, None)


# ══════════════════════════════════════════════════════════════════════
# 3. _ilike_fallback
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ilike_fallback_no_significant_keywords():
    db = FakeRowDB()
    out = await _ilike_fallback(db, "a ab", 5, None)
    assert out == []
    assert db.executed == []


@pytest.mark.asyncio
async def test_ilike_fallback_maps_rows():
    # The fallback SQL hard-codes 0.0 as the rank column
    row = _row(title="Notes", snippet="clémence travels")
    row = (*row[:6], 0.0, row[7])
    db = FakeRowDB(rows=[row])
    out = await _ilike_fallback(db, "clémence travels", 5, None)
    assert out[0]["conversation_title"] == "Notes"
    assert out[0]["rank"] == 0.0  # fallback has no relevance rank
    assert out[0]["content_full"].endswith("(full)")


@pytest.mark.asyncio
async def test_ilike_fallback_coerces_none_epoch():
    row = _row()
    row = (*row[:7], None)
    db = FakeRowDB(rows=[row])
    out = await _ilike_fallback(db, "kyoto", 5, None)
    assert out[0]["created_at"] == 0


@pytest.mark.asyncio
async def test_ilike_fallback_with_valid_exclude_uuid():
    exc = uuid.uuid4()
    db = FakeRowDB(rows=[])
    await _ilike_fallback(db, "kyoto", 5, str(exc))
    stmt = db.executed[0]
    assert "exc" in stmt._bindparams
    assert stmt._bindparams["exc"].value == str(exc)


@pytest.mark.asyncio
async def test_ilike_fallback_ignores_invalid_exclude_uuid():
    db = FakeRowDB(rows=[])
    out = await _ilike_fallback(db, "kyoto", 5, "not-a-uuid")
    assert out == []
    stmt = db.executed[0]
    assert "exc" not in stmt._bindparams  # no exclude clause
    assert "m.conversation_id != CAST(:exc AS uuid)" not in str(stmt)


@pytest.mark.asyncio
async def test_ilike_fallback_exception_returns_empty():
    db = FakeRowDB(fail=True)
    assert await _ilike_fallback(db, "kyoto", 5, None) == []


# ══════════════════════════════════════════════════════════════════════
# 4. format_results_for_llm
# ══════════════════════════════════════════════════════════════════════


def test_format_results_empty():
    assert (
        format_results_for_llm([], "query")
        == "No past conversations found matching 'query'."
    )


def test_format_results_single():
    results = [
        {
            "content_snippet": "we went to Kyoto",
            "conversation_title": "Trip",
            "role": "user",
            "rank": 0.424,
        }
    ]
    out = format_results_for_llm(results, "kyoto")
    assert out.startswith("Found 1 matching past message(s):")
    assert "[1] From 'Trip' (user, rank=0.424):" in out
    assert "we went to Kyoto" in out


def test_format_results_multiple_and_rank_formatting():
    results = [
        {
            "content_snippet": "first",
            "conversation_title": "A",
            "role": "assistant",
            "rank": 0.5,
        },
        {
            "content_snippet": "second",
            "conversation_title": "B",
            "role": "user",
            "rank": 0.1,
        },
    ]
    out = format_results_for_llm(results, "q")
    assert "[1] From 'A'" in out
    assert "[2] From 'B'" in out
    assert "rank=0.500" in out
    assert "rank=0.100" in out


def test_format_results_truncates_long_snippet():
    results = [
        {
            "content_snippet": "x" * 400,
            "conversation_title": "T",
            "role": "user",
            "rank": 0.9,
        }
    ]
    out = format_results_for_llm(results, "q")
    snippet_line = next(line for line in out.splitlines() if line.strip().startswith("x"))
    assert snippet_line.strip() == "x" * 300 + "…"
