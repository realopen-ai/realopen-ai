"""
Past-conversation transcript search service.

Uses PostgreSQL tsvector + ts_rank_cd (GIN-indexed) to find specific past
messages by keyword. Complements the cross-session SUMMARY injection
(which only sees 150-word summaries) by letting the LLM pull up exact
past turns when the user asks "what did I tell you about X last week?".

The 'simple' dictionary preserves non-English names (Clémence, Søren)
that 'english' would stem away.

Returns messages with their conversation title + timestamp + a snippet
of the matching text, so the LLM can cite the source conversation.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import List, Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[session_search] {formatted}", flush=True)


def _sanitize_tsquery(q: str) -> str:
    """Sanitize a free-text query into a safe tsquery.

    Splits on whitespace, keeps tokens >=2 chars, joins with OR (|) so
    any match contributes. Quotes are stripped — we don't support phrase
    queries from the LLM (it's not reliable enough to produce them).
    """
    tokens = re.findall(r"\w+", q.lower())
    tokens = [t for t in tokens if len(t) >= 2]
    if not tokens:
        return ""
    return " | ".join(f"'{t}'" for t in tokens)


async def search_past_messages(
    db: AsyncSession,
    query: str,
    limit: int = 10,
    exclude_conversation_id: Optional[str] = None,
) -> List[dict]:
    """Search past messages by keyword using tsvector + ts_rank_cd.

    Args:
        db: Async DB session.
        query: Free-text search query.
        limit: Max results to return.
        exclude_conversation_id: Optional conversation ID to exclude
            (typically the current conversation — don't search within it).

    Returns:
        List of dicts with: message_id, conversation_id, conversation_title,
        role, content_snippet, content_full, rank, created_at.
    """
    tsquery = _sanitize_tsquery(query)
    if not tsquery:
        return []

    try:
        # Build the query. We join messages → conversations to get the
        # title, and use ts_rank_cd for relevance ranking. The snippet
        # function highlights matching terms (though we strip the markers
        # for cleanliness).
        exclude_clause = ""
        params = {"q": tsquery, "lim": limit}
        if exclude_conversation_id:
            try:
                exclude_uuid = str(uuid.UUID(str(exclude_conversation_id)))
                exclude_clause = "AND m.conversation_id != CAST(:exc AS uuid)"
                params["exc"] = exclude_uuid
            except (ValueError, TypeError):
                pass

        sql = f"""
            SELECT
                m.id::text AS message_id,
                m.conversation_id::text AS conversation_id,
                COALESCE(c.title, 'Untitled') AS conversation_title,
                m.role,
                LEFT(m.content, 500) AS content_snippet,
                m.content AS content_full,
                ts_rank_cd(m.search_vector, to_tsquery('simple', :q)) AS rank,
                EXTRACT(EPOCH FROM m.created_at)::bigint AS created_at_epoch
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.search_vector @@ to_tsquery('simple', :q)
              {exclude_clause}
            ORDER BY rank DESC
            LIMIT :lim
        """

        result = await db.execute(text(sql).bindparams(**params))
        rows = result.all()

        out = []
        for row in rows:
            out.append(
                {
                    "message_id": row[0],
                    "conversation_id": row[1],
                    "conversation_title": row[2],
                    "role": row[3],
                    "content_snippet": row[4],
                    "content_full": row[5],
                    "rank": float(row[6]) if row[6] else 0.0,
                    "created_at": int(row[7]) if row[7] else 0,
                }
            )

        _log("search '%s' → %d results", query[:60], len(out))
        return out

    except Exception as e:
        _log("search failed: %s", e)
        # Fallback: ILIKE keyword search (no tsvector needed)
        return await _ilike_fallback(db, query, limit, exclude_conversation_id)


async def _ilike_fallback(
    db: AsyncSession,
    query: str,
    limit: int,
    exclude_conversation_id: Optional[str],
) -> List[dict]:
    """Fallback ILIKE search when tsvector unavailable.

    Extracts significant keywords (>=3 chars) and OR-joins them with ILIKE.
    Less precise than ts_rank_cd but works without the GIN index.
    """
    keywords = [w for w in re.findall(r"\w+", query.lower()) if len(w) >= 3][:5]
    if not keywords:
        return []

    try:
        # Build OR conditions with bind parameters (no direct string interpolation)
        like_clauses = []
        params = {"lim": limit}
        for i, kw in enumerate(keywords):
            param_name = f"kw{i}"
            like_clauses.append(f"LOWER(m.content) LIKE :{param_name}")
            params[param_name] = f"%{kw}%"
        conditions = " OR ".join(like_clauses)

        exclude_clause = ""
        if exclude_conversation_id:
            try:
                exclude_uuid = str(uuid.UUID(str(exclude_conversation_id)))
                exclude_clause = "AND m.conversation_id != CAST(:exc AS uuid)"
                params["exc"] = exclude_uuid
            except (ValueError, TypeError):
                pass

        sql = f"""
            SELECT
                m.id::text AS message_id,
                m.conversation_id::text AS conversation_id,
                COALESCE(c.title, 'Untitled') AS conversation_title,
                m.role,
                LEFT(m.content, 500) AS content_snippet,
                m.content AS content_full,
                0.0 AS rank,
                EXTRACT(EPOCH FROM m.created_at)::bigint AS created_at_epoch
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE ({conditions})
              {exclude_clause}
            ORDER BY m.created_at DESC
            LIMIT :lim
        """

        result = await db.execute(text(sql).bindparams(**params))
        rows = result.all()

        out = []
        for row in rows:
            out.append(
                {
                    "message_id": row[0],
                    "conversation_id": row[1],
                    "conversation_title": row[2],
                    "role": row[3],
                    "content_snippet": row[4],
                    "content_full": row[5],
                    "rank": float(row[6]) if row[6] else 0.0,
                    "created_at": int(row[7]) if row[7] else 0,
                }
            )
        _log("ILIKE fallback '%s' → %d results", query[:60], len(out))
        return out

    except Exception as e:
        _log("ILIKE fallback also failed: %s", e)
        return []


def format_results_for_llm(results: List[dict], query: str) -> str:
    """Format search results as a string for the LLM."""
    if not results:
        return f"No past conversations found matching '{query}'."

    lines = [f"Found {len(results)} matching past message(s):"]
    for i, r in enumerate(results, 1):
        snippet = r["content_snippet"]
        if len(snippet) > 300:
            snippet = snippet[:300] + "…"
        lines.append(
            f"\n[{i}] From '{r['conversation_title']}' "
            f"({r['role']}, rank={r['rank']:.3f}):\n"
            f"    {snippet}"
        )
    return "\n".join(lines)
