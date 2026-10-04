"""Tests for app/services/memory.py (MemoryManager + pure helpers).

What is tested:
  - Pure text helpers: tokenize, get_text_similarity, content_tokens,
    content_jaccard, is_likely_duplicate (3-tier dedup decision with
    content-aware vector guard), _fingerprint_memories.
  - AppState helpers: _get/_set_app_state, audit counter
    increment/reset, audit fingerprint get/set.
  - MemoryManager CRUD: add/get/update/delete/pin memories.
  - Hybrid retrieval: _hybrid_scored (empty query → pinned only, gate,
    cutoff, pinned bypass, BM25-only degraded mode), get_relevant_memories
    (top_k trim + pinned re-add), search_memories.
  - Dedup lookups: find_duplicates (exact + Jaccard + vector tiers),
    find_similar_by_vector (guard rejection, missing candidates).
  - increment_uses, get_categories_with_counts, singleton wrapper.

What is mocked:
  - The AsyncSession is replaced by FakeDB (a tiny in-memory double that
    routes db.execute() by SQLAlchemy statement type) — NO real Postgres.
  - app.services.memory.get_embedding is patched (AsyncMock) so no call
    ever reaches Ollama at OLLAMA_BASE_URL.
"""

import uuid
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import Select, TextClause, Update

import app.services.memory as memory_service
from app.db.models import AppState, Memory
from app.services.memory import (
    MemoryManager,
    _fingerprint_memories,
    _get_app_state,
    _get_audit_fingerprint,
    _get_extractions_since_audit,
    _get_manager,
    _increment_extractions_since_audit,
    _reset_extractions_since_audit,
    _set_app_state,
    _set_audit_fingerprint,
    content_jaccard,
    content_tokens,
    get_text_similarity,
    is_likely_duplicate,
    tokenize,
)


# ══════════════════════════════════════════════════════════════════════
# Test doubles
# ══════════════════════════════════════════════════════════════════════


class _ScalarsResult:
    """Mimics a SQLAlchemy Result for ORM select() statements."""

    def __init__(self, items):
        self._items = list(items)

    def scalars(self):
        return self

    def all(self):
        return list(self._items)

    def scalar_one_or_none(self):
        return self._items[0] if self._items else None


class _RowcountResult:
    def __init__(self, rowcount):
        self.rowcount = rowcount


class FakeDB:
    """Minimal AsyncSession double routed by SQLAlchemy statement type.

    - select_results: list of result sets; each successive Select pops the
      front (the last set is reused once exhausted).
    - text_rows: callable(TextClause) -> list of row tuples for raw SQL.
    - update_rowcount: rowcount returned for update() statements.
    - get_map: {(Model, pk): obj} for db.get().
    """

    def __init__(
        self,
        select_results=None,
        text_rows=None,
        update_rowcount=0,
        get_map=None,
    ):
        self._select_results = [list(r) for r in (select_results or [])]
        self._text_rows = text_rows or (lambda stmt: [])
        self._update_rowcount = update_rowcount
        self._get_map = dict(get_map or {})
        self.added = []
        self.deleted = []
        self.executed = []
        self.refreshed = []
        self.flush_count = 0
        self.commit_count = 0

    async def execute(self, stmt, *args, **kwargs):
        self.executed.append(stmt)
        if isinstance(stmt, Select):
            if self._select_results:
                result = self._select_results.pop(0)
            elif self._last_select:
                result = self._last_select
            else:
                result = []
            self._last_select = result
            return _ScalarsResult(result)
        if isinstance(stmt, TextClause):
            return list(self._text_rows(stmt))
        if isinstance(stmt, Update):
            return _RowcountResult(self._update_rowcount)
        raise AssertionError(f"unexpected statement type: {type(stmt)!r}")

    _last_select = None

    async def get(self, model, key):
        return self._get_map.get((model, key))

    def add(self, obj):
        self.added.append(obj)

    async def delete(self, obj):
        self.deleted.append(obj)

    async def flush(self):
        self.flush_count += 1

    async def refresh(self, obj):
        self.refreshed.append(obj)

    async def commit(self):
        self.commit_count += 1


def route_text_rows(routes):
    """Build a TextClause router from (sql-substring, rows) pairs."""

    def _route(stmt):
        sql = str(stmt)
        for substring, rows in routes:
            if substring in sql:
                return rows
        return []

    return _route


def make_memory(**kwargs) -> Memory:
    defaults = dict(
        id=uuid.uuid4(),
        text="User likes pizza",
        category="preference",
        source="auto",
        pinned=False,
        uses=0,
        conversation_id=None,
        embedding=[0.1, 0.2, 0.3],
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    defaults.update(kwargs)
    return Memory(**defaults)


@pytest.fixture
def no_embeddings():
    """Patch get_embedding in the memory module to return None (no Ollama)."""
    with patch.object(memory_service, "get_embedding", new=AsyncMock(return_value=None)):
        yield


# ══════════════════════════════════════════════════════════════════════
# 1. Pure text helpers
# ══════════════════════════════════════════════════════════════════════


def test_tokenize_splits_and_strips_punctuation():
    assert tokenize("Hello, world! ") == ["Hello", "world"]
    assert tokenize('say "hi".') == ["say", "hi"]


def test_tokenize_drops_punctuation_only_tokens():
    assert tokenize("a . . b") == ["a", "b"]


def test_get_text_similarity_identical():
    assert get_text_similarity("User likes tea", "User likes tea") == 1.0


def test_get_text_similarity_case_insensitive_overlap():
    sim = get_text_similarity("User Likes Tea", "user likes tea")
    assert sim == 1.0


def test_get_text_similarity_disjoint():
    assert get_text_similarity("alpha beta", "gamma delta") == 0.0


def test_get_text_similarity_empty_inputs():
    assert get_text_similarity("", "abc") == 0.0
    assert get_text_similarity("abc", "") == 0.0
    assert get_text_similarity("", "") == 0.0


def test_get_text_similarity_both_only_punctuation_is_one():
    # both token sets empty after tokenization → treated as identical
    assert get_text_similarity("...", "!!!") == 1.0


def test_get_text_similarity_one_side_only_punctuation_is_zero():
    # one token set empty after tokenization → no overlap possible
    assert get_text_similarity("...", "abc") == 0.0
    assert get_text_similarity("abc", "...") == 0.0


def test_content_tokens_strips_stopwords_and_short_tokens():
    tokens = content_tokens("The user's name is Sam, and he lives in Paris")
    assert "sam" in tokens
    assert "paris" in tokens
    # stopwords / boilerplate / single chars removed
    assert "the" not in tokens
    assert "user" not in tokens
    assert "is" not in tokens
    assert "s" not in tokens


def test_content_tokens_empty_text():
    assert content_tokens("") == set()
    assert content_tokens(None) == set()


def test_content_tokens_keeps_accented_words():
    tokens = content_tokens("Clémence et Søren")
    assert "clémence" in tokens
    assert "søren" in tokens


def test_content_jaccard_both_empty():
    jac, n1, n2 = content_jaccard("the is", "of a")
    assert (jac, n1, n2) == (1.0, 0, 0)


def test_content_jaccard_one_side_empty():
    jac, n1, n2 = content_jaccard("paris", "the of")
    assert jac == 0.0
    assert n1 == 1
    assert n2 == 0


def test_content_jaccard_partial_overlap():
    jac, n1, n2 = content_jaccard("sam paris", "sam berlin")
    assert jac == pytest.approx(1 / 3)
    assert (n1, n2) == (2, 2)


def test_is_likely_duplicate_exact_match():
    assert is_likely_duplicate("User likes tea", "user likes TEA") == (True, "exact")


def test_is_likely_duplicate_empty_text_is_no_match():
    assert is_likely_duplicate("", "something") == (False, "no_match")
    assert is_likely_duplicate("something", "") == (False, "no_match")


def test_is_likely_duplicate_fuzzy_text_tier():
    # Content Jaccard >= 0.6 with default settings
    is_dup, reason = is_likely_duplicate(
        "User prefers dark mode", "User prefers dark mode at night"
    )
    assert is_dup is True
    assert reason == "fuzzy_text"


def test_is_likely_duplicate_distinct_cities_not_fuzzy():
    # "User lives in Paris" vs "User lives in Berlin" share only stopwords
    is_dup, reason = is_likely_duplicate("User lives in Paris", "User lives in Berlin")
    assert is_dup is False
    assert reason == "no_match"


def test_is_likely_duplicate_vector_with_content_overlap():
    is_dup, reason = is_likely_duplicate(
        "User's name is Sam",
        "The user is called Sam",
        vector_sim=0.90,
        vector_threshold=0.85,
        short_text_threshold=0.92,
        content_min_overlap=0.10,
    )
    assert is_dup is True
    assert reason == "vector+content"


def test_is_likely_duplicate_vector_rejected_by_content_guard():
    # The Clémence-vs-Abdel false positive: high cosine, zero content tokens
    is_dup, reason = is_likely_duplicate(
        "User's name is Abdel",
        "User's girlfriend is Clémence",
        vector_sim=0.99,
        vector_threshold=0.85,
        short_text_threshold=0.92,
        content_min_overlap=0.10,
    )
    assert is_dup is False
    assert reason == "no_match"


def test_is_likely_duplicate_short_text_bump_applies_when_overlap_weak():
    # Both texts <5 content tokens with weak overlap → stricter threshold (0.92)
    is_dup, _ = is_likely_duplicate(
        "Abdel lives in Lyon",
        "Marco lives in Rome",
        vector_sim=0.88,  # >= 0.85 but < 0.92
        vector_threshold=0.85,
        short_text_threshold=0.92,
        content_min_overlap=0.10,
    )
    assert is_dup is False


def test_is_likely_duplicate_short_text_bump_skipped_when_overlap_strong():
    # cj >= 0.5 → no bump, normal 0.85 threshold applies
    is_dup, reason = is_likely_duplicate(
        "User's name is Sam",
        "The user is called Sam",
        vector_sim=0.88,  # >= 0.85, would fail the 0.92 bump
        vector_threshold=0.85,
        short_text_threshold=0.92,
        content_min_overlap=0.10,
    )
    assert is_dup is True
    assert reason == "vector+content"


def test_is_likely_duplicate_vector_below_threshold():
    is_dup, reason = is_likely_duplicate(
        "Sam likes pizza",
        "Sam likes pasta",
        vector_sim=0.5,
        vector_threshold=0.85,
    )
    assert is_dup is False
    assert reason == "no_match"


def test_is_likely_duplicate_without_vector_args():
    # No vector score supplied → only exact/fuzzy tiers
    assert is_likely_duplicate("User likes tea", "user likes tea") == (True, "exact")
    assert is_likely_duplicate("User likes tea", "Sam lives in Rome")[0] is False


def test_fingerprint_stable_and_order_independent():
    m1 = make_memory(text="alpha", category="fact")
    m2 = make_memory(text="beta", category="goal")
    assert _fingerprint_memories([m1, m2]) == _fingerprint_memories([m2, m1])


def test_fingerprint_changes_when_text_changes():
    m1 = make_memory(text="alpha")
    m2 = make_memory(text="alpha!")
    assert _fingerprint_memories([m1]) != _fingerprint_memories([m2])


def test_fingerprint_depends_only_on_id_text_category():
    fixed_id = uuid.uuid4()
    m1 = make_memory(id=fixed_id, uses=0, created_at=datetime.utcnow())
    m2 = make_memory(
        id=fixed_id, uses=99, created_at=datetime.utcnow() - timedelta(days=5)
    )
    assert _fingerprint_memories([m1]) == _fingerprint_memories([m2])


# ══════════════════════════════════════════════════════════════════════
# 2. AppState helpers
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_app_state_missing_returns_none():
    db = FakeDB(select_results=[[]])
    assert await _get_app_state(db, "memory.audit_fingerprint") is None


@pytest.mark.asyncio
async def test_get_app_state_present():
    db = FakeDB(select_results=[["abc123"]])
    assert await _get_app_state(db, "memory.audit_fingerprint") == "abc123"


@pytest.mark.asyncio
async def test_set_app_state_updates_existing_row():
    existing = AppState(key="k", value="old", updated_at=datetime(2020, 1, 1))
    db = FakeDB(get_map={(AppState, "k"): existing})
    await _set_app_state(db, "k", "new")
    assert existing.value == "new"
    assert db.added == []  # no insert when the row exists
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_set_app_state_inserts_new_row():
    db = FakeDB()
    await _set_app_state(db, "k", "v1")
    assert len(db.added) == 1
    assert db.added[0].key == "k"
    assert db.added[0].value == "v1"
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_get_extractions_since_audit_defaults_to_zero():
    assert await _get_extractions_since_audit(FakeDB(select_results=[[]])) == 0
    # non-numeric garbage → 0
    assert await _get_extractions_since_audit(FakeDB(select_results=[["junk"]])) == 0
    # negative clamped to 0
    assert await _get_extractions_since_audit(FakeDB(select_results=[["-3"]])) == 0


@pytest.mark.asyncio
async def test_get_extractions_since_audit_valid_value():
    assert await _get_extractions_since_audit(FakeDB(select_results=[["7"]])) == 7


@pytest.mark.asyncio
async def test_increment_extractions_since_audit():
    db = FakeDB(select_results=[["4"]])
    new_val = await _increment_extractions_since_audit(db, 3)
    assert new_val == 7
    assert len(db.added) == 1
    assert db.added[0].value == "7"


@pytest.mark.asyncio
async def test_reset_extractions_since_audit():
    db = FakeDB()
    await _reset_extractions_since_audit(db)
    assert db.added[0].key == "memory.extractions_since_audit"
    assert db.added[0].value == "0"


@pytest.mark.asyncio
async def test_audit_fingerprint_round_trip():
    db = FakeDB(select_results=[["fp-1"]])
    assert await _get_audit_fingerprint(db) == "fp-1"
    await _set_audit_fingerprint(db, "fp-2")
    assert db.added[0].key == "memory.audit_fingerprint"
    assert db.added[0].value == "fp-2"


# ══════════════════════════════════════════════════════════════════════
# 3. MemoryManager CRUD
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_add_memory_with_explicit_embedding_skips_ollama():
    db = FakeDB()
    mgr = MemoryManager()
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock()
    ) as embed_mock:
        mem = await mgr.add_memory(
            db,
            "  User prefers dark mode  ",
            category="preference",
            source="user",
            embedding=[0.5],
        )
    embed_mock.assert_not_awaited()
    assert mem.text == "User prefers dark mode"  # stripped
    assert mem.category == "preference"
    assert mem.source == "user"
    assert mem.pinned is False
    assert mem.embedding == [0.5]
    assert db.added == [mem]
    assert db.flush_count == 1
    assert db.refreshed == [mem]


@pytest.mark.asyncio
async def test_add_memory_empty_text_raises():
    with pytest.raises(ValueError, match="empty"):
        await MemoryManager().add_memory(FakeDB(), "   ")


@pytest.mark.asyncio
async def test_add_memory_invalid_category_and_source_fall_back():
    mem = await MemoryManager().add_memory(
        FakeDB(), "Some fact", category="nonsense", source="bogus", embedding=[1.0]
    )
    assert mem.category == "fact"
    assert mem.source == "auto"


@pytest.mark.asyncio
async def test_add_memory_identity_auto_pins():
    mem = await MemoryManager().add_memory(
        FakeDB(), "User's name is Sam", category="identity", embedding=[0.1]
    )
    assert mem.pinned is True


@pytest.mark.asyncio
async def test_add_memory_invalid_conversation_id_becomes_none():
    mem = await MemoryManager().add_memory(
        FakeDB(), "A fact", conversation_id="not-a-uuid", embedding=[0.1]
    )
    assert mem.conversation_id is None


@pytest.mark.asyncio
async def test_add_memory_valid_conversation_id_parsed():
    conv_id = uuid.uuid4()
    mem = await MemoryManager().add_memory(
        FakeDB(), "A fact", conversation_id=str(conv_id), embedding=[0.1]
    )
    assert mem.conversation_id == conv_id


@pytest.mark.asyncio
async def test_add_memory_generates_embedding_when_missing():
    db = FakeDB()
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.9, 0.8])
    ) as embed_mock:
        mem = await MemoryManager().add_memory(db, "User hates spam")
    embed_mock.assert_awaited_once_with("User hates spam")
    assert mem.embedding == [0.9, 0.8]


@pytest.mark.asyncio
async def test_get_all_memories_returns_rows():
    mems = [make_memory(), make_memory()]
    db = FakeDB(select_results=[mems])
    result = await MemoryManager().get_all_memories(db, limit=2, offset=4)
    assert result == mems


@pytest.mark.asyncio
async def test_get_all_memories_valid_category_adds_where():
    db = FakeDB(select_results=[[]])
    await MemoryManager().get_all_memories(db, category="goal")
    assert "WHERE memories.category" in str(db.executed[0])


@pytest.mark.asyncio
async def test_get_all_memories_invalid_category_no_where():
    db = FakeDB(select_results=[[]])
    await MemoryManager().get_all_memories(db, category="nonsense")
    assert "WHERE" not in str(db.executed[0])


@pytest.mark.asyncio
async def test_get_memory_by_id_invalid_uuid_returns_none():
    assert await MemoryManager().get_memory_by_id(FakeDB(), "xyz") is None


@pytest.mark.asyncio
async def test_get_memory_by_id_found():
    mem = make_memory()
    db = FakeDB(select_results=[[mem]])
    assert await MemoryManager().get_memory_by_id(db, str(mem.id)) is mem


@pytest.mark.asyncio
async def test_update_memory_not_found_returns_none():
    db = FakeDB(select_results=[[]])
    assert await MemoryManager().update_memory(db, str(uuid.uuid4()), text="new") is None


@pytest.mark.asyncio
async def test_update_memory_regenerates_embedding_on_text_change():
    mem = make_memory(text="old text", embedding=[0.1])
    db = FakeDB(select_results=[[mem]])
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.7])
    ) as embed_mock:
        updated = await MemoryManager().update_memory(db, str(mem.id), text="new text")
    assert updated is mem
    assert mem.text == "new text"
    assert mem.embedding == [0.7]
    embed_mock.assert_awaited_once_with("new text")


@pytest.mark.asyncio
async def test_update_memory_same_text_skips_embedding():
    mem = make_memory(text="same text", embedding=[0.1])
    db = FakeDB(select_results=[[mem]])
    with patch.object(memory_service, "get_embedding", new=AsyncMock()) as embed_mock:
        await MemoryManager().update_memory(db, str(mem.id), text="same text")
    embed_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_update_memory_invalid_category_ignored():
    mem = make_memory(category="fact")
    db = FakeDB(select_results=[[mem]])
    await MemoryManager().update_memory(db, str(mem.id), category="bogus")
    assert mem.category == "fact"


@pytest.mark.asyncio
async def test_update_memory_valid_category():
    mem = make_memory(category="fact")
    db = FakeDB(select_results=[[mem]])
    await MemoryManager().update_memory(db, str(mem.id), category="goal")
    assert mem.category == "goal"


@pytest.mark.asyncio
async def test_delete_memory_not_found_returns_false():
    db = FakeDB(select_results=[[]])
    assert await MemoryManager().delete_memory(db, str(uuid.uuid4())) is False
    assert db.deleted == []


@pytest.mark.asyncio
async def test_delete_memory_found():
    mem = make_memory()
    db = FakeDB(select_results=[[mem]])
    assert await MemoryManager().delete_memory(db, str(mem.id)) is True
    assert db.deleted == [mem]
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_pin_memory_not_found():
    db = FakeDB(select_results=[[]])
    assert await MemoryManager().pin_memory(db, str(uuid.uuid4()), True) is None


@pytest.mark.asyncio
async def test_pin_memory_toggles_flag():
    mem = make_memory(pinned=False)
    db = FakeDB(select_results=[[mem]])
    updated = await MemoryManager().pin_memory(db, str(mem.id), True)
    assert updated is mem
    assert mem.pinned is True


# ══════════════════════════════════════════════════════════════════════
# 4. Hybrid retrieval
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_hybrid_scored_empty_query_returns_pinned_only():
    pinned = make_memory(pinned=True, text="User's name is Sam")
    unpinned = make_memory(pinned=False)
    db = FakeDB(select_results=[[pinned]])
    scored = await MemoryManager()._hybrid_scored(db, "   ", category=None, top_k=5)
    assert scored == [(1.0, pinned)]
    assert unpinned not in [m for _, m in scored]


@pytest.mark.asyncio
async def test_hybrid_scored_no_memories_returns_empty(no_embeddings):
    db = FakeDB(select_results=[[]])
    scored = await MemoryManager()._hybrid_scored(
        db, "what is my name", category=None, top_k=5
    )
    assert scored == []


@pytest.mark.asyncio
async def test_hybrid_scored_vector_path_ranks_and_gates():
    hit = make_memory(text="User's name is Sam", category="fact")
    miss = make_memory(text="irrelevant fact", category="fact")
    pinned = make_memory(text="pinned fact", pinned=True)
    # First Select: all memories. TextClause calls: BM25 ranks, vector sims.
    db = FakeDB(
        select_results=[[hit, miss, pinned]],
        text_rows=route_text_rows(
            [
                ("ts_rank_cd", [(str(hit.id), 0.10)]),
                ("GREATEST", [(str(hit.id), 0.90)]),
            ]
        ),
    )
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.3])
    ):
        scored = await MemoryManager()._hybrid_scored(
            db, "name sam", category=None, top_k=5
        )
    ids = {m.text: s for s, m in scored}
    # Pinned bypasses gate/cutoff at 1.0
    assert ids["pinned fact"] == 1.0
    # Vector hit passes gate (0.90 >= 0.20) and cutoff
    assert 0.3 < ids["User's name is Sam"] < 1.0
    # Low-relevance memory gated out
    assert "irrelevant fact" not in ids


@pytest.mark.asyncio
async def test_hybrid_scored_bm25_only_when_embedding_unavailable():
    hit = make_memory(text="User's name is Sam")
    db = FakeDB(
        select_results=[[hit]],
        text_rows=route_text_rows([("ts_rank_cd", [(str(hit.id), 6.0)])]),
    )
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=None)
    ):
        scored = await MemoryManager()._hybrid_scored(
            db, "sam", category=None, top_k=5
        )
    assert len(scored) == 1
    score, mem = scored[0]
    assert mem is hit
    # kw_norm=1.0 → final = 0.95 * 1.0 + 0.05 * recency
    assert score > 0.95


@pytest.mark.asyncio
async def test_hybrid_scored_bm25_failure_degrades_gracefully():
    hit = make_memory(text="User's name is Sam")

    def bad_text_rows(stmt):
        raise RuntimeError("tsvector not available")

    db = FakeDB(select_results=[[hit]], text_rows=bad_text_rows)
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=None)
    ):
        scored = await MemoryManager()._hybrid_scored(
            db, "sam", category=None, top_k=5
        )
    # BM25 failed AND embedding is None → nothing can pass the gate
    assert scored == []


@pytest.mark.asyncio
async def test_hybrid_scored_empty_query_with_category_filter():
    pinned = make_memory(pinned=True, category="identity", text="User's name is Sam")
    db = FakeDB(select_results=[[pinned]])
    scored = await MemoryManager()._hybrid_scored(
        db, "", category="identity", top_k=5
    )
    assert scored == [(1.0, pinned)]
    # the pinned-only branch also applies the category filter
    assert "AND memories.category" in str(db.executed[0])


@pytest.mark.asyncio
async def test_hybrid_scored_category_filter_applies_to_candidates():
    # NOTE: with a category filter the BM25 rank statement currently
    # fails to bind :cat (app bug: bindparams(cat=...) is called on the
    # un-filtered statement) — the exception is swallowed and scoring
    # degrades to vector-only. This test pins the OBSERVED behavior:
    # the category filter reaches the candidate query and the vector hit
    # still scores.
    hit = make_memory(text="User's name is Sam", category="fact")
    db = FakeDB(
        select_results=[[hit]],
        text_rows=route_text_rows([("GREATEST", [(str(hit.id), 0.90)])]),
    )
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.3])
    ):
        scored = await MemoryManager()._hybrid_scored(
            db, "name sam", category="fact", top_k=5
        )
    assert [m for _, m in scored] == [hit]
    # candidate fetch filtered by category
    assert "WHERE memories.category" in str(db.executed[0])
    # ...and the hit survived on vector similarity alone (BM25 lost)
    score = scored[0][0]
    assert 0.49 < score < 0.56  # ≈ 0.55 * 0.90 + 0.05 * recency


@pytest.mark.asyncio
async def test_hybrid_scored_vector_batch_failure_degrades_to_bm25():
    hit = make_memory(text="User's name is Sam")

    def router(stmt):
        if "ts_rank_cd" in str(stmt):
            return [(str(hit.id), 6.0)]
        raise RuntimeError("pgvector batch distance query failed")  # GREATEST stmt

    db = FakeDB(select_results=[[hit]], text_rows=router)
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.3])
    ):
        scored = await MemoryManager()._hybrid_scored(db, "sam", category=None, top_k=5)
    # vector sims lost → vs=0, but BM25 (kw_norm=1.0) carries the memory
    assert len(scored) == 1
    score, mem = scored[0]
    assert mem is hit
    assert 0.40 <= score <= 0.46  # 0.40 * 1.0 + 0.05 * recency(≈1)


@pytest.mark.asyncio
async def test_hybrid_scored_invalid_created_at_recency_fallback():
    # created_at is not a datetime → the recency computation falls back to
    # days_old = 0 instead of crashing the scoring loop
    hit = make_memory(created_at="not-a-date")
    db = FakeDB(
        select_results=[[hit]],
        text_rows=route_text_rows([("GREATEST", [(str(hit.id), 0.90)])]),
    )
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.3])
    ):
        scored = await MemoryManager()._hybrid_scored(db, "sam", category=None, top_k=5)
    assert len(scored) == 1
    assert scored[0][1] is hit


@pytest.mark.asyncio
async def test_hybrid_scored_cutoff_drops_borderline_memory():
    # vs=0.20 passes the gate (>= GATE_VECTOR 0.20) but the final score
    # ≈ 0.55*0.20 + 0.05*recency(400 days old) ≈ 0.112 <= CUTOFF 0.12
    old = make_memory(created_at=datetime.utcnow() - timedelta(days=400))
    db = FakeDB(
        select_results=[[old]],
        text_rows=route_text_rows([("GREATEST", [(str(old.id), 0.20)])]),
    )
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.3])
    ):
        scored = await MemoryManager()._hybrid_scored(db, "sam", category=None, top_k=5)
    assert scored == []


@pytest.mark.asyncio
async def test_hybrid_scored_empty_query_returns_all_pinned():
    # The empty-query branch returns every pinned memory at score 1.0
    # (trimming to top_k happens in get_relevant_memories).
    mems = [make_memory(pinned=True, text=f"pinned {i}") for i in range(5)]
    db = FakeDB(select_results=[mems])
    scored = await MemoryManager()._hybrid_scored(db, "", category=None, top_k=2)
    assert len(scored) == 5
    assert all(score == 1.0 for score, _ in scored)


@pytest.mark.asyncio
async def test_get_relevant_memories_trims_to_top_k(no_embeddings):
    mems = [make_memory(pinned=True, text=f"pinned {i}") for i in range(5)]
    db = FakeDB(select_results=[mems])
    result = await MemoryManager().get_relevant_memories(db, "", top_k=3)
    assert len(result) == 3
    assert all(m.pinned for m in result)


@pytest.mark.asyncio
async def test_get_relevant_memories_readds_pinned_below_cutoff():
    # Query with no vector hits returns nothing scored; the pinned re-add
    # pass then pulls pinned memories back in up to top_k.
    pinned = make_memory(pinned=True, text="User's name is Sam")
    db = FakeDB(
        select_results=[[], [pinned]],  # candidates query, then pinned re-add
        text_rows=route_text_rows([]),
    )
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.3])
    ):
        result = await MemoryManager().get_relevant_memories(db, "anything", top_k=5)
    assert result == [pinned]


@pytest.mark.asyncio
async def test_get_relevant_memories_pinned_readd_stops_at_top_k():
    # No scored candidates → the pinned re-add loop fills up to top_k and
    # breaks instead of appending every pinned memory.
    pinned = [make_memory(pinned=True, text=f"pinned {i}") for i in range(3)]
    db = FakeDB(select_results=[[], pinned])
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.3])
    ):
        result = await MemoryManager().get_relevant_memories(db, "query", top_k=1)
    assert len(result) == 1
    assert result[0].pinned is True


@pytest.mark.asyncio
async def test_search_memories_returns_unscored_list(no_embeddings):
    hit = make_memory(text="User's name is Sam")
    db = FakeDB(
        select_results=[[hit]],
        text_rows=route_text_rows([("ts_rank_cd", [(str(hit.id), 2.0)])]),
    )
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=None)
    ):
        result = await MemoryManager().search_memories(db, "sam", limit=10)
    assert result == [hit]


# ══════════════════════════════════════════════════════════════════════
# 5. Dedup lookups
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_find_duplicates_empty_text_returns_empty(no_embeddings):
    assert await MemoryManager().find_duplicates(FakeDB(), "   ") == []


@pytest.mark.asyncio
async def test_find_duplicates_exact_match_tier(no_embeddings):
    dup = make_memory(text="User likes pizza")
    db = FakeDB(select_results=[[dup]])
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=None)
    ):
        result = await MemoryManager().find_duplicates(db, "USER LIKES PIZZA")
    assert result == [dup]


@pytest.mark.asyncio
async def test_find_duplicates_jaccard_tier(no_embeddings):
    dup = make_memory(text="User likes pizza with extra cheese")
    db = FakeDB(select_results=[[dup]])
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=None)
    ):
        result = await MemoryManager().find_duplicates(
            db, "User likes pizza with extra cheese and olives"
        )
    assert result == [dup]


@pytest.mark.asyncio
async def test_find_duplicates_no_match(no_embeddings):
    other = make_memory(text="Sam works at Acme")
    db = FakeDB(select_results=[[other]])
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=None)
    ):
        result = await MemoryManager().find_duplicates(db, "User hates rain in July")
    assert result == []


@pytest.mark.asyncio
async def test_find_duplicates_vector_tier_accepted_with_content_guard():
    dup = make_memory(text="The user is called Sam")
    db = FakeDB(
        select_results=[[dup]],
        text_rows=route_text_rows(
            [
                ("ORDER BY sim DESC LIMIT 10", [(str(dup.id), 0.95)]),
                ("WHERE id = ANY", [(str(dup.id), 0.95)]),
            ]
        ),
    )
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.2])
    ):
        result = await MemoryManager().find_duplicates(db, "User's name is Sam")
    assert result == [dup]


@pytest.mark.asyncio
async def test_find_duplicates_vector_tier_rejected_by_guard():
    # High cosine but zero content overlap → guard rejects, no duplicates.
    other = make_memory(text="User's girlfriend is Clémence")
    db = FakeDB(
        select_results=[[other]],
        text_rows=route_text_rows(
            [
                ("ORDER BY sim DESC LIMIT 10", [(str(other.id), 0.99)]),
                ("WHERE id = ANY", [(str(other.id), 0.99)]),
            ]
        ),
    )
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.2])
    ):
        result = await MemoryManager().find_duplicates(db, "User's name is Abdel")
    assert result == []


@pytest.mark.asyncio
async def test_find_duplicates_vector_query_failure_falls_back_to_text(no_embeddings):
    dup = make_memory(text="User likes pizza")

    def bad_text_rows(stmt):
        raise RuntimeError("pgvector down")

    db = FakeDB(select_results=[[dup]], text_rows=bad_text_rows)
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.2])
    ):
        result = await MemoryManager().find_duplicates(db, "User likes pizza")
    # vector tier failed → exact tier still catches it
    assert result == [dup]


@pytest.mark.asyncio
async def test_find_duplicates_seen_ids_not_duplicated_in_result():
    # the same row appearing twice in the candidate list is reported once
    dup = make_memory(text="User likes pizza")
    db = FakeDB(select_results=[[dup, dup]])
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=None)
    ):
        result = await MemoryManager().find_duplicates(db, "User likes pizza")
    assert result == [dup]


@pytest.mark.asyncio
async def test_find_duplicates_sim_refetch_failure_degrades():
    # The sim re-fetch query fails → sim defaults to 0.0 → the content
    # guard rejects a match that would otherwise have been a duplicate.
    dup = make_memory(text="User likes pizza and cheese")

    def router(stmt):
        if "ORDER BY sim DESC LIMIT 10" in str(stmt):
            return [(str(dup.id), 0.95)]
        raise RuntimeError("sim re-fetch failed")  # WHERE id = ANY stmt

    db = FakeDB(select_results=[[dup]], text_rows=router)
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.2])
    ):
        result = await MemoryManager().find_duplicates(db, "User likes pizza")
    assert result == []


@pytest.mark.asyncio
async def test_find_similar_by_vector_no_embedding_returns_none(no_embeddings):
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=None)
    ):
        assert await MemoryManager().find_similar_by_vector(FakeDB(), "text") is None


@pytest.mark.asyncio
async def test_find_similar_by_vector_no_candidates():
    db = FakeDB(text_rows=route_text_rows([("LIMIT 5", [])]))
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.2])
    ):
        assert await MemoryManager().find_similar_by_vector(db, "text") is None


@pytest.mark.asyncio
async def test_find_similar_by_vector_accepted():
    mem = make_memory(text="The user is called Sam")
    db = FakeDB(
        select_results=[[mem]],
        text_rows=route_text_rows([("LIMIT 5", [(str(mem.id), 0.91)])]),
    )
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.2])
    ):
        result = await MemoryManager().find_similar_by_vector(
            db, "User's name is Sam"
        )
    assert result is mem


@pytest.mark.asyncio
async def test_find_similar_by_vector_all_rejected_by_guard():
    mem = make_memory(text="User's girlfriend is Clémence")
    db = FakeDB(
        select_results=[[mem]],
        text_rows=route_text_rows([("LIMIT 5", [(str(mem.id), 0.99)])]),
    )
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.2])
    ):
        result = await MemoryManager().find_similar_by_vector(
            db, "User's name is Abdel"
        )
    assert result is None


@pytest.mark.asyncio
async def test_find_similar_by_vector_walks_past_missing_candidate():
    # First candidate id no longer resolves → skipped, second one returned.
    mem = make_memory(text="The user is called Sam")
    ghost = uuid.uuid4()
    db = FakeDB(
        select_results=[[], [mem]],  # get_memory_by_id: ghost → None, then mem
        text_rows=route_text_rows(
            [("LIMIT 5", [(str(ghost), 0.95), (str(mem.id), 0.91)])]
        ),
    )
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.2])
    ):
        result = await MemoryManager().find_similar_by_vector(db, "User's name is Sam")
    assert result is mem


@pytest.mark.asyncio
async def test_find_similar_by_vector_query_error_returns_none():
    def bad_text_rows(stmt):
        raise RuntimeError("db exploded")

    db = FakeDB(text_rows=bad_text_rows)
    with patch.object(
        memory_service, "get_embedding", new=AsyncMock(return_value=[0.2])
    ):
        assert await MemoryManager().find_similar_by_vector(db, "text") is None


# ══════════════════════════════════════════════════════════════════════
# 6. Usage counter / categories / singleton
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_increment_uses_empty_list_returns_zero():
    assert await MemoryManager().increment_uses(FakeDB(), []) == 0


@pytest.mark.asyncio
async def test_increment_uses_invalid_uuids_return_zero():
    assert await MemoryManager().increment_uses(FakeDB(), ["nope", "also-nope"]) == 0


@pytest.mark.asyncio
async def test_increment_uses_mixed_ids_count_valid_only():
    db = FakeDB(update_rowcount=1)
    result = await MemoryManager().increment_uses(
        db, ["not-a-uuid", str(uuid.uuid4())]
    )
    assert result == 1
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_get_categories_with_counts_fills_missing_categories():
    db = FakeDB(select_results=[[("fact", 3), ("goal", 1)]])
    categories = await MemoryManager().get_categories_with_counts(db)
    by_cat = {c["category"]: c["count"] for c in categories}
    assert by_cat["fact"] == 3
    assert by_cat["goal"] == 1
    for missing in ("identity", "preference", "contact", "project"):
        assert by_cat[missing] == 0


def test_get_manager_is_singleton():
    m1 = _get_manager()
    m2 = _get_manager()
    assert m1 is m2
    assert isinstance(m1, MemoryManager)


@pytest.mark.asyncio
async def test_module_level_get_relevant_memories_delegates():
    pinned = make_memory(pinned=True)
    db = FakeDB(select_results=[[pinned]])
    result = await memory_service.get_relevant_memories(db, "", top_k=5)
    assert result == [pinned]
