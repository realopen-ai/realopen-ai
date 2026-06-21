"""
Tests for the RAG text extraction layer.

Each extractor takes raw bytes and returns an ExtractionResult with pages
and (optionally) extracted images. These tests verify the happy-path
behavior for the formats we support: TXT, MD, CSV, XLSX, DOCX, PDF.

We focus on observable behavior — page count, text presence, image
count — rather than exact byte-for-byte output, so the tests stay
maintainable as we tweak the extractors.
"""

import io

import pytest

from app.services import rag

# ─── TXT ─────────────────────────────────────────────────────────────


def test_extract_txt_basic():
    """Plain text becomes a single page with the full content."""
    buf = b"Hello world\nThis is a test document."
    result = rag.extract_content(buf, "test.txt")
    assert result.mime_type == "text/plain"
    assert len(result.pages) == 1
    assert "Hello world" in result.pages[0].text
    assert "test document" in result.pages[0].text
    assert result.pages[0].page_number == 1
    assert result.images == []


def test_extract_txt_utf8():
    """UTF-8 text with non-ASCII characters survives extraction."""
    buf = "Café résumé naïve — 日本語".encode("utf-8")
    result = rag.extract_content(buf, "unicode.txt")
    assert "Café" in result.pages[0].text
    assert "日本語" in result.pages[0].text


def test_extract_txt_latin1_fallback():
    """Non-UTF-8 bytes fall back to latin-1 decoding (no exception)."""
    # 0xff is not valid as the first byte of a UTF-8 sequence
    buf = b"\xff\xfeHello"
    result = rag.extract_content(buf, "latin1.txt")
    assert len(result.pages) == 1
    # The text may have replacement chars but should not raise
    assert "Hello" in result.pages[0].text


# ─── Markdown ────────────────────────────────────────────────────────


def test_extract_markdown_treated_as_text():
    """Markdown is extracted as plain text — we don't render it."""
    buf = b"# Title\n\nSome **bold** text and [a link](https://example.com)."
    result = rag.extract_content(buf, "doc.md")
    assert len(result.pages) == 1
    assert "# Title" in result.pages[0].text
    assert "**bold**" in result.pages[0].text


# ─── CSV / TSV ───────────────────────────────────────────────────────


def test_extract_csv_basic():
    """CSV is kept as text — one page."""
    buf = b"name,age\nAlice,30\nBob,25\n"
    result = rag.extract_content(buf, "data.csv")
    assert result.mime_type == "text/csv"
    assert len(result.pages) == 1
    assert "Alice" in result.pages[0].text
    assert "Bob" in result.pages[0].text


def test_extract_tsv_uses_csv_extractor():
    """TSV falls through to the CSV extractor (same shape)."""
    buf = b"name\tage\nAlice\t30\n"
    result = rag.extract_content(buf, "data.tsv")
    assert len(result.pages) == 1
    assert "Alice" in result.pages[0].text


# ─── XLSX ────────────────────────────────────────────────────────────


def test_extract_xlsx_multiple_sheets():
    """Each sheet becomes a separate page, with the sheet name in the text."""
    import openpyxl

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Sheet1"
    ws1.append(["name", "age"])
    ws1.append(["Alice", 30])
    ws2 = wb.create_sheet("Sheet2")
    ws2.append(["city", "pop"])
    ws2.append(["NYC", 8_000_000])

    buf = io.BytesIO()
    wb.save(buf)
    result = rag.extract_content(buf.getvalue(), "wb.xlsx")
    assert len(result.pages) == 2
    assert "Sheet1" in result.pages[0].text
    assert "Alice" in result.pages[0].text
    assert "Sheet2" in result.pages[1].text
    assert "NYC" in result.pages[1].text


# ─── DOCX ────────────────────────────────────────────────────────────


def test_extract_docx_text_and_images():
    """DOCX yields paragraph text + any embedded images."""
    import docx
    from docx.shared import Inches

    doc = docx.Document()
    doc.add_paragraph("Hello from DOCX.")
    doc.add_paragraph("Second paragraph here.")

    # Add a 1x1 PNG image so we exercise the image-extraction path.
    import struct
    import zlib

    def _minimal_png(width=2, height=2) -> bytes:
        """Build a minimal valid PNG (solid color)."""
        signature = b"\x89PNG\r\n\x1a\n"
        ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        ihdr = b"IHDR" + ihdr_data
        ihdr_chunk = (
            struct.pack(">I", len(ihdr_data))
            + ihdr
            + struct.pack(">I", zlib.crc32(ihdr) & 0xFFFFFFFF)
        )
        raw = b""
        for _ in range(height):
            raw += b"\x00" + b"\xff\x00\x00" * width
        compressed = zlib.compress(raw)
        idat = b"IDAT" + compressed
        idat_chunk = (
            struct.pack(">I", len(compressed))
            + idat
            + struct.pack(">I", zlib.crc32(idat) & 0xFFFFFFFF)
        )
        iend = b"IEND"
        iend_chunk = (
            struct.pack(">I", 0)
            + iend
            + struct.pack(">I", zlib.crc32(iend) & 0xFFFFFFFF)
        )
        return signature + ihdr_chunk + idat_chunk + iend_chunk

    png_bytes = _minimal_png()
    image_stream = io.BytesIO(png_bytes)
    doc.add_picture(image_stream, width=Inches(1))

    buf = io.BytesIO()
    doc.save(buf)

    result = rag.extract_content(buf.getvalue(), "test.docx")
    assert len(result.pages) == 1
    assert "Hello from DOCX" in result.pages[0].text
    assert "Second paragraph" in result.pages[0].text
    # Image extraction — we should pick up at least one image.
    assert len(result.images) >= 1
    assert result.images[0].image_bytes  # non-empty bytes


# ─── PDF ─────────────────────────────────────────────────────────────


def test_extract_pdf_text():
    """A generated PDF yields page-level text.

    Uses reportlab if available; if not, we skip — the test env may not
    have it installed (it's not in pyproject.toml).
    """
    pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas  # pyright: ignore[reportMissingModuleSource]

    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(100, 750, "Page one content.")
    c.showPage()
    c.drawString(100, 750, "Page two content.")
    c.showPage()
    c.save()

    result = rag.extract_content(buf.getvalue(), "test.pdf")
    assert result.mime_type == "application/pdf"
    assert len(result.pages) >= 2
    all_text = " ".join(p.text for p in result.pages)
    assert "Page one" in all_text
    assert "Page two" in all_text


# ─── Unknown extension fallback ──────────────────────────────────────


def test_unknown_extension_falls_back_to_text():
    """Unknown ext → fall back to text extraction (logged warning)."""
    buf = b"Hello unknown format."
    result = rag.extract_content(buf, "file.unknownext")
    assert len(result.pages) == 1
    assert "Hello unknown format" in result.pages[0].text


# ─── Empty input ─────────────────────────────────────────────────────


def test_extract_empty_txt():
    """Empty TXT yields a single empty page (no crash)."""
    result = rag.extract_content(b"", "empty.txt")
    assert len(result.pages) == 1
    assert result.pages[0].text == ""
