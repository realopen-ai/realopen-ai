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
from typing import Optional, List

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.agent.tools.config_base import (
    ConfigField,
    ToolConfigDefinition,
    register_config,
)
from app.db.session import async_session_factory
from app.services import rag as rag_service

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    """Always-visible print() logger for the RAG tool."""
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[rag_tool] {formatted}", flush=True)


class RagSearchTool(BaseTool):
    """Search the user's document knowledge base (public + this conversation's private docs)."""

    name = "rag_search"
    display_name = "Document Search (RAG)"
    description = (
        "Search the user's knowledge base of uploaded documents (PDFs, DOCX, text files, "
        "spreadsheets) for information. Use PROACTIVELY when the user has uploaded documents "
        "and asks about their content — retrieve relevant excerpts BEFORE answering so you "
        "can ground your response in the actual document text."
    )
    tool_type = ToolType.FILE_READ

    param_aliases = {
        "query": "query",
        "search": "query",
        "question": "query",
        "q": "query",
    }

    def get_parameters(self) -> dict:
        return {
            "query": {
                "type": "string",
                "description": "Search query to find relevant document excerpts",
            },
        }

    def get_required_params(self) -> List[str]:
        return ["query"]

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
            _log("empty query — nothing to search")
            return ToolResult(
                success=True,
                output="No query provided.",
                tool_call=tool_call,
            )

        _log("execute START  query=%r  conv=%s", query[:80], conversation_id)

        # Parse conversation_id (it arrives as a string from the LLM/tool args)
        conv_uuid: Optional[uuid.UUID] = None
        if conversation_id:
            try:
                conv_uuid = uuid.UUID(str(conversation_id))
            except (ValueError, TypeError):
                _log(
                    "invalid conversation_id '%s' — searching public docs only",
                    conversation_id,
                )
                conv_uuid = None

        try:
            # Retrieval knobs come from the persisted tool configuration
            # (Brain ▸ Tools ▸ Document Search); defaults mirror the
            # global RAG_* env settings.
            top_k_per_doc, top_k_total, cutoff = _configured_retrieval()
            async with async_session_factory() as db:
                sources = await rag_service.search_documents(
                    db,
                    query=query,
                    conversation_id=conv_uuid,
                    top_k_per_doc=top_k_per_doc,
                    top_k_total=top_k_total,
                    similarity_cutoff=cutoff,
                )
        except Exception as e:
            _log("search FAILED: %s", e)
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

        _log(
            "execute DONE  query=%r  conv=%s  sources=%d  time=%.2fs",
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


# ── Configuration definition (Brain ▸ Tools ▸ Document Search) ────


def _configured_retrieval() -> tuple:
    """The persisted retrieval settings, or the global defaults.

    Returns (top_k_per_doc, top_k_total, similarity_cutoff) — any of
    them None (unset) falls back to the global RAG_* settings inside
    rag_service.search_documents.
    """
    from app.agent.tools import config_store
    from app.config import settings as app_settings

    cfg = config_store.get_tool_config("rag_search") or {}
    custom = cfg.get("custom") or {}

    def _int(key):
        value = custom.get(key)
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _float(key):
        value = custom.get(key)
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    top_k_per_doc = _int("top_k_per_doc") or app_settings.RAG_TOP_K_PER_DOC
    top_k_total = _int("top_k_total") or app_settings.RAG_TOP_K_TOTAL
    cutoff = _float("similarity_cutoff")
    if cutoff is None:
        cutoff = app_settings.RAG_SIMILARITY_CUTOFF
    return top_k_per_doc, top_k_total, cutoff


def _validate_rag_custom(custom: dict) -> dict:
    """Validate the custom settings; returns the cleaned object."""
    if not isinstance(custom, dict):
        raise ValueError("'custom' must be an object")
    for key, lo, hi in (
        ("top_k_per_doc", 1, 20),
        ("top_k_total", 1, 50),
    ):
        if key in custom and custom[key] is not None:
            try:
                v = int(custom[key])
            except (TypeError, ValueError):
                raise ValueError(f"'{key}' must be a number")
            if v < lo or v > hi:
                raise ValueError(f"'{key}' must be between {lo} and {hi}")
            custom[key] = v
    if "similarity_cutoff" in custom and custom["similarity_cutoff"] is not None:
        try:
            v = float(custom["similarity_cutoff"])
        except (TypeError, ValueError):
            raise ValueError("'similarity_cutoff' must be a number")
        if v < 0 or v > 1:
            raise ValueError("'similarity_cutoff' must be between 0 and 1")
        custom["similarity_cutoff"] = v
    return custom


RAG_SEARCH_CONFIG = ToolConfigDefinition(
    tool_name="rag_search",
    display_name="Document Search (RAG)",
    description=(
        "Search the user's knowledge base of uploaded documents (PDFs, DOCX, text files, "
        "spreadsheets) for information. Use PROACTIVELY when the user has uploaded documents "
        "and asks about their content — retrieve relevant excerpts BEFORE answering so you "
        "can ground your response in the actual document text."
    ),
    custom_defaults={
        "top_k_per_doc": 3,
        "top_k_total": 8,
        "similarity_cutoff": 0.20,
    },
    custom_schema=[
        {
            "key": "retrieval",
            "label": "Retrieval",
            "fields": [
                ConfigField(
                    "top_k_per_doc",
                    "Chunks per document",
                    "int",
                    default=3,
                    help="How many excerpts to keep from each matched document",
                ).to_dict(),
                ConfigField(
                    "top_k_total",
                    "Total chunks",
                    "int",
                    default=8,
                    help="Overall cap on excerpts returned to the model",
                ).to_dict(),
                ConfigField(
                    "similarity_cutoff",
                    "Similarity cutoff",
                    "float",
                    default=0.20,
                    help=(
                        "Minimum similarity (0–1) for an excerpt to count "
                        "as a match — higher is stricter"
                    ),
                ).to_dict(),
            ],
        },
    ],
    validate_custom=_validate_rag_custom,
)

register_config(RAG_SEARCH_CONFIG)


# Register the tool
tool_registry.register(RagSearchTool())
logger.info("[rag_tool] RagSearchTool registered")
