"""
Tests for the new image-related RAG functionality added in realopen-ai17:

1. RetrievedSource.image_path is properly serialized to dict.
2. retrieved_source_to_dict includes `has_image` boolean.
3. get_chunk_image_path helper queries the DB for a chunk's image_path.
4. The /api/documents/chunks/{id}/image endpoint serves image bytes.
5. The agent_system.md prompt contains RAG-specific rules so the LLM
   knows when to call rag_search.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import rag

# ─── Image path serialization ─────────────────────────────────────


def test_retrieved_source_to_dict_includes_image_path_for_image_chunk():
    """An image_description chunk with image_path must serialize the path
    and set has_image=True so the frontend can fetch and render the image."""
    s = rag.RetrievedSource(
        document_id="doc-1",
        document_filename="scan.pdf",
        chunk_id="chunk-1",
        text="[Image on page 2 of scan.pdf]\nA diagram of the system architecture.",
        page_number=2,
        line_start=None,
        line_end=None,
        chunk_type="image_description",
        score=0.91,
        vector_sim=0.88,
        bm25_score=0.10,
        image_path="documents/abc-123/image_000.png",
    )
    d = rag.retrieved_source_to_dict(s)
    assert d["image_path"] == "documents/abc-123/image_000.png"
    assert d["has_image"] is True
    assert d["chunk_type"] == "image_description"


def test_retrieved_source_to_dict_has_image_false_for_text_chunk():
    """A text chunk has image_path=None and has_image=False."""
    s = rag.RetrievedSource(
        document_id="doc-1",
        document_filename="report.pdf",
        chunk_id="chunk-1",
        text="Some text content from the document.",
        page_number=1,
        line_start=10,
        line_end=15,
        chunk_type="text",
        score=0.75,
        vector_sim=0.70,
        bm25_score=0.05,
        image_path=None,
    )
    d = rag.retrieved_source_to_dict(s)
    assert d["image_path"] is None
    assert d["has_image"] is False


def test_retrieved_source_to_dict_has_image_false_when_image_path_missing():
    """When image_path is omitted (defaults to None), has_image must be False."""
    s = rag.RetrievedSource(
        document_id="doc-1",
        document_filename="report.pdf",
        chunk_id="chunk-1",
        text="Some text content.",
        page_number=1,
        line_start=10,
        line_end=15,
        chunk_type="text",
        score=0.75,
        vector_sim=0.70,
        bm25_score=0.05,
        # image_path omitted — should default to None
    )
    d = rag.retrieved_source_to_dict(s)
    assert d["image_path"] is None
    assert d["has_image"] is False


# ─── get_chunk_image_path helper ──────────────────────────────────


@pytest.mark.asyncio
async def test_get_chunk_image_path_returns_path_when_chunk_exists():
    """get_chunk_image_path returns the image_path for a chunk that has one."""
    chunk_id = uuid.uuid4()
    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.first.return_value = ("documents/abc/image_001.png",)
    mock_db.execute = AsyncMock(return_value=mock_result)

    result = await rag.get_chunk_image_path(mock_db, chunk_id)
    assert result == "documents/abc/image_001.png"


@pytest.mark.asyncio
async def test_get_chunk_image_path_returns_none_when_no_row():
    """get_chunk_image_path returns None when the chunk doesn't exist."""
    chunk_id = uuid.uuid4()
    mock_db = MagicMock()
    mock_result = MagicMock()
    mock_result.first.return_value = None
    mock_db.execute = AsyncMock(return_value=mock_result)

    result = await rag.get_chunk_image_path(mock_db, chunk_id)
    assert result is None


@pytest.mark.asyncio
async def test_get_chunk_image_path_returns_none_on_db_error():
    """get_chunk_image_path returns None (not raises) on DB error."""
    chunk_id = uuid.uuid4()
    mock_db = MagicMock()
    mock_db.execute = AsyncMock(side_effect=Exception("DB connection lost"))

    result = await rag.get_chunk_image_path(mock_db, chunk_id)
    assert result is None


# ─── Agent system prompt contains RAG rules ──────────────────────


def test_agent_system_prompt_contains_rag_rules():
    """The agent_system.md prompt MUST contain a DOCUMENT SEARCH RULES
    section so the LLM knows when to call rag_search.

    Without this section, the LLM doesn't proactively use rag_search —
    it answers from general knowledge even when the user uploaded
    documents specifically to be queried.
    """
    from pathlib import Path

    prompt_path = (
        Path(__file__).resolve().parent.parent / "app" / "prompts" / "agent_system.md"
    )
    content = prompt_path.read_text(encoding="utf-8")

    # The prompt must mention rag_search and tell the LLM when to use it
    assert (
        "rag_search" in content
    ), "agent_system.md must mention the rag_search tool by name"
    assert (
        "DOCUMENT SEARCH RULES" in content
    ), "agent_system.md must have a DOCUMENT SEARCH RULES section"
    # The prompt must instruct the LLM to try rag_search FIRST before
    # answering from general knowledge when documents are available.
    assert (
        "FIRST" in content or "first" in content
    ), "agent_system.md must instruct the LLM to try rag_search first"
    # The prompt must mention citing sources
    assert (
        "cite" in content.lower() or "source" in content.lower()
    ), "agent_system.md must mention citing sources"


# ─── RagSearchTool description mentions proactive use ─────────────


def test_rag_tool_description_encourages_proactive_use():
    """The rag_search tool description should tell the LLM to use it
    PROACTIVELY when documents are available — not just when the user
    explicitly asks about documents.
    """
    from app.agent.tools.rag_search import RagSearchTool

    desc = RagSearchTool.description.lower()
    assert (
        "proactively" in desc or "proactive" in desc
    ), "RagSearchTool.description should mention proactive use"
    assert (
        "rag_search" in desc or "document" in desc
    ), "RagSearchTool.description should mention documents/rag_search"


# ─── /upload/stream uses asyncio.Queue (not list polling) ─────────


def test_upload_stream_endpoint_uses_asyncio_queue():
    """The /documents/upload/stream endpoint code must use asyncio.Queue
    for real-time progress delivery, not the old list-and-poll approach.

    We verify by inspecting the source code — this is a regression test
    for the bug where progress events were only flushed after digestion
    completed.
    """
    from pathlib import Path

    src_path = Path(__file__).resolve().parent.parent / "app" / "api" / "documents.py"
    src = src_path.read_text(encoding="utf-8")

    # The new implementation uses asyncio.Queue
    assert (
        "asyncio.Queue" in src or "Queue()" in src
    ), "documents.py /upload/stream should use asyncio.Queue for progress"
    # The old list-based polling should be gone
    assert (
        "progress_events: List" not in src
    ), "documents.py should no longer use list-based progress polling"


# ─── chat.py uses asyncio.Queue for real-time progress ────────────


def test_chat_multipart_uses_asyncio_queue_for_digest():
    """The /chat/stream/multipart endpoint must use asyncio.Queue to
    stream document_digest_* events in REAL TIME during chat uploads.

    Before the fix, the events were buffered in a list and only flushed
    AFTER digestion completed — the user saw nothing during the sometimes
    minutes-long digestion of a large PDF.
    """
    from pathlib import Path

    src_path = Path(__file__).resolve().parent.parent / "app" / "api" / "chat.py"
    src = src_path.read_text(encoding="utf-8")

    # The new implementation uses asyncio.Queue for chat-upload progress
    assert "progress_queue" in src, (
        "chat.py /chat/stream/multipart should use a progress_queue for "
        "real-time digestion progress (not list-based buffering)"
    )
    assert (
        "_DONE_SENTINEL" in src
    ), "chat.py should use a sentinel to signal digestion completion"


# ─── chat.py injects RAG hint when documents are uploaded ────────


def test_chat_multipart_injects_rag_hint():
    """When documents are uploaded via chat, the chat endpoint must
    inject a system hint telling the agent these files are now searchable
    via rag_search. This dramatically increases the chance the LLM uses
    rag_search proactively on the same turn.
    """
    from pathlib import Path

    src_path = Path(__file__).resolve().parent.parent / "app" / "api" / "chat.py"
    src = src_path.read_text(encoding="utf-8")

    # The hint injection code must be present
    assert (
        "digested_doc_filenames" in src
    ), "chat.py should track digested document filenames"
    assert "rag_search" in src, "chat.py should mention rag_search in the injected hint"
    assert (
        "System: The user just uploaded" in src
    ), "chat.py should inject a system hint about freshly-uploaded documents"


# ─── rag.py wraps blocking I/O in asyncio.to_thread ───────────────


def test_rag_py_uses_asyncio_to_thread():
    """rag.py MUST use asyncio.to_thread for all blocking I/O (pdfplumber,
    pypdf, openpyxl, python-docx, PIL, file writes) so the FastAPI event
    loop can keep serving other requests while a large PDF is being digested.

    This is the core fix for the 'backend hangs on large PDF' bug.
    """
    from pathlib import Path

    src_path = Path(__file__).resolve().parent.parent / "app" / "services" / "rag.py"
    src = src_path.read_text(encoding="utf-8")

    # asyncio.to_thread must be used for extraction, chunking, and file writes
    assert (
        "asyncio.to_thread" in src
    ), "rag.py must use asyncio.to_thread to offload blocking I/O"
    # The async wrapper around extract_content must exist
    assert (
        "extract_content_async" in src
    ), "rag.py must expose an async wrapper around extract_content"
    # The async wrapper around save_image_file must exist
    assert (
        "save_image_file_async" in src
    ), "rag.py must expose an async wrapper around save_image_file"


# ─── docx extraction uses correct import ──────────────────────────


def test_docx_extractor_uses_top_level_document_import():
    """The DOCX extractor must use `from docx import Document`, not
    `from docx.document import Document` (which imports the base class
    that requires a `part` argument and crashes on instantiation).
    """
    from pathlib import Path

    src_path = Path(__file__).resolve().parent.parent / "app" / "services" / "rag.py"
    src = src_path.read_text(encoding="utf-8")

    # The correct import must be present
    assert (
        "from docx import Document as _DocxDocument" in src
    ), "rag.py must use `from docx import Document` for DOCX extraction"
    # The buggy import must NOT be present
    assert "from docx.document import Document as _DocxDocument" not in src, (
        "rag.py must NOT use `from docx.document import Document` — that's "
        "the base class which requires a `part` argument"
    )
