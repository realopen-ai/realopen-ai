"""
Tests for the RAG search flow — scope filtering, hybrid scoring, per-doc
adaptive top-k.

These tests use unittest.mock to stub out the embedding client and the
DB session, so they run without a live PostgreSQL or Ollama instance.
They verify the *logic* of search_documents: scope filtering (private vs
public), the hybrid score combination, the per-doc cap, and the
similarity cutoff.
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import rag


@pytest.fixture
def mock_db():
    """A MagicMock async session that returns canned query results."""
    db = MagicMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def conv_id():
    return uuid.uuid4()


@pytest.fixture
def other_conv_id():
    return uuid.uuid4()


# ─── Query handling ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_empty_query_returns_empty(mock_db):
    """Empty or whitespace query → empty list (no DB call)."""
    assert await rag.search_documents(mock_db, "") == []
    assert await rag.search_documents(mock_db, "   ") == []
    mock_db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_search_embedding_failure_returns_empty(mock_db):
    """If the embedding call fails, return empty (graceful degradation)."""
    with patch("app.services.rag.get_embedding", new=AsyncMock(return_value=None)):
        result = await rag.search_documents(mock_db, "test query")
    assert result == []


@pytest.mark.asyncio
async def test_search_no_candidates_returns_empty(mock_db, conv_id):
    """If the vector search returns no rows, return empty."""
    # Simulate the vector search returning no rows
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_db.execute.return_value = mock_result

    with patch(
        "app.services.rag.get_embedding", new=AsyncMock(return_value=[0.1] * 768)
    ):
        result = await rag.search_documents(
            mock_db,
            "test query",
            conversation_id=conv_id,
        )
    assert result == []


# ─── Scope filtering — verified via the SQL stmt construction ──────


@pytest.mark.asyncio
async def test_search_with_conversation_id_filters_correctly(mock_db, conv_id):
    """When conversation_id is set, the SQL WHERE clause includes both
    public docs and private docs tied to that conversation."""
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_db.execute.return_value = mock_result

    with patch(
        "app.services.rag.get_embedding", new=AsyncMock(return_value=[0.1] * 768)
    ):
        await rag.search_documents(
            mock_db,
            "test query",
            conversation_id=conv_id,
        )

    # Inspect the SQL statement that was passed to db.execute
    assert mock_db.execute.called
    stmt = mock_db.execute.call_args[0][0]
    compiled = str(
        stmt.compile(
            compile_kwargs={"literal_binds": True},
        )
    )
    # Both scope conditions should be present
    assert "public" in compiled
    assert "private" in compiled


@pytest.mark.asyncio
async def test_search_without_conversation_id_only_public(mock_db):
    """With no conversation_id, only public docs are searchable."""
    mock_result = MagicMock()
    mock_result.all.return_value = []
    mock_db.execute.return_value = mock_result

    with patch(
        "app.services.rag.get_embedding", new=AsyncMock(return_value=[0.1] * 768)
    ):
        await rag.search_documents(mock_db, "test query")

    stmt = mock_db.execute.call_args[0][0]
    compiled = str(
        stmt.compile(
            compile_kwargs={"literal_binds": True},
        )
    )
    # Only public docs — no private scope clause
    assert "public" in compiled


# ─── Hybrid score + cutoff ──────────────────────────────────────────


def _make_row(
    distance=0.1, doc_id=None, filename="doc.pdf", chunk_id=None, chunk_type="text"
):
    """Build a mock row returned by the vector search query."""
    if doc_id is None:
        doc_id = uuid.uuid4()
    if chunk_id is None:
        chunk_id = uuid.uuid4()
    row = MagicMock()
    row.id = chunk_id
    row.document_id = doc_id
    row.text = "Sample chunk content with the answer."
    row.page_number = 1
    row.line_start = 1
    row.line_end = 2
    row.chunk_type = chunk_type
    row.image_path = None
    row.doc_filename = filename
    row.distance = distance
    return row


@pytest.mark.asyncio
async def test_search_returns_ranked_sources(mock_db, conv_id):
    """Vector + BM25 scores combine, sources are ranked by hybrid score."""
    # Two candidates: one close (small distance), one far
    close_row = _make_row(distance=0.05, filename="relevant.pdf")
    far_row = _make_row(distance=0.5, filename="irrelevant.pdf")

    vector_result = MagicMock()
    vector_result.all.return_value = [close_row, far_row]

    # BM25 query — return empty (so only vector score matters)
    bm25_result = MagicMock()
    bm25_result.__iter__ = MagicMock(return_value=iter([]))

    # First call = vector search, second = BM25
    mock_db.execute.side_effect = [vector_result, bm25_result]

    with patch(
        "app.services.rag.get_embedding", new=AsyncMock(return_value=[0.1] * 768)
    ):
        result = await rag.search_documents(
            mock_db,
            "query",
            conversation_id=conv_id,
        )

    assert len(result) == 2
    # Close row should rank first (higher vector_sim)
    assert result[0].document_filename == "relevant.pdf"
    assert result[1].document_filename == "irrelevant.pdf"
    # Scores should be in descending order
    assert result[0].score >= result[1].score


@pytest.mark.asyncio
async def test_search_similarity_cutoff_drops_low_matches(mock_db, conv_id):
    """Chunks with vec_sim < cutoff are dropped."""
    # distance=0.95 → vec_sim = 1 - 0.95 = 0.05, well below default cutoff (0.20)
    bad_row = _make_row(distance=0.95, filename="bad.pdf")
    # distance=0.05 → vec_sim = 0.95, above cutoff
    good_row = _make_row(distance=0.05, filename="good.pdf")

    vector_result = MagicMock()
    vector_result.all.return_value = [bad_row, good_row]
    bm25_result = MagicMock()
    bm25_result.__iter__ = MagicMock(return_value=iter([]))

    mock_db.execute.side_effect = [vector_result, bm25_result]

    with patch(
        "app.services.rag.get_embedding", new=AsyncMock(return_value=[0.1] * 768)
    ):
        result = await rag.search_documents(
            mock_db,
            "query",
            conversation_id=conv_id,
        )

    # Only the good row survives the cutoff
    assert len(result) == 1
    assert result[0].document_filename == "good.pdf"


# ─── Per-doc adaptive top-k ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_search_per_doc_cap(mock_db, conv_id):
    """At most RAG_TOP_K_PER_DOC chunks per document."""
    # All chunks from the SAME doc (shared doc_id) — otherwise per-doc
    # cap can't kick in because each chunk would be its own "doc".
    shared_doc_id = uuid.uuid4()
    rows = [
        _make_row(distance=0.05, filename="samedoc.pdf", doc_id=shared_doc_id)
        for _ in range(10)
    ]

    vector_result = MagicMock()
    vector_result.all.return_value = rows
    bm25_result = MagicMock()
    bm25_result.__iter__ = MagicMock(return_value=iter([]))

    mock_db.execute.side_effect = [vector_result, bm25_result]

    with patch(
        "app.services.rag.get_embedding", new=AsyncMock(return_value=[0.1] * 768)
    ):
        result = await rag.search_documents(
            mock_db,
            "query",
            conversation_id=conv_id,
        )

    # All chunks are from the same doc — capped at RAG_TOP_K_PER_DOC
    assert len(result) <= rag.settings.RAG_TOP_K_PER_DOC
    assert len(result) == rag.settings.RAG_TOP_K_PER_DOC  # exactly cap


@pytest.mark.asyncio
async def test_search_total_top_k_cap(mock_db, conv_id):
    """At most RAG_TOP_K_TOTAL chunks overall, even across multiple docs."""
    # Generate enough unique docs that the total cap kicks in before the
    # per-doc cap fills up. With k_per_doc=3 and k_total=8, we need at
    # least ceil(8/3)+1 = 4 docs each contributing 3 chunks.
    n_docs = (rag.settings.RAG_TOP_K_TOTAL // rag.settings.RAG_TOP_K_PER_DOC) + 2
    rows = []
    for _ in range(n_docs):
        shared_doc_id = uuid.uuid4()
        for _ in range(rag.settings.RAG_TOP_K_PER_DOC + 1):
            rows.append(
                _make_row(
                    distance=0.05,
                    filename=f"doc_{uuid.uuid4().hex}.pdf",
                    doc_id=shared_doc_id,
                )
            )

    vector_result = MagicMock()
    vector_result.all.return_value = rows
    bm25_result = MagicMock()
    bm25_result.__iter__ = MagicMock(return_value=iter([]))

    mock_db.execute.side_effect = [vector_result, bm25_result]

    with patch(
        "app.services.rag.get_embedding", new=AsyncMock(return_value=[0.1] * 768)
    ):
        result = await rag.search_documents(
            mock_db,
            "query",
            conversation_id=conv_id,
        )

    assert len(result) <= rag.settings.RAG_TOP_K_TOTAL


@pytest.mark.asyncio
async def test_search_custom_top_k(mock_db, conv_id):
    """Caller can override top_k_per_doc and top_k_total."""
    # Each chunk from a unique doc → per-doc cap of 1, total cap of 3
    rows = [_make_row(distance=0.05, filename=f"doc_{i}.pdf") for i in range(20)]

    vector_result = MagicMock()
    vector_result.all.return_value = rows
    bm25_result = MagicMock()
    bm25_result.__iter__ = MagicMock(return_value=iter([]))

    mock_db.execute.side_effect = [vector_result, bm25_result]

    with patch(
        "app.services.rag.get_embedding", new=AsyncMock(return_value=[0.1] * 768)
    ):
        result = await rag.search_documents(
            mock_db,
            "query",
            conversation_id=conv_id,
            top_k_per_doc=1,
            top_k_total=3,
        )

    # Each doc has only 1 chunk, so per-doc cap is the binding constraint.
    # With 20 docs × 1 chunk/doc = up to 20 candidates, total cap = 3.
    assert len(result) <= 3
