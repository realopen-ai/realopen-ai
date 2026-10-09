"""Tests for app/services/memory_extractor.py.

What is tested:
  - LLM output cleaning: _strip_think_tags, _extract_json_list (plain JSON,
    markdown fences, <think> blocks, bracket slicing, trailing-comma repair,
    garbage input).
  - Message helpers: _message_text / _message_role for objects and dicts.
  - _clean_memory_value (punctuation, leading articles, length cap, URL/@
    rejection).
  - _fallback_memory_candidates (name / call me / lives in / prefers /
    relationship / travel-goal regexes, dedup, cap of 2).
  - extract_and_store orchestration: model/URL guard, message-count guard,
    media stripping, LLM round-trip parsing (valid, malformed, retry,
    HTTP failure), fact dedup tiers, storage, audit-trigger threshold,
    fallback facts appended.
  - audit_memories: no model, empty store, fingerprint short-circuit, LLM
    failure, bad JSON, successful consolidation (rewrite/delete/category),
    unknown ids, unsafe-removal refusal.
  - Watermark helpers: get_messages_since_watermark / update_watermark.
  - maybe_run_memory_extraction: disabled interval, below interval, due
    (watermark advanced + job enqueued), failure path.

What is mocked:
  - httpx.AsyncClient is replaced by a fake client class — no request ever
    reaches OLLAMA_BASE_URL (http://localhost:11434 is NOT running here).
  - app.services.memory_extractor.async_session_factory is patched to hand
    out FakeDB sessions (no real Postgres).
  - app.services.memory.get_embedding is patched (AsyncMock → None) so the
    vector dedup tier is inert; app.services.embeddings.get_embedding is
    patched for the audit rewrite path.
  - settings.resolve_model / interval / audit interval via monkeypatch.
"""

import json
import uuid
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import Select, TextClause

import app.services.memory as memory_service
import app.services.memory_extractor as me
from app.config import settings
from app.db.models import Conversation, Memory, Message
from app.services.memory_extractor import (
    _clean_memory_value,
    _extract_json_list,
    _fallback_memory_candidates,
    _message_role,
    _message_text,
    _strip_think_tags,
    audit_memories,
    extract_and_store,
    get_messages_since_watermark,
    maybe_run_memory_extraction,
    update_watermark,
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

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class FakeDB:
    """Minimal AsyncSession double for select()/get()/add()/flush().

    text_rows: callable(TextClause) -> list of row tuples for raw SQL
    (used by the vector-dedup tier, which issues text() statements).
    """

    def __init__(self, select_results=None, get_map=None, text_rows=None):
        self._select_results = [list(r) for r in (select_results or [])]
        self._get_map = dict(get_map or {})
        self._text_rows = text_rows or (lambda stmt: [])
        self.added = []
        self.deleted = []
        self.flush_count = 0
        self.commit_count = 0

    async def execute(self, stmt, *args, **kwargs):
        if isinstance(stmt, TextClause):
            return list(self._text_rows(stmt))
        assert isinstance(stmt, Select), f"unexpected statement {stmt!r}"
        result = self._select_results.pop(0) if self._select_results else []
        return _ScalarsResult(result)

    async def get(self, model, key):
        return self._get_map.get((model, key))

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        self.flush_count += 1

    async def refresh(self, obj):
        pass

    async def commit(self):
        self.commit_count += 1


class FakeResponse:
    def __init__(self, payload=None, content=None, raise_on_status=False):
        self._payload = payload if payload is not None else {}
        self._content = content
        self._raise = raise_on_status
        self.text = json.dumps(self._payload)

    def raise_for_status(self):
        if self._raise:
            raise RuntimeError("simulated HTTP 500")

    def json(self):
        if self._content is not None:
            return {"message": {"content": self._content}}
        return self._payload


class FakeAsyncClient:
    """Records post() calls and replays queued responses/exceptions."""

    calls: list = []
    queue: list = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        FakeAsyncClient.calls.append({"url": url, "json": json})
        if not FakeAsyncClient.queue:
            raise AssertionError("unexpected LLM call: no queued response")
        item = FakeAsyncClient.queue.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture
def fake_llm():
    """Patch httpx.AsyncClient inside memory_extractor; no real network."""
    FakeAsyncClient.calls = []
    FakeAsyncClient.queue = []
    with patch.object(me.httpx, "AsyncClient", FakeAsyncClient):
        yield FakeAsyncClient


@pytest.fixture
def no_model(monkeypatch):
    # settings is a pydantic BaseSettings — patch resolve_model on the class
    monkeypatch.setattr(type(settings), "resolve_model", lambda self, role: "")


@pytest.fixture
def some_model(monkeypatch):
    monkeypatch.setattr(
        type(settings), "resolve_model", lambda self, role: "qwen3:4b-test"
    )


def _fake_session_factory(dbs):
    """Return an async_session_factory double handing out the given dbs."""
    dbs = list(dbs)
    calls = []

    class _Ctx:
        async def __aenter__(self):
            db = dbs.pop(0) if dbs else FakeDB()
            calls.append(db)
            return db

        async def __aexit__(self, *exc):
            return False

    factory = lambda: _Ctx()  # noqa: E731
    factory.calls = calls
    return factory


def _msg(role, content):
    return {"role": role, "content": content}


# ══════════════════════════════════════════════════════════════════════
# 1. LLM output cleaning
# ══════════════════════════════════════════════════════════════════════


def test_strip_think_tags_removes_think_blocks():
    raw = "<think>reasoning here</think>[{\"text\": \"a\"}]"
    assert _strip_think_tags(raw) == "[{\"text\": \"a\"}]"


def test_strip_think_tags_removes_thinking_blocks():
    raw = "<thinking>chain</thinking>payload"
    assert _strip_think_tags(raw) == "payload"


def test_strip_think_tags_plain_text_untouched():
    assert _strip_think_tags("  hello  ") == "hello"


def test_extract_json_list_direct():
    assert _extract_json_list('[{"text": "a"}]') == [{"text": "a"}]


def test_extract_json_list_empty_string():
    assert _extract_json_list("") is None
    assert _extract_json_list(None) is None


def test_extract_json_list_with_think_tags():
    raw = '<think>let me think</think>\n[{"text": "User likes tea"}]'
    assert _extract_json_list(raw) == [{"text": "User likes tea"}]


def test_extract_json_list_markdown_fenced():
    raw = "```json\n[{\"text\": \"a\"}, {\"text\": \"b\"}]\n```"
    assert _extract_json_list(raw) == [{"text": "a"}, {"text": "b"}]


def test_extract_json_list_bare_fence():
    raw = "```\n[\"plain\"]\n```"
    assert _extract_json_list(raw) == ["plain"]


def test_extract_json_list_bracket_slice_from_commentary():
    raw = 'Sure! Here is the result: [{"text": "a"}] hope that helps'
    assert _extract_json_list(raw) == [{"text": "a"}]


def test_extract_json_list_repairs_trailing_commas():
    raw = '[{"text": "a",},]'
    assert _extract_json_list(raw) == [{"text": "a"}]


def test_extract_json_list_garbage_returns_none():
    assert _extract_json_list("no json here at all") is None
    assert _extract_json_list("[unclosed") is None


def test_extract_json_list_non_list_json_returns_none():
    # A bare JSON object (not a list) is not accepted
    assert _extract_json_list('{"text": "a"}') is None


def test_extract_json_list_empty_fenced_block():
    # fence matched but its body is empty → nothing parseable
    assert _extract_json_list("```json\n```") is None
    assert _extract_json_list("```\n```") is None


# ══════════════════════════════════════════════════════════════════════
# 2. Message helpers + cleaning
# ══════════════════════════════════════════════════════════════════════


def test_message_text_from_dict_and_object():
    assert _message_text({"content": " hi "}) == "hi"
    obj = SimpleNamespace(content="hello")
    assert _message_text(obj) == "hello"


def test_message_text_from_content_blocks():
    blocks = [{"type": "text", "text": "part one"}, {"type": "text", "content": "two"}]
    assert _message_text({"content": blocks}) == "part one two"


def test_message_text_from_mixed_blocks():
    blocks = ["raw-string", {"type": "text", "text": "dict"}]
    assert _message_text({"content": blocks}) == "raw-string dict"


def test_message_text_missing_content():
    assert _message_text({"role": "user"}) == ""
    assert _message_text(SimpleNamespace()) == ""


def test_message_role_variants():
    assert _message_role({"role": "USER"}) == "user"
    assert _message_role(SimpleNamespace(role="Assistant")) == "assistant"
    assert _message_role({}) == ""
    assert _message_role(SimpleNamespace()) == ""


def test_clean_memory_value_strips_articles_and_punctuation():
    assert _clean_memory_value("  The Sam . ") == "Sam"


def test_clean_memory_value_collapses_whitespace():
    assert _clean_memory_value("Sam   Smith") == "Sam Smith"


def test_clean_memory_value_rejects_urls_and_emails():
    assert _clean_memory_value("see https://example.com") == ""
    assert _clean_memory_value("mail me at sam@x.io") == ""


def test_clean_memory_value_rejects_too_long():
    assert _clean_memory_value("word " * 40) == ""


def test_clean_memory_value_empty():
    assert _clean_memory_value("") == ""
    assert _clean_memory_value(None) == ""


# ══════════════════════════════════════════════════════════════════════
# 3. Fallback regex extraction
# ══════════════════════════════════════════════════════════════════════


def test_fallback_extracts_name():
    msgs = [_msg("user", "Hi, my name is Clémence!")]
    out = _fallback_memory_candidates(msgs)
    assert out == [{"text": "User's name is Clémence", "category": "identity"}]


def test_fallback_extracts_call_me():
    out = _fallback_memory_candidates([_msg("user", "Please call me Sam.")])
    assert out == [{"text": "User wants to be called Sam", "category": "identity"}]


def test_fallback_extracts_lives_in():
    out = _fallback_memory_candidates([_msg("user", "I live in Cape Town.")])
    assert out == [{"text": "User lives in Cape Town", "category": "identity"}]


def test_fallback_extracts_preference():
    out = _fallback_memory_candidates([_msg("user", "I prefer strong coffee")])
    assert out == [{"text": "User prefers strong coffee", "category": "preference"}]


def test_fallback_extracts_relationship():
    out = _fallback_memory_candidates([_msg("user", "My girlfriend is Clémence")])
    assert out == [{"text": "User's girlfriend is Clémence", "category": "fact"}]


def test_fallback_extracts_travel_goal():
    out = _fallback_memory_candidates([_msg("user", "I want to go to Japan in April")])
    assert out[0]["category"] == "goal"
    assert "Japan" in out[0]["text"]


def test_fallback_ignores_assistant_messages():
    out = _fallback_memory_candidates([_msg("assistant", "my name is Bob")])
    assert out == []


def test_fallback_skips_user_message_with_no_text():
    # whitespace-only user content is dropped before the regexes run
    msgs = [{"role": "user", "content": "   "}, _msg("user", "my name is Sam")]
    out = _fallback_memory_candidates(msgs)
    assert out == [{"text": "User's name is Sam", "category": "identity"}]


def test_fallback_rejects_url_like_place():
    # "I live in <url>" matches the place regex but the cleaner rejects URLs
    out = _fallback_memory_candidates([_msg("user", "I live in https://mysite")])
    assert out == []


def test_fallback_dedups_identical_candidates():
    out = _fallback_memory_candidates(
        [_msg("user", "my name is Sam"), _msg("user", "My name is Sam.")]
    )
    assert len(out) == 1


def test_fallback_caps_at_two_candidates():
    msgs = [
        _msg("user", "my name is Sam"),
        _msg("user", "I live in Paris"),
        _msg("user", "I prefer tea"),
    ]
    assert len(_fallback_memory_candidates(msgs)) == 2


def test_fallback_no_durable_facts():
    assert _fallback_memory_candidates([_msg("user", "what is the weather?")]) == []


# ══════════════════════════════════════════════════════════════════════
# 4. extract_and_store (httpx + DB mocked)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_extract_skips_when_no_model(no_model):
    assert await extract_and_store([_msg("user", "hi"), _msg("assistant", "hey")]) == 0


@pytest.mark.asyncio
async def test_extract_skips_when_too_few_messages(some_model):
    assert await extract_and_store([_msg("user", "hi")]) == 0


@pytest.mark.asyncio
async def test_extract_skips_when_no_user_or_assistant_content(some_model):
    msgs = [_msg("system", "sysprompt"), _msg("user", "")]
    assert await extract_and_store(msgs) == 0


@pytest.mark.asyncio
async def test_extract_skips_media_only_messages(some_model, fake_llm):
    # BOTH turns are image-only → nothing survives stripping → no LLM call
    msgs = [
        _msg("user", [{"type": "image_url", "image_url": {"url": "x"}}]),
        _msg("assistant", [{"type": "image_url", "image_url": {"url": "y"}}]),
    ]
    factory = _fake_session_factory([])
    with patch.object(me, "async_session_factory", factory):
        added = await extract_and_store(msgs)
    assert added == 0
    assert fake_llm.calls == []  # never attempted a real LLM call
    assert factory.calls == []  # never opened a DB session


@pytest.mark.asyncio
async def test_extract_keeps_text_blocks_from_mixed_media_content(some_model, fake_llm):
    fake_llm.queue = [FakeResponse(content="[]")]
    factory = _fake_session_factory([])
    msgs = [
        _msg(
            "user",
            [
                {"type": "text", "text": "hello there"},
                {"type": "image_url", "image_url": {"url": "x"}},
            ],
        ),
        _msg("assistant", "hi"),
    ]
    with patch.object(me, "async_session_factory", factory):
        added = await extract_and_store(msgs)
    assert added == 0
    payload = fake_llm.calls[0]["json"]["messages"][1]["content"]
    # the text block survives in the transcript; the image block is dropped
    assert "hello there" in payload
    assert "image_url" not in payload


@pytest.mark.asyncio
async def test_extract_stores_llm_facts(some_model, fake_llm):
    fake_llm.queue = [FakeResponse(content='[{"text": "User likes matcha", "category": "preference"}]')]
    db = FakeDB(select_results=[[]])  # no existing memories, no counter
    factory = _fake_session_factory([db])
    with (
        patch.object(me, "async_session_factory", factory),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)),
    ):
        added = await extract_and_store(
            [_msg("user", "So, about my beverage habits"), _msg("assistant", "Noted!")],
            conversation_id=str(uuid.uuid4()),
        )
    assert added == 1
    # One LLM call to the chat endpoint with a flattened transcript
    assert len(fake_llm.calls) == 1
    call = fake_llm.calls[0]
    assert call["url"].endswith("/api/chat")
    assert call["json"]["model"] == "qwen3:4b-test"
    assert call["json"]["stream"] is False
    user_msg = call["json"]["messages"][1]
    assert "Conversation to analyze:" in user_msg["content"]
    assert "USER: So, about my beverage habits" in user_msg["content"]
    # The memory was persisted via the manager
    mems = [m for m in db.added if isinstance(m, Memory)]
    assert len(mems) == 1
    assert mems[0].text == "User likes matcha"
    assert mems[0].category == "preference"
    assert mems[0].source == "auto"
    assert db.commit_count >= 1


@pytest.mark.asyncio
async def test_extract_retries_then_succeeds(some_model, fake_llm):
    fake_llm.queue = [
        FakeResponse(content="utter garbage, not json"),
        FakeResponse(content='[{"text": "User owns a cat"}]'),
    ]
    db = FakeDB(select_results=[[]])
    factory = _fake_session_factory([db])
    with (
        patch.object(me, "async_session_factory", factory),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)),
    ):
        added = await extract_and_store(
            [_msg("user", "I own a cat"), _msg("assistant", "cool")]
        )
    assert len(fake_llm.calls) == 2  # retried once
    assert added == 1


@pytest.mark.asyncio
async def test_extract_llm_http_error_falls_back_to_regex(some_model, fake_llm):
    fake_llm.queue = [RuntimeError("connection refused"), RuntimeError("timeout")]
    db = FakeDB(select_results=[[]])
    factory = _fake_session_factory([db])
    with (
        patch.object(me, "async_session_factory", factory),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)),
    ):
        added = await extract_and_store(
            [_msg("user", "my name is Sam"), _msg("assistant", "hi Sam")]
        )
    assert added == 1  # fallback fact survived the LLM outage
    mems = [m for m in db.added if isinstance(m, Memory)]
    assert mems[0].text == "User's name is Sam"
    assert mems[0].category == "identity"


@pytest.mark.asyncio
async def test_extract_llm_non_200_status(some_model, fake_llm):
    fake_llm.queue = [FakeResponse(raise_on_status=True), FakeResponse(raise_on_status=True)]
    factory = _fake_session_factory([])
    with patch.object(me, "async_session_factory", factory):
        added = await extract_and_store(
            [_msg("user", "tell me about pyramids"), _msg("assistant", "sure")]
        )
    assert added == 0


@pytest.mark.asyncio
async def test_extract_llm_timeout_falls_back_to_regex(some_model, fake_llm):
    fake_llm.queue = [httpx.ReadTimeout("read timed out")] * 2
    db = FakeDB(select_results=[[]])
    factory = _fake_session_factory([db])
    with (
        patch.object(me, "async_session_factory", factory),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)),
    ):
        added = await extract_and_store(
            [_msg("user", "my name is Sam"), _msg("assistant", "hi Sam")]
        )
    assert added == 1  # regex fallback fact survived the timeouts
    assert len(fake_llm.calls) == 2  # one retry, then gave up


@pytest.mark.asyncio
async def test_extract_no_facts_no_db_session(some_model, fake_llm):
    fake_llm.queue = [FakeResponse(content="[]")]
    factory = _fake_session_factory([])
    with patch.object(me, "async_session_factory", factory):
        added = await extract_and_store(
            [_msg("user", "hello"), _msg("assistant", "hi")]
        )
    assert added == 0
    assert factory.calls == []  # never opened a DB session


@pytest.mark.asyncio
async def test_extract_skips_short_and_non_string_facts(some_model, fake_llm):
    fake_llm.queue = [
        FakeResponse(content='["abc", 42, {"text": "User enjoys long walks"}]')
    ]
    db = FakeDB(select_results=[[]])
    factory = _fake_session_factory([db])
    with (
        patch.object(me, "async_session_factory", factory),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)),
    ):
        added = await extract_and_store(
            [_msg("user", "walks"), _msg("assistant", "ok")]
        )
    assert added == 1
    mems = [m for m in db.added if isinstance(m, Memory)]
    assert mems[0].text == "User enjoys long walks"


@pytest.mark.asyncio
async def test_extract_dedups_against_existing_exact_match(some_model, fake_llm):
    fake_llm.queue = [FakeResponse(content='[{"text": "User likes matcha"}]')]
    existing = Memory(
        id=uuid.uuid4(),
        text="User likes matcha",
        category="preference",
        source="auto",
    )
    db = FakeDB(select_results=[[existing]])  # get_all_memories → existing
    factory = _fake_session_factory([db])
    with (
        patch.object(me, "async_session_factory", factory),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)),
    ):
        added = await extract_and_store(
            [_msg("user", "regarding my preferences"), _msg("assistant", "noted")]
        )
    assert added == 0
    assert [m for m in db.added if isinstance(m, Memory)] == []


@pytest.mark.asyncio
async def test_extract_dedups_jaccard_tier(some_model, fake_llm):
    fake_llm.queue = [FakeResponse(content='[{"text": "User enjoys long walks"}]')]
    existing = Memory(
        id=uuid.uuid4(),
        text="User enjoys long walks outside",
        category="fact",
        source="auto",
    )
    db = FakeDB(select_results=[[existing]])
    factory = _fake_session_factory([db])
    with (
        patch.object(me, "async_session_factory", factory),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)),
    ):
        added = await extract_and_store(
            [_msg("user", "walking"), _msg("assistant", "ok")]
        )
    assert added == 0


@pytest.mark.asyncio
async def test_extract_dedups_vector_tier_skips_paraphrase(some_model, fake_llm):
    # LLM fact is a paraphrase of an existing memory → the vector tier
    # (find_similar_by_vector + content guard) drops it.
    fake_llm.queue = [FakeResponse(content='[{"text": "User\'s name is Sam"}]')]
    existing = Memory(
        id=uuid.uuid4(),
        text="The user is called Sam",
        category="fact",
        source="auto",
    )
    # Selects: get_all_memories → [existing], then get_memory_by_id → [existing]
    db = FakeDB(
        select_results=[[existing], [existing]],
        text_rows=lambda stmt: [(str(existing.id), 0.91)]
        if "LIMIT 5" in str(stmt)
        else [],
    )
    factory = _fake_session_factory([db])
    with (
        patch.object(me, "async_session_factory", factory),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=[0.2])),
    ):
        added = await extract_and_store(
            [_msg("user", "names"), _msg("assistant", "ok")]
        )
    assert added == 0
    assert [m for m in db.added if isinstance(m, Memory)] == []


@pytest.mark.asyncio
async def test_extract_vector_dedup_error_falls_back_to_text_tiers(some_model, fake_llm):
    # The vector dedup call raises → the extractor falls back to the text
    # tiers, where the exact match still catches the duplicate.
    fake_llm.queue = [FakeResponse(content='[{"text": "User owns a cat"}]')]
    existing = Memory(
        id=uuid.uuid4(),
        text="User owns a cat",
        category="fact",
        source="auto",
    )
    db = FakeDB(select_results=[[existing]])
    factory = _fake_session_factory([db])
    with (
        patch.object(me, "async_session_factory", factory),
        patch.object(
            memory_service,
            "get_embedding",
            new=AsyncMock(side_effect=RuntimeError("embeddings down")),
        ),
    ):
        added = await extract_and_store(
            [_msg("user", "pets"), _msg("assistant", "ok")]
        )
    assert added == 0


@pytest.mark.asyncio
async def test_extract_db_session_failure_returns_zero(some_model, fake_llm):
    fake_llm.queue = [FakeResponse(content='[{"text": "User owns a cat"}]')]

    def exploding_factory():
        raise RuntimeError("db unavailable")

    with patch.object(me, "async_session_factory", exploding_factory):
        added = await extract_and_store(
            [_msg("user", "cats"), _msg("assistant", "ok")]
        )
    assert added == 0  # outer except swallows the failure


@pytest.mark.asyncio
async def test_extract_add_failure_is_swallowed(some_model, fake_llm):
    fake_llm.queue = [FakeResponse(content='[{"text": "User owns a cat"}]')]
    db = FakeDB(select_results=[[]])
    db.flush_count = 0

    async def boom_flush():
        raise RuntimeError("db write failed")

    db.flush = boom_flush
    factory = _fake_session_factory([db])
    with (
        patch.object(me, "async_session_factory", factory),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)),
    ):
        added = await extract_and_store(
            [_msg("user", "cat"), _msg("assistant", "ok")]
        )
    assert added == 0  # error logged, never raised


@pytest.mark.asyncio
async def test_extract_triggers_audit_at_threshold(some_model, fake_llm, monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_AUDIT_INTERVAL", 5)
    fake_llm.queue = [FakeResponse(content='[{"text": "User owns a cat"}]')]
    # Selects: get_all_memories → [], _get_app_state(counter) → "4" (4+1 >= 5)
    db = FakeDB(select_results=[[], ["4"]])
    factory = _fake_session_factory([db])
    audit_mock = AsyncMock(return_value={"before": 1, "after": 1})
    with (
        patch.object(me, "async_session_factory", factory),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)),
        patch.object(me, "audit_memories", audit_mock),
    ):
        added = await extract_and_store(
            [_msg("user", "cat"), _msg("assistant", "ok")]
        )
    assert added == 1
    audit_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_extract_below_audit_threshold_no_audit(some_model, fake_llm):
    fake_llm.queue = [FakeResponse(content='[{"text": "User owns a cat"}]')]
    db = FakeDB(select_results=[[], ["0"]])  # counter 0 → 1 < 5
    factory = _fake_session_factory([db])
    audit_mock = AsyncMock()
    with (
        patch.object(me, "async_session_factory", factory),
        patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)),
        patch.object(me, "audit_memories", audit_mock),
    ):
        added = await extract_and_store(
            [_msg("user", "cat"), _msg("assistant", "ok")]
        )
    assert added == 1
    audit_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_extract_caps_transcript_turn_length(some_model, fake_llm):
    long_text = "x" * 3000
    fake_llm.queue = [FakeResponse(content="[]")]
    factory = _fake_session_factory([])
    with patch.object(me, "async_session_factory", factory):
        await extract_and_store(
            [_msg("user", long_text), _msg("assistant", long_text)]
        )
    payload = fake_llm.calls[0]["json"]["messages"][1]["content"]
    assert "…" in payload
    assert len(payload) < 3000  # truncated for the 4B model


# ══════════════════════════════════════════════════════════════════════
# 5. audit_memories
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_audit_no_model_returns_error(no_model):
    assert await audit_memories() == {"error": "no_model"}


@pytest.mark.asyncio
async def test_audit_empty_store(some_model):
    factory = _fake_session_factory([FakeDB(select_results=[[]])])
    with patch.object(me, "async_session_factory", factory):
        result = await audit_memories()
    assert result == {"before": 0, "after": 0}


def _existing(n=2):
    return [
        Memory(
            id=uuid.uuid4(),
            text=f"Memory number {i}",
            category="fact",
            source="auto",
            created_at=datetime.utcnow(),
        )
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_audit_fingerprint_short_circuit(some_model, fake_llm):
    mems = _existing(2)
    fp = memory_service._fingerprint_memories(mems)
    # Selects: get_all_memories → mems, _get_audit_fingerprint → same fp
    db = FakeDB(select_results=[mems, [fp]])
    factory = _fake_session_factory([db])
    with patch.object(me, "async_session_factory", factory):
        result = await audit_memories()
    assert result == {"before": 2, "after": 2, "already_tidy": True}
    assert fake_llm.calls == []  # LLM skipped entirely


@pytest.mark.asyncio
async def test_audit_llm_failure(some_model, fake_llm):
    mems = _existing(2)
    db = FakeDB(select_results=[mems, [None]])  # no fingerprint yet
    factory = _fake_session_factory([db])
    fake_llm.queue = [RuntimeError("ollama down")]
    with patch.object(me, "async_session_factory", factory):
        result = await audit_memories()
    assert result == {"before": 2, "after": 2, "error": "llm_failed"}


@pytest.mark.asyncio
async def test_audit_bad_json_response(some_model, fake_llm):
    mems = _existing(2)
    db = FakeDB(select_results=[mems, [None]])
    factory = _fake_session_factory([db])
    fake_llm.queue = [FakeResponse(content="I cannot do that, Dave")]
    with patch.object(me, "async_session_factory", factory):
        result = await audit_memories()
    assert result == {"before": 2, "after": 2, "error": "bad_json"}


@pytest.mark.asyncio
async def test_audit_consolidates_and_deletes(some_model, fake_llm):
    keep, drop = _existing(2)
    keep.text = "User enjoys tea"
    drop.text = "user enjoys tea"  # near-duplicate to be removed
    db = FakeDB(select_results=[[keep, drop], [None]])
    factory = _fake_session_factory([db])
    fake_llm.queue = [
        FakeResponse(
            content=json.dumps([{"id": str(keep.id), "text": "User enjoys tea"}])
        )
    ]
    with (
        patch.object(me, "async_session_factory", factory),
        patch("app.services.embeddings.get_embedding", new=AsyncMock(return_value=None)),
    ):
        result = await audit_memories()
    assert result == {"before": 2, "after": 1}
    assert db.deleted == [drop]  # the losing duplicate was deleted
    assert db.commit_count == 1
    # A new fingerprint was persisted
    fps = [a for a in db.added if getattr(a, "key", None) == "memory.audit_fingerprint"]
    assert len(fps) == 1


@pytest.mark.asyncio
async def test_audit_rewrites_text_and_regenerates_embedding(some_model, fake_llm):
    mem = _existing(1)[0]
    db = FakeDB(select_results=[[mem], [None]])
    factory = _fake_session_factory([db])
    fake_llm.queue = [
        FakeResponse(
            content=json.dumps(
                [{"id": str(mem.id), "text": "Rewritten text", "category": "goal"}]
            )
        )
    ]
    with (
        patch.object(me, "async_session_factory", factory),
        patch(
            "app.services.embeddings.get_embedding",
            new=AsyncMock(return_value=[0.5]),
        ) as embed_mock,
    ):
        result = await audit_memories()
    assert result == {"before": 1, "after": 1}
    assert mem.text == "Rewritten text"
    assert mem.category == "goal"
    embed_mock.assert_awaited_once_with("Rewritten text")
    assert db.deleted == []


@pytest.mark.asyncio
async def test_audit_unknown_id_skipped(some_model, fake_llm):
    mem = _existing(1)[0]
    db = FakeDB(select_results=[[mem], [None]])
    factory = _fake_session_factory([db])
    fake_llm.queue = [
        FakeResponse(
            content=json.dumps([{"id": str(uuid.uuid4()), "text": "ghost"}])
        )
    ]
    with patch.object(me, "async_session_factory", factory):
        result = await audit_memories()
    # unknown id → not kept → memory deleted; small store → no unsafe refusal
    assert result == {"before": 1, "after": 0}
    assert db.deleted == [mem]


@pytest.mark.asyncio
async def test_audit_ignores_non_dict_items(some_model, fake_llm):
    keep, drop = _existing(2)
    db = FakeDB(select_results=[[keep, drop], [None]])
    factory = _fake_session_factory([db])
    fake_llm.queue = [FakeResponse(content='["just-a-string"]')]
    with patch.object(me, "async_session_factory", factory):
        result = await audit_memories()
    # the string item is not a dict → ignored → both memories removed
    assert result == {"before": 2, "after": 0}
    assert set(db.deleted) == {keep, drop}


@pytest.mark.asyncio
async def test_audit_skips_items_with_empty_text(some_model, fake_llm):
    keep, drop = _existing(2)
    db = FakeDB(select_results=[[keep, drop], [None]])
    factory = _fake_session_factory([db])
    fake_llm.queue = [
        FakeResponse(
            content=json.dumps(
                [
                    {"id": str(keep.id), "text": "   "},
                    {"id": str(drop.id), "text": "Kept text"},
                ]
            )
        )
    ]
    with (
        patch.object(me, "async_session_factory", factory),
        # patch the embedding call made by the rewrite path — otherwise a
        # real httpx request to OLLAMA_BASE_URL would be attempted
        patch(
            "app.services.embeddings.get_embedding",
            new=AsyncMock(return_value=[0.4]),
        ),
    ):
        result = await audit_memories()
    assert result == {"before": 2, "after": 1}
    assert db.deleted == [keep]
    assert drop.text == "Kept text"


@pytest.mark.asyncio
async def test_audit_db_failure_returns_error(some_model):
    def exploding_factory():
        raise RuntimeError("db unavailable")

    with patch.object(me, "async_session_factory", exploding_factory):
        result = await audit_memories()
    assert result == {"error": "db unavailable"}


@pytest.mark.asyncio
async def test_audit_refuses_unsafe_removal(some_model, fake_llm):
    mems = _existing(10)
    db = FakeDB(select_results=[mems, [None]])
    factory = _fake_session_factory([db])
    fake_llm.queue = [
        FakeResponse(
            content=json.dumps(
                [{"id": str(mems[0].id), "text": mems[0].text}]
            )
        )
    ]
    with patch.object(me, "async_session_factory", factory):
        result = await audit_memories()
    # 10 → 1 would remove >50% → refused, nothing deleted
    assert result == {"before": 10, "after": 10, "error": "unsafe_removal"}
    assert db.deleted == []


# ══════════════════════════════════════════════════════════════════════
# 6. Watermark helpers
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_messages_since_watermark_invalid_uuid():
    msgs, wm = await get_messages_since_watermark(FakeDB(), "not-a-uuid")
    assert msgs == []
    assert wm is None


@pytest.mark.asyncio
async def test_get_messages_since_watermark_conversation_missing():
    db = FakeDB()
    msgs, wm = await get_messages_since_watermark(db, str(uuid.uuid4()))
    assert msgs == []
    assert wm is None


@pytest.mark.asyncio
async def test_get_messages_since_watermark_no_watermark_returns_all():
    conv = Conversation(id=uuid.uuid4(), title="t", memory_watermark_message_id=None)
    db = FakeDB(get_map={(Conversation, conv.id): conv}, select_results=[[]])
    msgs, wm = await get_messages_since_watermark(db, str(conv.id))
    assert msgs == []
    assert wm is None


@pytest.mark.asyncio
async def test_get_messages_since_watermark_with_watermark():
    wm_id = uuid.uuid4()
    conv = Conversation(id=uuid.uuid4(), title="t", memory_watermark_message_id=wm_id)
    new_msgs = [
        Message(
            id=uuid.uuid4(),
            conversation_id=conv.id,
            role="user",
            content="x",
            created_at=datetime.utcnow(),
        )
    ]
    # db.get(Conversation) via get_map; watermark Message fetch via get_map too
    wm_msg = Message(
        id=wm_id,
        conversation_id=conv.id,
        role="assistant",
        content="old",
        created_at=datetime.utcnow(),
    )
    db = FakeDB(
        get_map={(Conversation, conv.id): conv, (Message, wm_id): wm_msg},
        select_results=[new_msgs],
    )
    msgs, wm = await get_messages_since_watermark(db, str(conv.id))
    assert msgs == new_msgs
    assert wm == wm_id


@pytest.mark.asyncio
async def test_update_watermark_invalid_uuid_noop():
    db = FakeDB()
    await update_watermark(db, "bad", uuid.uuid4())
    assert db.flush_count == 0


@pytest.mark.asyncio
async def test_update_watermark_sets_field():
    conv = Conversation(id=uuid.uuid4(), title="t")
    db = FakeDB(get_map={(Conversation, conv.id): conv})
    new_wm = uuid.uuid4()
    await update_watermark(db, str(conv.id), new_wm)
    assert conv.memory_watermark_message_id == new_wm
    assert conv.updated_at is not None
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_update_watermark_conversation_missing():
    db = FakeDB()
    await update_watermark(db, str(uuid.uuid4()), uuid.uuid4())
    assert db.flush_count == 0


# ══════════════════════════════════════════════════════════════════════
# 7. maybe_run_memory_extraction
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_maybe_run_disabled_interval(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_EXTRACTION_INTERVAL", 0)
    assert await maybe_run_memory_extraction(uuid.uuid4(), "resp", []) == (0, False, False)


@pytest.mark.asyncio
async def test_notebook_material_does_not_become_global_memory(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_EXTRACTION_INTERVAL", 1)
    conv = Conversation(id=uuid.uuid4(), title="Study", is_notebook=True)
    db = FakeDB(get_map={(Conversation, conv.id): conv})
    with (
        patch.object(me, "async_session_factory", _fake_session_factory([db])),
        patch("app.services.background_queue.enqueue_extraction_job") as enqueue,
    ):
        assert await maybe_run_memory_extraction(conv.id, "source material", []) == (
            0,
            False,
            False,
        )
    enqueue.assert_not_called()


@pytest.mark.asyncio
async def test_maybe_run_below_interval(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_EXTRACTION_INTERVAL", 4)
    conv = Conversation(id=uuid.uuid4(), title="t", memory_watermark_message_id=None)
    db = FakeDB(get_map={(Conversation, conv.id): conv}, select_results=[[]])
    factory = _fake_session_factory([db])
    with patch.object(me, "async_session_factory", factory):
        result = await maybe_run_memory_extraction(conv.id, "resp", [])
    assert result == (0, False, False)


@pytest.mark.asyncio
async def test_maybe_run_due_enqueues_job(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_EXTRACTION_INTERVAL", 2)
    conv = Conversation(id=uuid.uuid4(), title="t", memory_watermark_message_id=None)
    msgs = [
        Message(id=uuid.uuid4(), conversation_id=conv.id, role="user", content="hi"),
        Message(id=uuid.uuid4(), conversation_id=conv.id, role="assistant", content="hello"),
    ]
    # Session 1: read messages since watermark; Session 2: advance watermark
    db1 = FakeDB(get_map={(Conversation, conv.id): conv}, select_results=[msgs])
    db2 = FakeDB(get_map={(Conversation, conv.id): conv})
    factory = _fake_session_factory([db1, db2])
    enqueue_mock = MagicMock()
    with (
        patch.object(me, "async_session_factory", factory),
        patch(
            "app.services.background_queue.enqueue_extraction_job", enqueue_mock
        ),
    ):
        result = await maybe_run_memory_extraction(
            conv.id, "the answer", [_msg("user", "question")]
        )
    assert result == (0, True, True)
    # Watermark advanced to the newest message before enqueueing
    assert conv.memory_watermark_message_id == msgs[-1].id
    assert db2.commit_count == 1
    enqueue_mock.assert_called_once()
    conv_id_arg, extraction_msgs = enqueue_mock.call_args[0]
    assert conv_id_arg == str(conv.id)
    # Context = request messages + the just-generated assistant response
    assert extraction_msgs[-1] == {"role": "assistant", "content": "the answer"}


@pytest.mark.asyncio
async def test_maybe_run_context_window_slicing(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_EXTRACTION_INTERVAL", 2)
    conv = Conversation(id=uuid.uuid4(), title="t", memory_watermark_message_id=None)
    msgs = [
        Message(id=uuid.uuid4(), conversation_id=conv.id, role="user", content="x"),
        Message(id=uuid.uuid4(), conversation_id=conv.id, role="assistant", content="y"),
    ]
    request_messages = [_msg("user", f"m{i}") for i in range(10)]
    db1 = FakeDB(get_map={(Conversation, conv.id): conv}, select_results=[msgs])
    db2 = FakeDB(get_map={(Conversation, conv.id): conv})
    factory = _fake_session_factory([db1, db2])
    enqueue_mock = MagicMock()
    with (
        patch.object(me, "async_session_factory", factory),
        patch(
            "app.services.background_queue.enqueue_extraction_job", enqueue_mock
        ),
    ):
        await maybe_run_memory_extraction(conv.id, "answer", request_messages)
    _, extraction_msgs = enqueue_mock.call_args[0]
    # ctx_window=6 → last 5 request messages + 1 assistant message
    assert len(extraction_msgs) == 6
    assert extraction_msgs[0]["content"] == "m5"


@pytest.mark.asyncio
async def test_maybe_run_failure_returns_not_run(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_EXTRACTION_INTERVAL", 2)

    def exploding_factory():
        raise RuntimeError("db unavailable")

    with patch.object(me, "async_session_factory", exploding_factory):
        result = await maybe_run_memory_extraction(uuid.uuid4(), "resp", [])
    assert result == (0, False, False)


def test_extract_log_handles_bad_format_string(capsys):
    me._extract_log("bad %d", "oops")
    assert "bad" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_maybe_run_watermark_failure_still_enqueues(monkeypatch):
    # If advancing the watermark fails, the extraction job is still enqueued.
    monkeypatch.setattr(settings, "MEMORY_EXTRACTION_INTERVAL", 2)
    conv = Conversation(id=uuid.uuid4(), title="t", memory_watermark_message_id=None)
    msgs = [
        Message(id=uuid.uuid4(), conversation_id=conv.id, role="user", content="hi"),
        Message(
            id=uuid.uuid4(), conversation_id=conv.id, role="assistant", content="hello"
        ),
    ]
    db1 = FakeDB(get_map={(Conversation, conv.id): conv}, select_results=[msgs])

    class _FlakyFactory:
        calls = 0

        def __call__(self):
            _FlakyFactory.calls += 1
            if _FlakyFactory.calls == 1:
                return _fake_session_factory([db1])()
            raise RuntimeError("watermark session failed")

    enqueue_mock = MagicMock()
    with (
        patch.object(me, "async_session_factory", _FlakyFactory()),
        patch("app.services.background_queue.enqueue_extraction_job", enqueue_mock),
    ):
        result = await maybe_run_memory_extraction(
            conv.id, "the answer", [_msg("user", "question")]
        )
    assert result == (0, True, True)  # pending — job enqueued despite failure
    enqueue_mock.assert_called_once()
