"""
Tests for the RAG agent tool (RagSearchTool).

These tests verify:
  - The tool registers itself in the global tool registry on import.
  - The tool name/description match what the agent prompt expects.
  - execute() with an empty query returns gracefully (no DB call).
  - execute() with a real query (mocked DB) returns formatted output +
    stashes the sources on the tool_call for the SSE layer to read.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.base import get_tool_registry
from app.agent.tools.rag_search import RagSearchTool
from app.services import rag


@pytest.fixture(autouse=True)
def ordinary_conversation(monkeypatch):
    # These tool-formatting tests have no notebook or live database.
    monkeypatch.setattr("app.agent.tools.rag_search.selected_document_ids", AsyncMock(return_value=None))

# ─── Registration ───────────────────────────────────────────────────


def test_rag_tool_registered():
    """The tool registers itself in the global registry at import time."""
    registry = get_tool_registry()
    assert registry.has_tool("rag_search"), (
        "rag_search tool not registered. Check app/agent/tools/__init__.py "
        "imports RagSearchTool."
    )


def test_rag_tool_metadata():
    """The tool exposes name + description for the agent system prompt."""
    registry = get_tool_registry()
    tool = registry.get("rag_search")
    assert tool is not None
    assert tool.name == "rag_search"
    assert "document" in tool.description.lower()
    assert "knowledge base" in tool.description.lower()


# ─── Empty query ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rag_tool_empty_query_returns_gracefully():
    """An empty query short-circuits without touching the DB."""
    tool = RagSearchTool()
    result = await tool.execute(query="")
    assert result.success is True
    assert "No query" in result.output
    assert result.tool_call is not None
    assert result.tool_call.status == "completed"


@pytest.mark.asyncio
async def test_rag_tool_whitespace_query_returns_gracefully():
    """A whitespace-only query short-circuits too."""
    tool = RagSearchTool()
    result = await tool.execute(query="   ")
    assert result.success is True
    assert "No query" in result.output


# ─── Real query with mocked DB ──────────────────────────────────────


@pytest.mark.asyncio
async def test_rag_tool_returns_formatted_output_and_sources():
    """A successful search returns formatted text for the LLM and stashes
    the structured sources on the tool_call.rag_sources attribute."""
    conv_id = str(uuid.uuid4())

    # Mock sources that the (mocked) search_documents call returns
    mock_sources = [
        rag.RetrievedSource(
            document_id=str(uuid.uuid4()),
            document_filename="report.pdf",
            chunk_id=str(uuid.uuid4()),
            text="Revenue was $4.2M in Q3.",
            page_number=7,
            line_start=12,
            line_end=14,
            chunk_type="text",
            score=0.87,
            vector_sim=0.91,
            bm25_score=0.42,
        ),
    ]

    tool = RagSearchTool()

    # Mock the DB session AND search_documents — we only care that the
    # tool wires its inputs/outputs correctly, not that search_documents
    # itself runs (that's tested in test_rag_search.py).
    mock_db = MagicMock()
    mock_db_ctx = AsyncMock()
    mock_db_ctx.__aenter__.return_value = mock_db
    mock_db_ctx.__aexit__.return_value = None

    with patch(
        "app.services.rag.search_documents",
        new=AsyncMock(return_value=mock_sources),
    ), patch(
        "app.db.session.async_session_factory",
        return_value=mock_db_ctx,
    ):
        result = await tool.execute(
            query="What was Q3 revenue?", conversation_id=conv_id
        )

    assert result.success is True
    assert "report.pdf" in result.output
    assert "$4.2M" in result.output
    assert "[Source 1:" in result.output

    # The SSE layer reads tool_call.rag_sources to emit rag_sources events
    assert result.tool_call is not None
    assert hasattr(result.tool_call, "rag_sources")
    assert len(result.tool_call.rag_sources) == 1
    assert result.tool_call.rag_sources[0]["document_filename"] == "report.pdf"
    assert result.tool_call.rag_sources[0]["page_number"] == 7
    assert result.tool_call.status == "completed"


# ─── Conversation ID parsing ────────────────────────────────────────


@pytest.mark.asyncio
async def test_rag_tool_invalid_conversation_id_falls_back_to_public():
    """An invalid conversation_id string is logged + ignored (public-only search)."""
    tool = RagSearchTool()

    mock_db_ctx = AsyncMock()
    mock_db_ctx.__aenter__.return_value = MagicMock()
    mock_db_ctx.__aexit__.return_value = None

    with patch(
        "app.services.rag.search_documents",
        new=AsyncMock(return_value=[]),
    ) as mock_search, patch(
        "app.db.session.async_session_factory",
        return_value=mock_db_ctx,
    ):
        result = await tool.execute(query="test", conversation_id="not-a-uuid")

    assert result.success is True
    # search_documents was called with conversation_id=None (parsed from
    # the invalid string)
    assert mock_search.called
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["conversation_id"] is None


@pytest.mark.asyncio
async def test_rag_tool_no_conversation_id_searches_public_only():
    """When no conversation_id is provided, search_documents gets None."""
    tool = RagSearchTool()

    mock_db_ctx = AsyncMock()
    mock_db_ctx.__aenter__.return_value = MagicMock()
    mock_db_ctx.__aexit__.return_value = None

    with patch(
        "app.services.rag.search_documents",
        new=AsyncMock(return_value=[]),
    ) as mock_search, patch(
        "app.db.session.async_session_factory",
        return_value=mock_db_ctx,
    ):
        result = await tool.execute(query="test")

    assert result.success is True
    call_kwargs = mock_search.call_args.kwargs
    assert call_kwargs["conversation_id"] is None


# ─── Error handling ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_rag_tool_search_exception_returns_error_result():
    """If search_documents raises, the tool returns an error result (not crash)."""
    tool = RagSearchTool()

    mock_db_ctx = AsyncMock()
    mock_db_ctx.__aenter__.return_value = MagicMock()
    mock_db_ctx.__aexit__.return_value = None

    with patch(
        "app.services.rag.search_documents",
        new=AsyncMock(side_effect=RuntimeError("DB exploded")),
    ), patch(
        "app.db.session.async_session_factory",
        return_value=mock_db_ctx,
    ):
        result = await tool.execute(query="test", conversation_id=str(uuid.uuid4()))

    assert result.success is False
    assert "DB exploded" in result.output
    assert result.tool_call is not None
    assert result.tool_call.status == "error"
    assert "DB exploded" in result.tool_call.error
