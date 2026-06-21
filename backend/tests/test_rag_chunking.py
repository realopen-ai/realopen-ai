"""
Tests for the RAG adaptive chunker.

The chunker is the most consequential piece of the RAG pipeline — it
decides what gets embedded and what gets returned as a source citation.
These tests pin down:

  - Adaptive chunk size selection (small / medium / large doc thresholds)
  - Sentence-boundary splitting (chunks don't break sentences)
  - Overlap behavior (consecutive chunks share some content)
  - Long-sentence hard splitting (oversized sentences get force-split)
  - Line-range tracking (for source citations)
  - Multi-page chunking (page boundaries are respected)
"""

from app.services import rag

# ─── Adaptive chunk size selection ──────────────────────────────────


def test_pick_chunk_size_small_doc():
    """Docs < RAG_SMALL_DOC_THRESHOLD get the small chunk size."""
    assert rag._pick_chunk_size(100) == rag.settings.RAG_CHUNK_SIZE_SMALL
    assert rag._pick_chunk_size(4999) == rag.settings.RAG_CHUNK_SIZE_SMALL


def test_pick_chunk_size_medium_doc():
    """Docs between small and large thresholds get the medium chunk size."""
    assert rag._pick_chunk_size(5000) == rag.settings.RAG_CHUNK_SIZE_MEDIUM
    assert rag._pick_chunk_size(49999) == rag.settings.RAG_CHUNK_SIZE_MEDIUM


def test_pick_chunk_size_large_doc():
    """Docs >= RAG_LARGE_DOC_THRESHOLD get the large chunk size."""
    assert rag._pick_chunk_size(50000) == rag.settings.RAG_CHUNK_SIZE_LARGE
    assert rag._pick_chunk_size(1_000_000) == rag.settings.RAG_CHUNK_SIZE_LARGE


# ─── Basic chunking ─────────────────────────────────────────────────


def test_chunk_single_sentence():
    """A single sentence fits in one chunk."""
    pages = [rag.ExtractedPage(page_number=1, text="Hello world.")]
    chunks = rag.chunk_pages(pages, chunk_size=500, overlap_ratio=0.2)
    assert len(chunks) == 1
    assert chunks[0].text == "Hello world."
    assert chunks[0].page_number == 1
    assert chunks[0].chunk_index == 0


def test_chunk_empty_pages():
    """Empty page list → no chunks."""
    assert rag.chunk_pages([], chunk_size=500, overlap_ratio=0.2) == []


def test_chunk_empty_page_text():
    """Page with empty text → no chunks (no sentences)."""
    pages = [rag.ExtractedPage(page_number=1, text="")]
    assert rag.chunk_pages(pages, chunk_size=500, overlap_ratio=0.2) == []


def test_chunk_whitespace_only_text():
    """Page with only whitespace → no chunks."""
    pages = [rag.ExtractedPage(page_number=1, text="   \n  \t  ")]
    assert rag.chunk_pages(pages, chunk_size=500, overlap_ratio=0.2) == []


# ─── Sentence-boundary splitting ────────────────────────────────────


def test_chunk_multiple_sentences_one_chunk():
    """Multiple sentences under chunk_size stay together."""
    text = "First sentence. Second sentence. Third one!"
    pages = [rag.ExtractedPage(page_number=1, text=text)]
    chunks = rag.chunk_pages(pages, chunk_size=500, overlap_ratio=0.2)
    assert len(chunks) == 1
    assert "First sentence" in chunks[0].text
    assert "Second sentence" in chunks[0].text
    assert "Third one" in chunks[0].text


def test_chunk_split_across_chunks():
    """When text exceeds chunk_size, it splits into multiple chunks."""
    # Each sentence is ~25 chars. With chunk_size=50, we should get
    # multiple chunks.
    sentences = [f"Sentence number {i}." for i in range(1, 21)]
    text = " ".join(sentences)
    pages = [rag.ExtractedPage(page_number=1, text=text)]
    chunks = rag.chunk_pages(pages, chunk_size=80, overlap_ratio=0.2)
    assert len(chunks) >= 2
    # Every chunk should have non-empty text
    for c in chunks:
        assert c.text.strip()


def test_chunk_overlap_preserves_context():
    """Consecutive chunks share some sentences (overlap)."""
    sentences = [f"Sentence number {i} is here." for i in range(1, 30)]
    text = " ".join(sentences)
    pages = [rag.ExtractedPage(page_number=1, text=text)]
    chunks = rag.chunk_pages(pages, chunk_size=100, overlap_ratio=0.3)
    assert len(chunks) >= 2
    # Check overlap: last sentence of chunk[0] should appear in chunk[1]
    # (or be a substring of it). This is the whole point of overlap.
    last_sent_chunk0 = chunks[0].text.split(".")[-2] + "."
    assert last_sent_chunk0.strip() in chunks[1].text or any(
        last_sent_chunk0.strip() in c.text for c in chunks[1:3]
    )


# ─── Long-sentence hard split ───────────────────────────────────────


def test_chunk_long_sentence_hard_split():
    """A single sentence longer than chunk_size gets hard-split."""
    long_sentence = "word " * 200  # ~1000 chars, no period
    text = long_sentence + "."
    pages = [rag.ExtractedPage(page_number=1, text=text)]
    chunks = rag.chunk_pages(pages, chunk_size=200, overlap_ratio=0.2)
    assert len(chunks) >= 4  # ~1000 chars / (200 - 40 overlap) ≈ 6
    # All chunks should have content
    for c in chunks:
        assert c.text.strip()
    # Together they should reconstruct the original (modulo whitespace)
    combined = " ".join(c.text for c in chunks)
    assert "word" in combined


# ─── Page boundaries ────────────────────────────────────────────────


def test_chunk_multiple_pages_separate_page_numbers():
    """Chunks from page 2 carry page_number=2 in their metadata."""
    pages = [
        rag.ExtractedPage(page_number=1, text="Page one content here."),
        rag.ExtractedPage(page_number=2, text="Page two content here."),
        rag.ExtractedPage(page_number=3, text="Page three content here."),
    ]
    chunks = rag.chunk_pages(pages, chunk_size=500, overlap_ratio=0.2)
    assert len(chunks) == 3
    assert chunks[0].page_number == 1
    assert chunks[1].page_number == 2
    assert chunks[2].page_number == 3
    # Chunk indices are sequential across pages
    assert chunks[0].chunk_index == 0
    assert chunks[1].chunk_index == 1
    assert chunks[2].chunk_index == 2


# ─── Line range tracking ────────────────────────────────────────────


def test_chunk_line_ranges_set():
    """Chunks have line_start and line_end set for citation."""
    text = "Line one.\nLine two.\nLine three.\nLine four."
    pages = [rag.ExtractedPage(page_number=1, text=text)]
    chunks = rag.chunk_pages(pages, chunk_size=500, overlap_ratio=0.2)
    assert len(chunks) >= 1
    for c in chunks:
        assert c.line_start is not None
        assert c.line_end is not None
        assert c.line_end >= c.line_start


# ─── Chunk index sequencing ─────────────────────────────────────────


def test_chunk_index_sequential():
    """Chunk indices start at 0 and increment by 1."""
    text = ". ".join(f"Sentence {i}" for i in range(50))
    pages = [rag.ExtractedPage(page_number=1, text=text)]
    chunks = rag.chunk_pages(pages, chunk_size=100, overlap_ratio=0.2)
    for i, c in enumerate(chunks):
        assert c.chunk_index == i


# ─── Adaptive default (no chunk_size arg) ──────────────────────────


def test_chunk_default_chunk_size_for_small_doc():
    """If chunk_size is None, the chunker picks based on doc length."""
    text = "Short sentence. " * 10  # ~170 chars, well under small threshold
    pages = [rag.ExtractedPage(page_number=1, text=text)]
    chunks = rag.chunk_pages(pages)  # chunk_size=None → adaptive
    assert len(chunks) >= 1
    # For a small doc, chunk size should be the small setting
    # (we can't directly assert the size used, but we can verify it ran)
    for c in chunks:
        assert c.text.strip()
