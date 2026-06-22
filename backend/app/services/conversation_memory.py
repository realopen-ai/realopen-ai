"""
Conversation Memory Service.

Provides cross-session context continuity:
1. Stores conversation summaries with vector embeddings
2. Retrieves relevant past conversations for new sessions
3. Auto-triggers summarization when conversations reach thresholds
"""

import logging
import uuid
from datetime import datetime
from typing import Any, List, Optional

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Message
from app.services.embeddings import get_embedding
from app.services.context_compactor import summarize_conversation

logger = logging.getLogger(__name__)


def _log(msg: str, *args: Any) -> None:
    """Always-visible print() logger.

    Uses print() with flush=True so output appears immediately in the
    container logs — no DEBUG flag needed.
    """
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[conversation_memory] {formatted}", flush=True)


async def _store_summary(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    summary: str,
) -> bool:
    """Store a conversation summary and its embedding."""
    try:
        conv = await db.get(Conversation, conversation_id)
        if not conv:
            return False

        conv.summary = summary
        conv.summary_at = datetime.utcnow()

        # Generate embedding for vector search
        embedding = await get_embedding(summary)
        if embedding:
            conv.summary_embedding = embedding

        await db.flush()
        _log("Stored summary for conversation %s", conversation_id)
        return True
    except Exception as e:
        logger.warning("Failed to store summary: %s", e)
        _log("Failed to store summary for conversation %s: %s", conversation_id, e)
        return False


async def maybe_summarize_conversation(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    model: str,
    min_messages: int = 8,
) -> bool:
    """Summarize a conversation if it has enough messages and no existing summary.

    Called after each assistant response. Only runs when:
    - Conversation has >= min_messages messages
    - No existing summary (or summary is >1 day old)
    - Conversation is not already summarized recently
    """
    try:
        conv = await db.get(Conversation, conversation_id)
        if not conv:
            return False

        # Skip if already summarized recently (within 1 hour)
        if conv.summary and conv.summary_at:
            age_seconds = (datetime.utcnow() - conv.summary_at).total_seconds()
            if age_seconds < 3600:
                return False

        # Count messages
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .limit(min_messages + 1)
        )
        result = await db.execute(stmt)
        messages = list(result.scalars().all())

        if len(messages) < min_messages:
            return False

        # Convert to dicts for the summarizer
        message_dicts = [{"role": m.role, "content": m.content} for m in messages]

        summary = await summarize_conversation(message_dicts, model)
        if summary:
            return await _store_summary(db, conversation_id, summary)

        return False
    except Exception as e:
        logger.warning("maybe_summarize_conversation failed: %s", e)
        _log(
            "maybe_summarize_conversation failed for conversation %s: %s",
            conversation_id,
            e,
        )
        return False


async def get_recent_summaries(
    db: AsyncSession,
    limit: int = 10,
    exclude_conversation_id: Optional[uuid.UUID] = None,
) -> List[dict]:
    """Get recent conversation summaries, newest first.

    Excludes a specific conversation (current one) to avoid showing
    the agent its own conversation as "past context."
    """
    try:
        stmt = (
            select(Conversation)
            .where(
                Conversation.summary.isnot(None),
                Conversation.summary != "",
            )
            .order_by(Conversation.summary_at.desc())
            .limit(limit)
        )
        if exclude_conversation_id:
            stmt = stmt.where(Conversation.id != exclude_conversation_id)

        result = await db.execute(stmt)
        conversations = result.scalars().all()

        return [
            {
                "id": str(c.id),
                "title": c.title or "Untitled",
                "summary": c.summary,
                "summary_at": int(c.summary_at.timestamp()) if c.summary_at else 0,
                "model": c.model,
            }
            for c in conversations
        ]
    except Exception as e:
        logger.warning("get_recent_summaries failed: %s", e)
        _log(
            "get_recent_summaries failed (limit=%d, exclude=%s): %s",
            limit,
            exclude_conversation_id,
            e,
        )
        return []


async def get_relevant_summaries(
    db: AsyncSession,
    query: str,
    exclude_conversation_id: Optional[uuid.UUID] = None,
    top_k: int = 3,
    threshold: float = 0.4,
) -> List[dict]:
    """Find conversation summaries relevant to the current user query.

    Uses pgvector cosine similarity on summary embeddings.
    Falls back to keyword matching (ILIKE) if vector search unavailable.
    """
    try:
        query_embedding = await get_embedding(query)
        if query_embedding is None:
            return await _keyword_summary_search(
                db, query, exclude_conversation_id, top_k
            )

        # Vector search on summary embeddings
        vec_stmt = text("""
            SELECT id, title, summary,
                   EXTRACT(EPOCH FROM summary_at)::bigint AS summary_at_epoch,
                   model,
                   1 - (summary_embedding <=> CAST(:q AS vector)) AS sim
            FROM conversations
            WHERE summary_embedding IS NOT NULL
              AND summary IS NOT NULL
              AND summary != ''
              AND 1 - (summary_embedding <=> CAST(:q AS vector)) >= :thr
            ORDER BY sim DESC
            LIMIT :lim
        """).bindparams(
            q=str(query_embedding),
            thr=threshold,
            lim=top_k * 2 if exclude_conversation_id else top_k,
        )

        result = await db.execute(vec_stmt)
        rows = result.all()

        summaries = []
        for row in rows:
            cid = str(row[0])
            if exclude_conversation_id and uuid.UUID(cid) == exclude_conversation_id:
                continue
            summaries.append(
                {
                    "id": cid,
                    "title": row[1] or "Untitled",
                    "summary": row[2],
                    "summary_at": int(row[3]) if row[3] else 0,
                    "model": row[4],
                    "score": float(row[5]) if row[5] else 0.0,
                }
            )
            if len(summaries) >= top_k:
                break

        return summaries
    except Exception as e:
        logger.warning("Vector summary search failed, using keyword fallback: %s", e)
        _log("Vector summary search failed, using keyword fallback: %s", e)
        return await _keyword_summary_search(db, query, exclude_conversation_id, top_k)


async def _keyword_summary_search(
    db: AsyncSession,
    query: str,
    exclude_conversation_id: Optional[uuid.UUID] = None,
    limit: int = 3,
) -> List[dict]:
    """Fallback: keyword search on conversation summaries."""
    try:
        # Extract significant keywords from query (words >= 3 chars)
        keywords = [w for w in query.lower().split() if len(w) >= 3][:5]
        if not keywords:
            return []

        # Build ILIKE conditions
        from sqlalchemy import or_

        conditions = [Conversation.summary.ilike(f"%{kw}%") for kw in keywords]
        stmt = (
            select(Conversation)
            .where(
                Conversation.summary.isnot(None),
                Conversation.summary != "",
                or_(*conditions) if conditions else True,
            )
            .order_by(Conversation.summary_at.desc())
            .limit(limit * 2)
        )
        if exclude_conversation_id:
            stmt = stmt.where(Conversation.id != exclude_conversation_id)

        result = await db.execute(stmt)
        conversations = result.scalars().all()

        summaries = []
        for c in conversations:
            if len(summaries) >= limit:
                break
            summaries.append(
                {
                    "id": str(c.id),
                    "title": c.title or "Untitled",
                    "summary": c.summary,
                    "summary_at": int(c.summary_at.timestamp()) if c.summary_at else 0,
                    "model": c.model,
                    "score": 0.0,
                }
            )

        return summaries
    except Exception as e:
        logger.warning("Keyword summary search failed: %s", e)
        _log("Keyword summary search failed: %s", e)
        return []


async def build_cross_session_context(
    db: AsyncSession,
    user_message: str,
    conversation_id: Optional[uuid.UUID] = None,
    max_summaries: int = 2,
) -> str:
    """Build a cross-session context block for system prompt injection.

    Finds relevant past conversation summaries and formats them compactly.
    Designed for small local models — keeps it brief.
    """
    conv_uuid = uuid.UUID(str(conversation_id)) if conversation_id else None

    # Try vector search first
    summaries = await get_relevant_summaries(
        db,
        user_message,
        exclude_conversation_id=conv_uuid,
        top_k=max_summaries,
    )

    # If vector search returned nothing, try recent summaries
    if not summaries:
        recent = await get_recent_summaries(
            db,
            limit=max_summaries,
            exclude_conversation_id=conv_uuid,
        )
        summaries = recent[:max_summaries]

    if not summaries:
        return ""

    lines = ["\n\n## Previous Conversations (for context)"]
    for s in summaries:
        title = s.get("title", "Untitled")
        summary_text = s.get("summary", "")
        # Truncate for small models
        words = summary_text.split()
        short = " ".join(words[:80])
        if len(words) > 80:
            short += "..."
        lines.append(f"- [{title}]: {short}")

    ctx = "\n".join(lines)
    _log("Cross-session context: %d summaries, %d chars", len(summaries), len(ctx))
    return ctx
