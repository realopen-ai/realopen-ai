"""
search_past_conversations tool — lets the LLM search past conversation transcripts.

WHY A TOOL:
The cross-session context injection only sees 150-word SUMMARIES of past
conversations — it can't pull up specific past turns. When the user asks
"what did I tell you about X last week?" or "show me the code we wrote
for Y", the summary doesn't have enough detail. This tool lets the LLM
search the actual message transcripts by keyword.

The tool uses the tsvector + GIN index on messages.content (created in
migration f1a5b3c9d2e7) for fast BM25-ranked search, with an ILIKE
fallback if the index is unavailable.

Returns formatted results with conversation title, role, snippet, and
rank so the LLM can cite the source conversation.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.db.session import async_session_factory
from app.services.session_search import (
    search_past_messages,
    format_results_for_llm,
)

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[search_past_conv] {formatted}", flush=True)


class SearchPastConversationsTool(BaseTool):
    """Search past conversation transcripts by keyword."""

    name = "search_past_conversations"
    description = (
        "Search the user's PAST conversation transcripts (not summaries — the actual "
        "messages) by keyword. Use when the user asks about something discussed "
        "previously ('what did I say about X?', 'show me the code from last week'). "
        "Returns matching messages with their source conversation title."
    )
    tool_type = ToolType.FILE_READ  # reads from the message store

    param_aliases = {
        "query": "query",
        "search": "query",
        "q": "query",
        "question": "query",
    }

    def get_parameters(self) -> dict:
        return {
            "query": {
                "type": "string",
                "description": "Search query — keywords to find in past messages",
            },
        }

    def get_required_params(self) -> List[str]:
        return ["query"]

    async def execute(
        self, *, query: str, conversation_id: Optional[str] = None, **kwargs
    ) -> ToolResult:
        """Search past messages and return formatted results for the LLM."""
        start = time.time()
        tool_call = ToolCall(
            id=f"tc-spc-{int(start * 1000)}",
            type=self.tool_type,
            name=self.name,
            status="running",
            title="Searching past conversations",
            query=query,
            started_at=start,
        )

        if not query or not query.strip():
            tool_call.status = "completed"
            tool_call.completed_at = time.time()
            return ToolResult(
                success=True,
                output="No search query provided.",
                tool_call=tool_call,
            )

        _log("execute START query=%r conv=%s", query[:80], conversation_id)

        try:
            async with async_session_factory() as db:
                results = await search_past_messages(
                    db,
                    query=query,
                    limit=10,
                    exclude_conversation_id=conversation_id,
                )
        except Exception as e:
            _log("search FAILED: %s", e)
            tool_call.status = "error"
            tool_call.completed_at = time.time()
            tool_call.error = str(e)
            return ToolResult(
                success=False,
                output=f"Past conversation search failed: {e}",
                tool_call=tool_call,
            )

        formatted = format_results_for_llm(results, query)

        tool_call.status = "completed"
        tool_call.completed_at = time.time()
        tool_call.output = formatted

        _log(
            "execute DONE query=%r results=%d time=%.2fs",
            query[:80],
            len(results),
            time.time() - start,
        )

        return ToolResult(
            success=True,
            output=formatted,
            tool_call=tool_call,
        )


# Register the tool
tool_registry.register(SearchPastConversationsTool())
logger.info("[search_past_conv] SearchPastConversationsTool registered")
