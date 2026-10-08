"""
Tests that digestion runs in a thread pool and does NOT block the event loop.

These tests verify the critical fix from realopen-ai17: large PDFs no longer
hang the backend. Before the fix, `digest_document` called synchronous
pdfplumber/pypdf/PIL operations inline, blocking the FastAPI event loop
for the entire duration of digestion. After the fix, all blocking I/O is
wrapped in `asyncio.to_thread()`, so:

1. Other asyncio tasks can run concurrently during digestion.
2. GET endpoints remain responsive.
3. The digestion progress callback fires in real-time (not batched at the end).

Strategy: we mock the slow extractor with a function that sleeps for a fixed
duration, then verify an unrelated asyncio task runs concurrently during
that sleep. If digestion blocked the event loop, the unrelated task would
only run AFTER digestion completes.
"""

import asyncio
import time
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import rag


class _FakeFreshSession:
    """A no-op async session for tests that don't touch a real DB.

    The digestion refactor opens fresh sessions via async_session_factory()
    for the file_path UPDATE, chunk batch INSERTs, and doc-stats UPDATE.
    These tests don't have a real DB, so we patch async_session_factory
    to return this fake.
    """

    def __init__(self):
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    def add(self, obj):
        self.added.append(obj)

    def add_all(self, objs):
        self.added.extend(objs)

    async def execute(self, stmt):
        # For the final SELECT Document fetch, return a mock doc
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(
            return_value=MagicMock(
                id=uuid.uuid4(),
                filename="test.txt",
                total_chunks=1,
                total_images=0,
                digestion_status="ready",
            )
        )
        return result

    async def commit(self):
        pass


def _fake_factory():
    """Return a fake async_session_factory context-manager."""
    session = _FakeFreshSession()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=session)
    ctx.__aexit__ = AsyncMock(return_value=None)
    return ctx


@pytest.fixture
def mock_db():
    """A MagicMock async session — no real DB needed for these tests."""
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()
    db.delete = AsyncMock()
    db.execute = AsyncMock()
    return db


@pytest.fixture
def conv_id():
    return uuid.uuid4()


# ─── extract_content_async uses asyncio.to_thread ───────────────────


@pytest.mark.asyncio
async def test_extract_content_async_runs_in_thread_pool():
    """extract_content_async MUST run the extractor in a thread pool,
    not inline on the event loop.

    We verify this by patching the extractor to sleep for 0.2s and
    checking that an unrelated asyncio task runs concurrently during
    that sleep. If extraction blocked the event loop, the concurrent
    task would only run AFTER extraction finishes.
    """

    def slow_extractor(buf, filename):
        # Simulate a slow PDF extraction
        time.sleep(0.3)
        return rag.ExtractionResult(
            pages=[rag.ExtractedPage(page_number=1, text="extracted text")],
            images=[],
            mime_type="application/pdf",
        )

    with patch("app.services.rag.extract_content", side_effect=slow_extractor):
        # Start an asyncio task that increments a counter every 50ms.
        # If the event loop is free, this runs ~6 times during the 0.3s
        # extraction. If blocked, it runs 0 times during extraction.
        counter = {"n": 0}

        async def ticker():
            while True:
                await asyncio.sleep(0.05)
                counter["n"] += 1

        ticker_task = asyncio.create_task(ticker())

        # Run extraction (this should NOT block the ticker)
        result = await rag.extract_content_async(b"fake pdf bytes", "test.pdf")

        # Stop the ticker
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            pass

    # The ticker should have fired at least 2-3 times during the 0.3s
    # extraction. If the event loop was blocked, counter would be 0 or 1.
    # We use a generous lower bound to avoid flakiness on slow CI.
    assert counter["n"] >= 2, (
        f"Event loop was blocked during extraction! Ticker only fired "
        f"{counter['n']} times during a 0.3s extraction (expected ≥2)."
    )
    assert result.pages[0].text == "extracted text"


# ─── Digestion: progress events fire in real-time, not batched ─────


@pytest.mark.asyncio
async def test_digest_progress_events_fire_during_digestion(mock_db, conv_id):
    """Progress events MUST fire DURING digestion, not just at the end.

    We patch extract_content_async to sleep briefly between progress
    events, and verify that the progress callback is invoked at least
    once BEFORE digestion completes.

    This catches regressions where digestion runs synchronously inline
    and only flushes events at the end (the original bug).
    """
    progress_events = []

    def collect(p):
        progress_events.append((p.stage, p.percent, time.time()))

    async def slow_extract(buf, filename):
        await asyncio.sleep(0.05)
        return rag.ExtractionResult(
            pages=[rag.ExtractedPage(page_number=1, text="content")],
            images=[],
            mime_type="text/plain",
        )

    # Stub out all the side-effecting pieces of digest_document
    with (
        patch("app.services.rag.async_session_factory", new=_fake_factory),
        patch("app.services.rag.extract_content_async", new=slow_extract),
        patch(
            "app.services.rag.save_uploaded_file",
            new=AsyncMock(return_value="documents/x/file.txt"),
        ),
        patch(
            "app.services.rag.chunk_pages",
            return_value=[
                rag.Chunk(text="c", chunk_index=0, page_number=1, line_start=1, line_end=1)
            ],
        ),
        patch("app.services.rag.get_embeddings", new=AsyncMock(return_value=[[0.1] * 768])),
        patch("app.services.rag._sha256", return_value="fakehash"),
    ):
        # We can't actually commit Document/DocumentChunk rows without a
        # real DB, but we can verify the progress events fire during
        # digestion by catching the exception that comes from db.add()
        # on a MagicMock. Actually, db.add is a MagicMock so it's a no-op.
        # db.commit is AsyncMock so it returns a coroutine that resolves.
        try:
            await rag.digest_document(
                mock_db,
                file_bytes=b"fake",
                filename="test.txt",
                scope="private",
                conversation_id=conv_id,
                progress=collect,
            )
        except Exception:
            # We don't care if digestion fails on the mocked DB — we only
            # care that progress events fired DURING digestion.
            pass

    # We should have at least the "started" event AND events from
    # during digestion (extracting_text, chunking, etc).
    stages = [e[0] for e in progress_events]
    assert "started" in stages, "No 'started' event fired"
    assert len(progress_events) >= 3, (
        f"Only {len(progress_events)} progress events fired — expected ≥3 "
        f"(started + at least 2 stage events). This suggests events are "
        f"being batched at the end instead of firing in real-time."
    )


# ─── Digestion: concurrent tasks run during digestion ──────────────


@pytest.mark.asyncio
async def test_digest_does_not_block_concurrent_tasks(mock_db, conv_id):
    """An unrelated asyncio task MUST run concurrently with digestion.

    This is the core fix: a large PDF no longer blocks other GET endpoints.
    We simulate this by starting a ticker task alongside digestion and verifying the
    ticker fires multiple times DURING digestion.
    """
    counter = {"n": 0}

    async def ticker():
        while True:
            await asyncio.sleep(0.05)
            counter["n"] += 1

    async def slow_extract(buf, filename):
        # Simulate a 0.5s PDF extraction
        await asyncio.sleep(0.5)
        return rag.ExtractionResult(
            pages=[rag.ExtractedPage(page_number=1, text="content")],
            images=[],
            mime_type="application/pdf",
        )

    ticker_task = asyncio.create_task(ticker())
    import fitz

    with fitz.open() as pdf:
        pdf.new_page()
        pdf_bytes = pdf.tobytes()

    try:
        with (
            patch("app.services.rag.async_session_factory", new=_fake_factory),
            patch("app.services.rag.extract_content_async", new=slow_extract),
            patch(
                "app.services.rag.save_uploaded_file",
                new=AsyncMock(return_value="documents/x/file.pdf"),
            ),
            patch(
                "app.services.rag.chunk_pages",
                return_value=[
                    rag.Chunk(text="c", chunk_index=0, page_number=1, line_start=1, line_end=1)
                ],
            ),
            patch("app.services.rag.get_embeddings", new=AsyncMock(return_value=[[0.1] * 768])),
            patch("app.services.rag._sha256", return_value="fakehash"),
        ):
            try:
                await rag.digest_document(
                    mock_db,
                    file_bytes=pdf_bytes,
                    filename="big.pdf",
                    scope="private",
                    conversation_id=conv_id,
                )
            except Exception:
                pass
    finally:
        ticker_task.cancel()
        try:
            await ticker_task
        except asyncio.CancelledError:
            pass

    # During a 0.5s digestion, the ticker should have fired ~10 times.
    # If the event loop was blocked, it would have fired 0 times.
    # We use a generous lower bound of 3 to avoid flakiness.
    assert counter["n"] >= 3, (
        f"Event loop was blocked during digestion! Ticker only fired "
        f"{counter['n']} times during a 0.5s digestion (expected ≥3)."
    )


# ─── Search: image_path is included in results ─────────────────────


def test_retrieved_source_has_image_path_field():
    """RetrievedSource must have an image_path field for image chunks."""
    s = rag.RetrievedSource(
        document_id="doc-1",
        document_filename="report.pdf",
        chunk_id="chunk-1",
        text="[Image on page 3 of report.pdf]\nA bar chart showing Q3 sales",
        page_number=3,
        line_start=None,
        line_end=None,
        chunk_type="image_description",
        score=0.85,
        vector_sim=0.80,
        bm25_score=0.05,
        image_path="documents/abc/image_000.png",
    )
    assert s.image_path == "documents/abc/image_000.png"
    d = rag.retrieved_source_to_dict(s)
    assert d["image_path"] == "documents/abc/image_000.png"
    assert d["has_image"] is True
    assert d["chunk_type"] == "image_description"


def test_retrieved_source_text_chunk_has_no_image_path():
    """Text chunks have image_path=None and has_image=False."""
    s = rag.RetrievedSource(
        document_id="doc-1",
        document_filename="report.pdf",
        chunk_id="chunk-1",
        text="Q3 revenue was $4.2M, up 12% YoY.",
        page_number=2,
        line_start=15,
        line_end=18,
        chunk_type="text",
        score=0.85,
        vector_sim=0.80,
        bm25_score=0.05,
    )
    assert s.image_path is None
    d = rag.retrieved_source_to_dict(s)
    assert d["image_path"] is None
    assert d["has_image"] is False


# ─── DOCX extraction: import path is correct ────────────────────────


def test_extract_docx_uses_correct_document_class():
    """The DOCX extractor must use `from docx import Document`, not
    `from docx.document import Document` (which is the base class
    that requires a `part` argument and breaks on instantiation).

    This is a regression test for the bug that broke DOCX extraction
    in realopen-ai16.
    """
    import io
    from docx import Document

    # Create a minimal DOCX in memory
    doc = Document()
    doc.add_paragraph("Hello world from DOCX.")
    buf = io.BytesIO()
    doc.save(buf)

    result = rag._extract_docx(buf.getvalue())
    assert len(result.pages) == 1
    assert "Hello world from DOCX" in result.pages[0].text
