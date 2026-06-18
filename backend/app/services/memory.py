"""
Memory management service for RealOpen-AI.

Provides CRUD operations, hybrid retrieval (pgvector cosine + tsvector BM25
+ recency), vector-based dedup, and LLM audit/consolidation for user
memories stored in PostgreSQL.

Retrieval design (hybrid):
    final = W_vec * vector_sim + W_bm25 * bm25_norm + W_rec * recency
    Gate:  drop if vector_sim < GATE_VEC AND bm25_norm < GATE_BM25
    Cutoff: keep only entries with final > CUTOFF
    Top-k: return the highest-scoring k entries

Vector search uses pgvector's cosine distance operator (<=>), which returns
DISTANCE (0=identical, 2=opposite). We convert to SIMILARITY = 1 - distance
so higher = better, matching the BM25 and recency terms.

BM25 uses PostgreSQL's built-in ts_rank_cd on the `search_vector` tsvector
column (managed by the DB via GENERATED ALWAYS AS). The 'simple' dictionary
is used so non-English names survive tokenization.

Recency is a soft tiebreaker: 1 / (1 + days_old * 0.05), capped at 5%
weight. This guarantees a memory can never rank highly on recency alone.
"""

import hashlib
import logging
import re
import uuid
from datetime import datetime
from typing import List, Optional, Tuple

from sqlalchemy import select, func, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import AppState, Memory
from app.services.embeddings import get_embedding

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {
    "identity",
    "preference",
    "fact",
    "contact",
    "project",
    "goal",
}
VALID_SOURCES = {"auto", "user", "ai_agent"}


# ---------------------------------------------------------------------------
# Tokenizer / Jaccard similarity (pure Python, used only as a fallback tier
# in the 3-tier dedup chain when pgvector is unavailable)
# ---------------------------------------------------------------------------


def tokenize(t: str) -> List[str]:
    """Whitespace split + strip trailing punctuation. Used by Jaccard fallback."""
    return [c for w in t.split() if (c := w.strip('.,!?";'))]


def get_text_similarity(text1: str, text2: str) -> float:
    """Jaccard similarity between two texts (0.0 - 1.0)."""
    if not text1 or not text2:
        return 0.0
    t1 = set(tokenize(text1.lower()))
    t2 = set(tokenize(text2.lower()))
    if not t1 and not t2:
        return 1.0
    if not t1 or not t2:
        return 0.0
    return len(t1 & t2) / len(t1 | t2)


# ---------------------------------------------------------------------------
# AppState helpers — persistent counters that survive restarts
# ---------------------------------------------------------------------------


async def _get_app_state(db: AsyncSession, key: str) -> Optional[str]:
    """Read a value from the app_state table. Returns None if missing."""
    stmt = select(AppState.value).where(AppState.key == key)
    result = await db.execute(stmt)
    row = result.scalar_one_or_none()
    return row


async def _set_app_state(db: AsyncSession, key: str, value: str) -> None:
    """Upsert a value in the app_state table."""
    existing = await db.get(AppState, key)
    if existing:
        existing.value = value
        existing.updated_at = datetime.utcnow()
    else:
        db.add(AppState(key=key, value=value, updated_at=datetime.utcnow()))
    await db.flush()


# ---------------------------------------------------------------------------
# Audit counter — DB-persisted so it survives restarts.
# ---------------------------------------------------------------------------


async def _get_extractions_since_audit(db: AsyncSession) -> int:
    raw = await _get_app_state(db, "memory.extractions_since_audit")
    if raw is None:
        return 0
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


async def _increment_extractions_since_audit(db: AsyncSession, delta: int) -> int:
    """Atomically increment the counter by delta. Returns the new value."""
    current = await _get_extractions_since_audit(db)
    new_val = current + delta
    await _set_app_state(db, "memory.extractions_since_audit", str(new_val))
    return new_val


async def _reset_extractions_since_audit(db: AsyncSession) -> None:
    await _set_app_state(db, "memory.extractions_since_audit", "0")


async def _get_audit_fingerprint(db: AsyncSession) -> Optional[str]:
    return await _get_app_state(db, "memory.audit_fingerprint")


async def _set_audit_fingerprint(db: AsyncSession, fp: str) -> None:
    await _set_app_state(db, "memory.audit_fingerprint", fp)


def _fingerprint_memories(memories: List[Memory]) -> str:
    """Stable SHA-256 of a memory set — order-independent, depends on id+text+category."""
    items = sorted((str(m.id), m.text or "", m.category or "") for m in memories)
    h = hashlib.sha256()
    for triple in items:
        h.update(("\x1f".join(triple) + "\x1e").encode("utf-8"))
    return h.hexdigest()


# ---------------------------------------------------------------------------
# MemoryManager — async, PostgreSQL-backed, pgvector + tsvector hybrid
# ---------------------------------------------------------------------------


class MemoryManager:
    """Manages memory entries in PostgreSQL with hybrid retrieval.

    All methods are async and take an AsyncSession. Callers are responsible
    for committing transactions (manager.flush() is called internally to
    populate auto-generated fields like IDs).
    """

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    async def add_memory(
        self,
        db: AsyncSession,
        text: str,
        category: str = "fact",
        source: str = "auto",
        conversation_id: Optional[str] = None,
        pinned: bool = False,
        embedding: Optional[List[float]] = None,
    ) -> Memory:
        """Add a new memory entry. Returns the created Memory object.

        If `embedding` is None, one will be generated automatically via
        Ollama. Pass an explicit embedding to avoid a redundant call when
        the caller has already embedded the text (e.g. during extraction
        when the text was just embedded for dedup).
        """
        if not text or not text.strip():
            raise ValueError("Memory text cannot be empty")

        category = category if category in VALID_CATEGORIES else "fact"
        source = source if source in VALID_SOURCES else "auto"

        conv_uuid = None
        if conversation_id:
            try:
                conv_uuid = uuid.UUID(str(conversation_id))
            except (ValueError, AttributeError):
                conv_uuid = None

        # Auto-pin identity facts
        if category == "identity" and not pinned:
            pinned = True

        # Generate embedding if not provided
        if embedding is None:
            embedding = await get_embedding(text.strip())

        memory = Memory(
            text=text.strip(),
            category=category,
            source=source,
            pinned=pinned,
            uses=0,
            conversation_id=conv_uuid,
            embedding=embedding,
        )
        db.add(memory)
        await db.flush()
        await db.refresh(memory)
        return memory

    async def get_all_memories(
        self,
        db: AsyncSession,
        category: Optional[str] = None,
        limit: int = 100,
        offset: int = 0,
    ) -> List[Memory]:
        """List all memories with optional category filter, newest first."""
        stmt = select(Memory).order_by(Memory.created_at.desc())

        if category and category in VALID_CATEGORIES:
            stmt = stmt.where(Memory.category == category)

        stmt = stmt.offset(offset).limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_memory_by_id(
        self, db: AsyncSession, memory_id: str
    ) -> Optional[Memory]:
        """Get a single memory by ID."""
        try:
            mid = uuid.UUID(str(memory_id))
        except (ValueError, AttributeError):
            return None

        stmt = select(Memory).where(Memory.id == mid)
        result = await db.execute(stmt)
        return result.scalar_one_or_none()

    async def update_memory(
        self,
        db: AsyncSession,
        memory_id: str,
        text: Optional[str] = None,
        category: Optional[str] = None,
    ) -> Optional[Memory]:
        """Update a memory's text and/or category. Regenerates embedding on text change."""
        memory = await self.get_memory_by_id(db, memory_id)
        if not memory:
            return None

        if text is not None and text.strip() != memory.text:
            memory.text = text.strip()
            # Regenerate embedding when text changes
            memory.embedding = await get_embedding(memory.text)

        if category is not None and category in VALID_CATEGORIES:
            memory.category = category

        memory.updated_at = datetime.utcnow()
        await db.flush()
        await db.refresh(memory)
        return memory

    async def delete_memory(self, db: AsyncSession, memory_id: str) -> bool:
        """Delete a memory by ID. Returns True if deleted."""
        memory = await self.get_memory_by_id(db, memory_id)
        if not memory:
            return False

        await db.delete(memory)
        await db.flush()
        return True

    async def pin_memory(
        self, db: AsyncSession, memory_id: str, pinned: bool
    ) -> Optional[Memory]:
        """Pin or unpin a memory. Pinned memories are always injected into the prompt."""
        memory = await self.get_memory_by_id(db, memory_id)
        if not memory:
            return None

        memory.pinned = pinned
        memory.updated_at = datetime.utcnow()
        await db.flush()
        await db.refresh(memory)
        return memory

    # ------------------------------------------------------------------
    # Hybrid retrieval (pgvector cosine + tsvector BM25 + recency)
    # ------------------------------------------------------------------

    async def _hybrid_scored(
        self,
        db: AsyncSession,
        query: str,
        category: Optional[str],
        top_k: int,
    ) -> List[Tuple[float, Memory]]:
        """Return [(score, memory), ...] sorted by score descending.

        Applies the hybrid formula:
            final = W_vec * vector_sim + W_bm25 * bm25_norm + W_rec * recency
            Gate:  drop if vector_sim < GATE_VEC AND bm25_norm < GATE_BM25
            Cutoff: keep only entries with final > CUTOFF

        Pinned memories are ALWAYS included with a high score (they bypass
        the gate/cutoff). This matches the "pinned = always in
        context" contract.
        """
        if not query or not query.strip():
            # No query — just return pinned memories with score 1.0
            stmt = select(Memory).where(Memory.pinned.is_(True))
            if category and category in VALID_CATEGORIES:
                stmt = stmt.where(Memory.category == category)
            result = await db.execute(stmt)
            pinned = list(result.scalars().all())
            return [(1.0, m) for m in pinned]

        # 1. Embed the query (may fail — fall back to BM25-only)
        query_embedding = await get_embedding(query.strip())

        # 2. Fetch candidate memories.
        # For correctness we need all memories in memory to compute BM25
        # normalization (IDF is corpus-wide). For typical scales (<10k
        # memories) this is fast enough.
        stmt = select(Memory)
        if category and category in VALID_CATEGORIES:
            stmt = stmt.where(Memory.category == category)

        result = await db.execute(stmt)
        all_memories = list(result.scalars().all())

        if not all_memories:
            return []

        # 3. Compute BM25-like score per memory using ts_rank_cd against
        # the query. We use PostgreSQL's websearch_to_tsquery which
        # handles quoted phrases and boolean operators cleanly.
        # We need to issue one query per memory to get ts_rank_cd, OR
        # compute it in SQL with a JOIN. Simpler: do it in Python by
        # calling the DB for each memory... but that's N queries.
        # Best: single SQL query that returns ts_rank_cd for all candidates.
        bm25_scores: dict[str, float] = {}  # memory_id_str -> raw bm25 score
        try:
            tsquery = " | ".join(
                # Split on whitespace, quote each token, join with OR so
                # any match contributes. Websearch syntax would also work
                # but plain OR is simpler (which sums per-token IDF).
                f"'{tok}'"
                for tok in re.findall(r"\w+", query.lower())
                if len(tok) >= 2
            )
            if tsquery:
                rank_stmt = text("""
                    SELECT id,
                           ts_rank_cd(search_vector, to_tsquery('simple', :q)) AS rank
                    FROM memories
                    WHERE search_vector @@ to_tsquery('simple', :q)
                    """).bindparams(q=tsquery)
                if category and category in VALID_CATEGORIES:
                    rank_stmt = rank_stmt.bindparams(cat=category)
                    rank_stmt = text("""
                        SELECT id,
                               ts_rank_cd(search_vector, to_tsquery('simple', :q)) AS rank
                        FROM memories
                        WHERE search_vector @@ to_tsquery('simple', :q)
                          AND category = :cat
                        """).bindparams(q=tsquery, cat=category)
                rank_result = await db.execute(rank_stmt)
                for row in rank_result:
                    bm25_scores[str(row[0])] = float(row[1] or 0.0)
        except Exception as e:
            print("[memory] BM25 query failed, using vector-only: %s", e)
            bm25_scores = {}

        # Normalize BM25 scores to 0..1
        max_bm25 = max(bm25_scores.values()) if bm25_scores else 0.0
        if max_bm25 <= 0:
            max_bm25 = 6.0  # default normalizer

        # 4. For each memory, compute the final hybrid score
        scored: List[Tuple[float, Memory]] = []
        now = datetime.utcnow()

        # Category boosts (default: identity 1.4, contact 1.3, preference 1.2)
        cat_boost = {
            "identity": 1.4,
            "contact": 1.3,
            "preference": 1.2,
            "goal": 1.1,
        }.get

        for m in all_memories:
            mid_str = str(m.id)

            # Pinned memories always pass — high score, skip gate/cutoff
            if m.pinned:
                scored.append((1.0, m))
                continue

            # Vector similarity (1 - cosine_distance)
            vs = 0.0
            if query_embedding is not None and m.embedding is not None:
                try:
                    # Use pgvector's cosine distance operator
                    dist_stmt = text(
                        "SELECT 1 - (embedding <=> CAST(:q AS vector)) AS sim "
                        "FROM memories WHERE id = CAST(:id AS uuid)"
                    ).bindparams(q=str(query_embedding), id=str(m.id))
                    dist_result = await db.execute(dist_stmt)
                    row = dist_result.first()
                    if row and row[0] is not None:
                        vs = max(0.0, float(row[0]))
                except Exception as e:
                    print(
                        "[memory] vector distance query failed for %s: %s",
                        mid_str,
                        e,
                    )

            # BM25 normalized to 0..1, with category boost
            raw_bm25 = bm25_scores.get(mid_str, 0.0)
            kw_norm = min(raw_bm25 / max_bm25, 1.0)
            boost = cat_boost(m.category, 1.0)
            kw_norm = min(kw_norm * boost, 1.0)

            # Recency: 1 / (1 + days_old * 0.05) — max 5% contribution
            try:
                days_old = (now - (m.created_at or now)).total_seconds() / 86400.0
            except Exception:
                days_old = 0.0
            recency = 1.0 / (1.0 + max(0.0, days_old) * 0.05)

            # Gate: need real relevance, not just recency
            if query_embedding is not None:
                if (
                    vs < settings.MEMORY_RETRIEVAL_GATE_VECTOR
                    and kw_norm < settings.MEMORY_RETRIEVAL_GATE_BM25
                ):
                    continue
                final = (
                    settings.MEMORY_RETRIEVAL_VECTOR_WEIGHT * vs
                    + settings.MEMORY_RETRIEVAL_BM25_WEIGHT * kw_norm
                    + settings.MEMORY_RETRIEVAL_RECENCY_WEIGHT * recency
                )
            else:
                # Vector store degraded — rely on BM25 only
                if kw_norm < settings.MEMORY_RETRIEVAL_GATE_BM25:
                    continue
                final = (
                    1.0 - settings.MEMORY_RETRIEVAL_RECENCY_WEIGHT
                ) * kw_norm + settings.MEMORY_RETRIEVAL_RECENCY_WEIGHT * recency

            # Cutoff
            if final <= settings.MEMORY_RETRIEVAL_CUTOFF:
                continue

            scored.append((final, m))

        # Sort by score descending
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[:top_k]

    async def get_relevant_memories(
        self,
        db: AsyncSession,
        query: str,
        top_k: int = 5,
    ) -> List[Memory]:
        """Get memories relevant to a query for system-prompt injection.

        Hybrid retrieval: pgvector cosine + tsvector BM25 + recency.
        Always includes pinned memories (they bypass the gate/cutoff).
        """
        print(f"[MemoryManager] get_relevant_memories query={query[:80]} top_k={top_k}")
        scored = await self._hybrid_scored(db, query, category=None, top_k=top_k)

        print(
            f"[MemoryManager] scored {len(scored)} candidates: {[(round(s, 3), m.text[:40]) for s, m in scored]}"
        )

        # Ensure pinned memories that didn't make the cutoff are still included.
        # (_hybrid_scored already pins them at score 1.0, but if top_k was
        # very small and there were many pinned memories, some may have been
        # trimmed. Re-add them here at the end.)
        if len(scored) < top_k:
            included_ids = {str(m.id) for _, m in scored}
            stmt = select(Memory).where(Memory.pinned.is_(True))
            result = await db.execute(stmt)
            for m in result.scalars().all():
                if str(m.id) not in included_ids:
                    scored.append((1.0, m))
                    if len(scored) >= top_k:
                        break

        # Re-sort: pinned first (score 1.0), then by score descending
        scored.sort(key=lambda x: (x[1].pinned, x[0]), reverse=True)

        print(
            "[MemoryManager] get_relevant_memories query=%r -> %d results: %s",
            query[:80],
            len(scored),
            [(round(s, 3), m.text[:40]) for s, m in scored],
        )
        return [m for _, m in scored[:top_k]]

    async def search_memories(
        self,
        db: AsyncSession,
        query: str,
        category: Optional[str] = None,
        limit: int = 20,
    ) -> List[Memory]:
        """Search memories via hybrid retrieval (used by Brain UI search box)."""
        scored = await self._hybrid_scored(db, query, category=category, top_k=limit)
        return [m for _, m in scored]

    # ------------------------------------------------------------------
    # Deduplication (3-tier: vector → exact match → Jaccard)
    # ------------------------------------------------------------------

    async def find_duplicates(
        self,
        db: AsyncSession,
        _text: str,
        threshold: Optional[float] = None,
    ) -> List[Memory]:
        """Find duplicate memories using the 3-tier chain.

        Tier 1 (vector): pgvector cosine similarity >= threshold (default 0.72).
        Tier 2 (exact):  case-insensitive text equality.
        Tier 3 (fuzzy):  Jaccard token similarity >= 0.6 (text fallback).

        Returns all matching memories (any tier). Empty list = no dupes.
        """
        if not _text or not _text.strip():
            return []

        threshold = threshold or settings.MEMORY_DEDUP_VECTOR_THRESHOLD
        text_lower = _text.strip().lower()

        # Tier 1: vector similarity
        vector_dup_ids: set[str] = set()
        query_embedding = await get_embedding(_text.strip())
        if query_embedding is not None:
            try:
                vec_stmt = text(
                    "SELECT id, 1 - (embedding <=> CAST(:q AS vector)) AS sim "
                    "FROM memories "
                    "WHERE embedding IS NOT NULL "
                    "  AND 1 - (embedding <=> CAST(:q AS vector)) >= :thr "
                    "ORDER BY sim DESC LIMIT 10"
                ).bindparams(q=str(query_embedding), thr=threshold)
                vec_result = await db.execute(vec_stmt)
                for row in vec_result:
                    vector_dup_ids.add(str(row[0]))
            except Exception as e:
                print(
                    "[memory] vector dedup query failed, falling back to text tiers: %s",
                    e,
                )

        # Fetch all memories (for tiers 2 & 3 — and to materialize tier 1)
        stmt = select(Memory)
        result = await db.execute(stmt)
        all_memories = list(result.scalars().all())

        duplicates: List[Memory] = []
        seen_ids: set[str] = set()
        for m in all_memories:
            mid_str = str(m.id)
            if mid_str in seen_ids:
                continue

            # Tier 1 match
            if mid_str in vector_dup_ids:
                duplicates.append(m)
                seen_ids.add(mid_str)
                continue

            # Tier 2: exact match
            if (m.text or "").lower() == text_lower:
                duplicates.append(m)
                seen_ids.add(mid_str)
                continue

            # Tier 3: Jaccard
            sim = get_text_similarity(_text, m.text or "")
            if sim >= settings.MEMORY_DEDUP_TEXT_THRESHOLD:
                duplicates.append(m)
                seen_ids.add(mid_str)

        return duplicates

    async def find_similar_by_vector(
        self,
        db: AsyncSession,
        _text: str,
        threshold: Optional[float] = None,
    ) -> Optional[Memory]:
        """Return the single most-similar memory by vector similarity, or None.

        Used by the extractor's Tier-1 dedup. Returns None if vector search
        is unavailable or no memory exceeds the threshold.
        """
        threshold = threshold or settings.MEMORY_DEDUP_VECTOR_THRESHOLD
        query_embedding = await get_embedding(_text.strip())
        if query_embedding is None:
            return None

        try:
            stmt = text(
                "SELECT id, 1 - (embedding <=> CAST(:q AS vector)) AS sim "
                "FROM memories "
                "WHERE embedding IS NOT NULL "
                "  AND 1 - (embedding <=> CAST(:q AS vector)) >= :thr "
                "ORDER BY sim DESC LIMIT 1"
            ).bindparams(q=str(query_embedding), thr=threshold)
            result = await db.execute(stmt)
            row = result.first()
            if not row:
                return None
            return await self.get_memory_by_id(db, str(row[0]))
        except Exception as e:
            print("[memory] find_similar_by_vector failed: %s", e)
            return None

    # ------------------------------------------------------------------
    # Usage counter
    # ------------------------------------------------------------------

    async def increment_uses(self, db: AsyncSession, memory_ids: List[str]) -> int:
        """Increment the usage counter for a list of memory IDs.

        Uses a single bulk UPDATE for efficiency.
        Returns the number of rows updated.
        """
        if not memory_ids:
            return 0
        uuids: List[uuid.UUID] = []
        for mid_str in memory_ids:
            try:
                uuids.append(uuid.UUID(str(mid_str)))
            except (ValueError, AttributeError):
                continue
        if not uuids:
            return 0

        stmt = update(Memory).where(Memory.id.in_(uuids)).values(uses=Memory.uses + 1)
        result = await db.execute(stmt)
        await db.flush()
        return result.rowcount or 0

    # ------------------------------------------------------------------
    # Categories
    # ------------------------------------------------------------------

    async def get_categories_with_counts(self, db: AsyncSession) -> List[dict]:
        """Get all categories with their memory counts."""
        stmt = select(Memory.category, func.count(Memory.id)).group_by(Memory.category)
        result = await db.execute(stmt)
        rows = result.all()

        categories = [{"category": cat, "count": cnt} for cat, cnt in rows]
        seen = {r[0] for r in rows}
        for cat in VALID_CATEGORIES:
            if cat not in seen:
                categories.append({"category": cat, "count": 0})

        return categories


# ---------------------------------------------------------------------------
# Module-level singleton + convenience function
# ---------------------------------------------------------------------------

_manager_instance: Optional[MemoryManager] = None


def _get_manager() -> MemoryManager:
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = MemoryManager()
    return _manager_instance


async def get_relevant_memories(
    db: AsyncSession, query: str, top_k: int = 5
) -> List[Memory]:
    """Module-level convenience wrapper for memory injection into the system prompt."""
    manager = _get_manager()
    return await manager.get_relevant_memories(db, query, top_k)
