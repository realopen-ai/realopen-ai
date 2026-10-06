"""
Tests for the RAG source formatting + serialization helpers.

These functions turn RetrievedSource objects into (a) a string the LLM
sees during the agent loop and (b) JSON-serializable dicts the frontend
renders as source cards. Pure functions — no DB or network.
"""

import pytest

from app.services import rag

# ─── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def sample_source():
    return rag.RetrievedSource(
        document_id="11111111-1111-1111-1111-111111111111",
        document_filename="report.pdf",
        chunk_id="22222222-2222-2222-2222-222222222222",
        text="The revenue for Q3 was $4.2M, up 18% year-over-year.",
        page_number=7,
        line_start=12,
        line_end=14,
        chunk_type="text",
        score=0.87,
        vector_sim=0.91,
        bm25_score=0.42,
    )


@pytest.fixture
def image_source():
    return rag.RetrievedSource(
        document_id="33333333-3333-3333-3333-333333333333",
        document_filename="slides.pdf",
        chunk_id="44444444-4444-4444-4444-444444444444",
        text="[Image on page 3 of slides.pdf]\nA bar chart showing monthly sales.",
        page_number=3,
        line_start=None,
        line_end=None,
        chunk_type="image_description",
        score=0.78,
        vector_sim=0.80,
        bm25_score=0.30,
    )


# ─── format_sources_for_llm ─────────────────────────────────────────


def test_format_sources_empty():
    """No sources → friendly message."""
    assert rag.format_sources_for_llm([]) == "No relevant document chunks found."


def test_format_sources_single(sample_source):
    """Single source → one [Source 1] block with location."""
    out = rag.format_sources_for_llm([sample_source])
    assert "[Source 1:" in out
    assert "report.pdf" in out
    assert f"document_id {sample_source.document_id}" in out
    assert "page 7" in out
    assert "lines 12-14" in out
    assert "$4.2M" in out
    assert out.endswith("---")


def test_format_sources_multiple(sample_source, image_source):
    """Multiple sources → numbered blocks."""
    out = rag.format_sources_for_llm([sample_source, image_source])
    assert "[Source 1:" in out
    assert "[Source 2:" in out
    assert "report.pdf" in out
    assert "slides.pdf" in out


def test_format_sources_single_line_range():
    """When line_start == line_end, format as 'line N' (not 'lines N-N')."""
    s = rag.RetrievedSource(
        document_id="x" * 36,
        document_filename="doc.txt",
        chunk_id="y" * 36,
        text="content",
        page_number=1,
        line_start=5,
        line_end=5,
        chunk_type="text",
        score=0.5,
        vector_sim=0.5,
        bm25_score=0.5,
    )
    out = rag.format_sources_for_llm([s])
    assert "line 5" in out
    assert "lines 5-5" not in out


def test_format_sources_image_no_line_range(image_source):
    """Image chunks (no line range) omit the line part of the citation."""
    out = rag.format_sources_for_llm([image_source])
    assert "slides.pdf" in out
    assert "page 3" in out
    # No 'line' part since line_start is None
    assert "line " not in out.split("[Source 1:")[1].split("]")[0]


# ─── retrieved_source_to_dict ───────────────────────────────────────


def test_source_to_dict_text_source(sample_source):
    """Text source → dict with all fields populated."""
    d = rag.retrieved_source_to_dict(sample_source)
    assert d["document_id"] == "11111111-1111-1111-1111-111111111111"
    assert d["document_filename"] == "report.pdf"
    assert d["page_number"] == 7
    assert d["line_start"] == 12
    assert d["line_end"] == 14
    assert d["chunk_type"] == "text"
    assert d["score"] == 0.87
    assert d["text"] == sample_source.text
    # Snippet should be the same since the text is short
    assert d["snippet"] == sample_source.text


def test_source_to_dict_image_source(image_source):
    """Image source → chunk_type='image_description' preserved."""
    d = rag.retrieved_source_to_dict(image_source)
    assert d["chunk_type"] == "image_description"
    assert d["page_number"] == 3
    assert d["line_start"] is None
    assert d["line_end"] is None


def test_source_to_dict_snippet_truncation():
    """Long text gets truncated to ~300 chars with ellipsis."""
    long_text = "x" * 500
    s = rag.RetrievedSource(
        document_id="a" * 36,
        document_filename="big.txt",
        chunk_id="b" * 36,
        text=long_text,
        page_number=1,
        line_start=1,
        line_end=2,
        chunk_type="text",
        score=0.5,
        vector_sim=0.5,
        bm25_score=0.5,
    )
    d = rag.retrieved_source_to_dict(s)
    assert len(d["snippet"]) <= 302  # 300 + "…"
    assert d["snippet"].endswith("…")
    assert d["text"] == long_text  # full text preserved in 'text'


# ─── document_to_dict (no DB) ───────────────────────────────────────


def test_document_to_dict_minimal():
    """A Document with only required fields → dict has all expected keys."""
    import uuid
    from datetime import datetime, UTC
    from app.db.models import Document

    now = datetime.now(UTC)
    doc = Document(
        id=uuid.uuid4(),
        filename="test.pdf",
        original_filename="test.pdf",
        mime_type="application/pdf",
        file_path="documents/abc/test.pdf",
        file_size_bytes=1024,
        content_hash="deadbeef",
        scope="private",
        conversation_id=uuid.uuid4(),
        message_id=None,
        total_pages=10,
        total_chunks=24,
        total_images=2,
        digestion_status="ready",
        digestion_error=None,
        created_at=now,
        updated_at=now,
    )
    d = rag.document_to_dict(doc, include_chunks=False)
    assert d["filename"] == "test.pdf"
    assert d["scope"] == "private"
    assert d["total_chunks"] == 24
    assert d["digestion_status"] == "ready"
    assert d["conversation_id"]  # non-null, stringified
    assert d["message_id"] is None
    assert "chunks" not in d


# ─── Path helpers ───────────────────────────────────────────────────


def test_get_file_extension_lowercase():
    """Extension is returned lowercase without the dot."""
    assert rag.get_file_extension("test.PDF") == "pdf"
    assert rag.get_file_extension("archive.Tar.Gz") == "gz"
    assert rag.get_file_extension("noext") == ""


def test_guess_mime_type_known():
    """Known extensions get a real MIME type."""
    assert rag.guess_mime_type("doc.pdf") == "application/pdf"
    assert rag.guess_mime_type("image.png") == "image/png"


def test_guess_mime_type_unknown():
    """Unknown extensions fall back to octet-stream."""
    assert rag.guess_mime_type("file.xyzunknown") == "application/octet-stream"
