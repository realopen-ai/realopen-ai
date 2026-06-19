"""
RAG search tool — lets the agent pull relevant document chunks on demand.

Why a tool (not always-inject):
  Always-injecting retrieved chunks into the system prompt burns tokens on
  every turn, even when the user just says "hi". By exposing RAG as a tool
  the LLM decides when it actually needs to ground its answer in the
  knowledge base — same pattern as web search, vision, etc.

The tool:
  1. Receives a `query` from the LLM (the question to ground).
  2. Calls app.services.rag.search_documents with the active conversation_id
     so private docs tied to this conversation are included alongside
     public docs.
  3. Returns the chunks as a formatted string for the LLM (with [Source N]
     markers) AND stashes the RetrievedSource list on the tool call so the
     SSE layer can emit a `rag_sources` event for the frontend to render
     collapsible source cards under the assistant message.

The conversation_id is threaded in via the `conversation_id` kwarg — the
chat API sets it on the tool call args before execution (see
app/api/chat.py).
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Optional

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.db.session import async_session_factory
from app.services import rag as rag_service

logger = logging.getLogger(__name__)


class RagSearchTool(BaseTool):
    """Search the user's document knowledge base (public + this conversation's private docs)."""

    name = "rag_search"
    description = (
        "Search the user's uploaded document knowledge base for information "
        "relevant to a question. Use this when the user asks about content "
        "that might be in documents they've shared with you — PDFs, DOCX, "
        "spreadsheets, text files, etc. Returns matching excerpts with "
        "source citations (document name, page, line range). "
        "Args: {query: string (the question or search terms)}"
    )
    tool_type = ToolType.FILE_READ  # reuse the "file" tool type — closest match
    # in the existing enum. The frontend treats it like a file-read badge.

    async def execute(
        self, *, query: str, conversation_id: Optional[str] = None, **kwargs
    ) -> ToolResult:
        """Run the RAG search and return formatted results for the LLM.

        Args:
            query: The search query (typically the user's question or a
                   reformulation of it).
            conversation_id: The active conversation ID (string UUID). The
                             chat API injects this so private docs tied to
                             this conversation are searchable. Optional —
                             if absent, only public docs are searched.

        Returns:
            ToolResult with:
              - output: formatted string for the LLM with [Source N] markers
              - tool_call.rag_sources: list of RetrievedSource dicts, used
                by the SSE layer to emit a `rag_sources` event for the
                frontend's collapsible source cards.
        """
        start = time.time()
        tool_call = ToolCall(
            id=f"tc-rag-{int(start * 1000)}",
            type=self.tool_type,
            name=self.name,
            status="running",
            title="Searching documents",
            query=query,
            started_at=start,
        )

        if not query or not query.strip():
            tool_call.status = "completed"
            tool_call.completed_at = time.time()
            return ToolResult(
                success=True,
                output="No query provided — nothing to search.",
                tool_call=tool_call,
            )

        # Parse conversation_id (it arrives as a string from the LLM/tool args)
        conv_uuid: Optional[uuid.UUID] = None
        if conversation_id:
            try:
                conv_uuid = uuid.UUID(str(conversation_id))
            except (ValueError, TypeError):
                logger.warning(
                    "[rag_tool] invalid conversation_id '%s' — searching public docs only",
                    conversation_id,
                )
                conv_uuid = None

        try:
            async with async_session_factory() as db:
                sources = await rag_service.search_documents(
                    db,
                    query=query,
                    conversation_id=conv_uuid,
                )
        except Exception as e:
            logger.exception("[rag_tool] search failed: %s", e)
            tool_call.status = "error"
            tool_call.completed_at = time.time()
            tool_call.error = str(e)
            return ToolResult(
                success=False,
                output=f"Document search failed: {e}",
                tool_call=tool_call,
            )

        # Stash the sources on the tool_call so the SSE layer can emit a
        # rag_sources event for the frontend. We piggy-back on the
        # `output` field for the LLM-facing text, and put the structured
        # sources in a separate attribute.
        formatted = rag_service.format_sources_for_llm(sources)
        source_dicts = [rag_service.retrieved_source_to_dict(s) for s in sources]

        tool_call.status = "completed"
        tool_call.completed_at = time.time()
        tool_call.output = formatted
        # Custom field — the agent service reads this and emits rag_sources.
        # ToolCall is a dataclass so we can attach ad-hoc attributes.
        tool_call.rag_sources = source_dicts  # type: ignore[attr-defined]

        logger.info(
            "[rag_tool] query='%s' conv=%s → %d sources in %.2fs",
            query[:80],
            conv_uuid,
            len(sources),
            time.time() - start,
        )

        return ToolResult(
            success=True,
            output=formatted,
            tool_call=tool_call,
        )


# Register at import time — main.py imports app.agent.tools which triggers this.
tool_registry.register(RagSearchTool())
logger.info("[rag_tool] RagSearchTool registered")
