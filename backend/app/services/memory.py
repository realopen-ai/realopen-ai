"""
Memory management service for RealOpen-AI.

Provides CRUD operations and similarity-based retrieval for user memories
stored in PostgreSQL. Uses Jaccard similarity for dedup and retrieval;
pgvector support is included for future vector search enhancement.
"""

import logging
import re
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Memory

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tokenizer / Jaccard similarity
# ---------------------------------------------------------------------------

VALID_CATEGORIES = {"identity", "preference", "fact", "contact", "project", "goal"}
VALID_SOURCES = {"auto", "user", "ai_agent"}


def tokenize(text: str) -> List[str]:
    """Simple tokenizer that splits on whitespace and removes punctuation."""
    return [cleaned for word in text.split() if (cleaned := word.strip('.,!?";'))]


def get_text_similarity(text1: str, text2: str) -> float:
    """Calculate Jaccard similarity between two texts."""
    if not text1 or not text2:
        return 0.0

    tokens1 = set(tokenize(text1.lower()))
    tokens2 = set(tokenize(text2.lower()))

    if not tokens1 and not tokens2:
        return 1.0
    if not tokens1 or not tokens2:
        return 0.0

    intersection = tokens1.intersection(tokens2)
    union = tokens1.union(tokens2)

    return len(intersection) / len(union)


# ---------------------------------------------------------------------------
# MemoryManager — async, PostgreSQL-backed
# ---------------------------------------------------------------------------


class MemoryManager:
    """Manages memory entries in PostgreSQL with Jaccard similarity search."""

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
    ) -> Memory:
        """Add a new memory entry. Returns the created Memory object."""
        if not text or not text.strip():
            raise ValueError("Memory text cannot be empty")

        category = category if category in VALID_CATEGORIES else "fact"
        source = source if source in VALID_SOURCES else "auto"

        conv_uuid = None
        if conversation_id:
            try:
                conv_uuid = uuid.UUID(conversation_id)
            except (ValueError, AttributeError):
                conv_uuid = None

        # Auto-pin identity facts
        if category == "identity" and not pinned:
            pinned = True

        memory = Memory(
            text=text.strip(),
            category=category,
            source=source,
            pinned=pinned,
            uses=0,
            conversation_id=conv_uuid,
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
        """List all memories with optional category filter."""
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
            mid = uuid.UUID(memory_id)
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
        """Update a memory's text and/or category."""
        memory = await self.get_memory_by_id(db, memory_id)
        if not memory:
            return None

        if text is not None:
            memory.text = text.strip()
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
        """Pin or unpin a memory."""
        memory = await self.get_memory_by_id(db, memory_id)
        if not memory:
            return None

        memory.pinned = pinned
        memory.updated_at = datetime.utcnow()
        await db.flush()
        await db.refresh(memory)
        return memory

    # ------------------------------------------------------------------
    # Search & retrieval
    # ------------------------------------------------------------------

    async def search_memories(
        self,
        db: AsyncSession,
        query: str,
        category: Optional[str] = None,
        limit: int = 20,
    ) -> List[Memory]:
        """Search memories using Jaccard similarity."""
        stmt = select(Memory)
        if category and category in VALID_CATEGORIES:
            stmt = stmt.where(Memory.category == category)

        result = await db.execute(stmt)
        all_memories = list(result.scalars().all())

        if not query or not query.strip():
            return all_memories[:limit]

        scored = []
        for m in all_memories:
            sim = get_text_similarity(query, m.text)
            # Exact substring match gets a boost
            if query.lower() in m.text.lower():
                sim = max(sim, 0.8)
            if sim >= 0.05:
                scored.append((sim, m))

        scored.sort(key=lambda x: x[0], reverse=True)
        return [m for _, m in scored[:limit]]

    async def get_relevant_memories(
        self,
        db: AsyncSession,
        query: str,
        top_k: int = 8,
    ) -> List[Memory]:
        """Get memories relevant to a query for context injection.

        Uses keyword-boosted Jaccard similarity approach.
        Always includes pinned memories.
        """
        # Fetch all memories (pinned ones are always included)
        stmt = select(Memory)
        result = await db.execute(stmt)
        all_memories = list(result.scalars().all())

        if not all_memories or not query.strip():
            # Return just pinned memories if no query
            return [m for m in all_memories if m.pinned][:top_k]

        # Define keyword categories for semantic matching
        identity_words = [
            "name",
            "who",
            "i",
            "am",
            "called",
            "identity",
            "myself",
            "me",
            "my",
        ]
        contact_words = [
            "phone",
            "email",
            "address",
            "contact",
            "number",
            "where",
            "located",
            "reach",
        ]
        preference_words = [
            "like",
            "prefer",
            "favorite",
            "want",
            "love",
            "hate",
            "dislike",
            "enjoy",
            "interested",
        ]
        task_words = [
            "todo",
            "task",
            "remind",
            "meeting",
            "appointment",
            "schedule",
            "deadline",
        ]
        fact_words = [
            "what",
            "when",
            "where",
            "how",
            "why",
            "explain",
            "describe",
            "information",
            "know",
        ]

        query_lower = query.lower()

        # Determine query type based on keywords
        query_type = None
        if any(word in query_lower for word in identity_words):
            query_type = "identity"
        elif any(word in query_lower for word in contact_words):
            query_type = "contact"
        elif any(word in query_lower for word in preference_words):
            query_type = "preference"
        elif any(word in query_lower for word in task_words):
            query_type = "task"
        elif any(word in query_lower for word in fact_words):
            query_type = "fact"

        relevant = []
        identity_memories = []
        other_memories = []

        # Separate identity memories from others
        for memory in all_memories:
            memory_text = memory.text.lower()
            is_identity = any(
                [
                    re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", memory.text),
                    any(
                        word in memory_text
                        for word in [
                            "name is",
                            "i'm",
                            "i am",
                            "called",
                            "my name",
                            "named",
                            "call me",
                        ]
                    ),
                ]
            )
            if is_identity:
                identity_memories.append(memory)
            else:
                other_memories.append(memory)

        # For identity queries, include all identity memories regardless of similarity
        if query_type == "identity" and identity_memories:
            for memory in identity_memories:
                relevant.append((0.9, memory))

        # Process other memories with similarity scoring
        for memory in other_memories:
            memory_text = memory.text.lower()
            memory_tokens = set(tokenize(memory_text))
            query_tokens = set(tokenize(query_lower))

            if not query_tokens or not memory_tokens:
                continue

            base_similarity = len(query_tokens & memory_tokens) / len(
                query_tokens | memory_tokens
            )
            final_score = base_similarity

            # Apply boosts based on semantic matching
            if query_type == "contact":
                has_contact_info = any(
                    word in memory_text
                    for word in [
                        "@gmail.com",
                        "@",
                        ".com",
                        "phone",
                        "number",
                        "address",
                        "http",
                        "www",
                        "tel:",
                    ]
                )
                if has_contact_info:
                    final_score *= 1.4

            elif query_type == "preference":
                has_preference = any(
                    word in memory_text
                    for word in [
                        "like",
                        "love",
                        "hate",
                        "dislike",
                        "prefer",
                        "favorite",
                        "enjoy",
                        "interested",
                    ]
                )
                if has_preference:
                    final_score *= 1.3

            elif query_type == "task":
                has_task = any(
                    word in memory_text
                    for word in [
                        "todo",
                        "task",
                        "remind",
                        "meeting",
                        "appointment",
                        "schedule",
                        "deadline",
                        "need to",
                    ]
                )
                if has_task:
                    final_score *= 1.3

            # Always consider exact phrase matches as highly relevant
            if query.lower() in memory.text.lower():
                final_score = max(final_score, 0.8)

            # Include memory if it meets threshold after boosts
            if final_score >= 0.05:
                relevant.append((final_score, memory))

        # Always include pinned memories (they may have been filtered out)
        # pinned_ids = {m.id for m in all_memories if m.pinned}
        included_ids = {m.id for _, m in relevant}
        for m in all_memories:
            if m.pinned and m.id not in included_ids:
                relevant.append((1.0, m))

        # Sort by final score (descending) and return top matches
        relevant.sort(key=lambda x: x[0], reverse=True)
        print(
            f"[memory-manager] get_relevant_memories: query='{query}' -> {len(relevant)} relevant memories found"
        )
        print(
            f"[memory-manager] Top relevant memories: {[m.text for _, m in relevant[:top_k]]}"
        )
        return [mem for _, mem in relevant[:top_k]]

    async def find_duplicates(
        self,
        db: AsyncSession,
        text: str,
        threshold: float = 0.6,
    ) -> List[Memory]:
        """Check for duplicate memories using Jaccard similarity.

        Returns a list of memories that exceed the similarity threshold.
        """
        stmt = select(Memory)
        result = await db.execute(stmt)
        all_memories = list(result.scalars().all())

        text_lower = text.strip().lower()
        # First check exact match
        for m in all_memories:
            if m.text.lower() == text_lower:
                return [m]

        # Then check fuzzy Jaccard similarity
        new_tokens = set(text_lower.split())
        if not new_tokens:
            return []

        duplicates = []
        for m in all_memories:
            old_tokens = set(m.text.lower().split())
            if not old_tokens:
                continue
            intersection = new_tokens & old_tokens
            union = new_tokens | old_tokens
            if len(intersection) / len(union) >= threshold:
                duplicates.append(m)

        return duplicates

    async def increment_uses(self, db: AsyncSession, memory_ids: List[str]) -> int:
        """Increment the usage counter for a list of memory IDs."""
        count = 0
        for mid_str in memory_ids:
            try:
                mid = uuid.UUID(mid_str)
            except (ValueError, AttributeError):
                continue

            stmt = select(Memory).where(Memory.id == mid)
            result = await db.execute(stmt)
            memory = result.scalar_one_or_none()
            if memory:
                memory.uses = (memory.uses or 0) + 1
                count += 1

        if count:
            await db.flush()
        return count

    async def get_categories_with_counts(self, db: AsyncSession) -> List[dict]:
        """Get all categories with their memory counts."""
        stmt = select(Memory.category, func.count(Memory.id)).group_by(Memory.category)
        result = await db.execute(stmt)
        rows = result.all()

        categories = []
        for cat, count in rows:
            categories.append({"category": cat, "count": count})

        # Ensure all known categories are represented
        seen = {r[0] for r in rows}
        for cat in VALID_CATEGORIES:
            if cat not in seen:
                categories.append({"category": cat, "count": 0})

        return categories


# ---------------------------------------------------------------------------
# Module-level convenience function for memory injection
# ---------------------------------------------------------------------------

_manager_instance: Optional[MemoryManager] = None


def _get_manager() -> MemoryManager:
    global _manager_instance
    if _manager_instance is None:
        _manager_instance = MemoryManager()
    return _manager_instance


async def get_relevant_memories(
    db: AsyncSession, query: str, top_k: int = 8
) -> List[Memory]:
    """Get relevant memories for context injection (module-level convenience)."""
    manager = _get_manager()
    return await manager.get_relevant_memories(db, query, top_k)
