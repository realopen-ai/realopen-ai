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
from typing import Any, Dict, List, Optional

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.agent.tools.config_base import (
    ConfigField,
    ToolConfigDefinition,
    register_config,
)
from app.db.session import async_session_factory
from app.services.session_search import (
    search_past_messages,
    format_results_for_llm,
)

logger = logging.getLogger(__name__)

# Fallback result cap when the persisted configuration predates the
# setting — the actual value lives in the tool config (Brain ▸ Tools).
DEFAULT_MAX_RESULTS = 10


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[search_past_conv] {formatted}", flush=True)


class SearchPastConversationsTool(BaseTool):
    """Search past conversation transcripts by keyword."""

    name = "search_past_conversations"
    display_name = "Past Conversation Search"
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
                    limit=_configured_max_results(),
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


# ── Configuration definition (Brain ▸ Tools ▸ Past Conversation Search) ──


def _configured_max_results() -> int:
    """The persisted result cap (custom.max_results)."""
    from app.agent.tools import config_store

    cfg = config_store.get_tool_config("search_past_conversations") or {}
    try:
        value = int((cfg.get("custom") or {}).get("max_results", DEFAULT_MAX_RESULTS))
    except (TypeError, ValueError):
        return DEFAULT_MAX_RESULTS
    return max(1, min(50, value))


def _validate_spc_custom(custom: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the custom settings; returns the cleaned object."""
    if not isinstance(custom, dict):
        raise ValueError("'custom' must be an object")
    if "max_results" in custom:
        try:
            v = float(custom["max_results"])
        except (TypeError, ValueError):
            raise ValueError("'max_results' must be a number")
        if v <= 0 or v > 50:
            raise ValueError("'max_results' must be between 1 and 50")
        custom["max_results"] = int(v)
    return custom


SPC_CONFIG = ToolConfigDefinition(
    tool_name="search_past_conversations",
    display_name="Past Conversation Search",
    description=(
        "Search the user's PAST conversation transcripts (not summaries — the actual "
        "messages) by keyword. Use when the user asks about something discussed "
        "previously ('what did I say about X?', 'show me the code from last week'). "
        "Returns matching messages with their source conversation title."
    ),
    custom_defaults={"max_results": DEFAULT_MAX_RESULTS},
    custom_schema=[
        {
            "key": "results",
            "label": "Results",
            "fields": [
                ConfigField(
                    "max_results",
                    "Max results",
                    "int",
                    default=DEFAULT_MAX_RESULTS,
                    help="How many matching messages to return to the model",
                ).to_dict(),
            ],
        },
    ],
    validate_custom=_validate_spc_custom,
)

register_config(SPC_CONFIG)


# Register the tool
tool_registry.register(SearchPastConversationsTool())
logger.info("[search_past_conv] SearchPastConversationsTool registered")
