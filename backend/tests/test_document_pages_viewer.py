"""
Tests for the generic document page-image viewer (PDF / DOCX / XLSX).

Extends the PPTX slide viewer pipeline to the other deliverable formats:

  PDF  — rasterized directly (PyMuPDF / pdftoppm). LibreOffice NOT needed.
  DOCX — soffice converts to PDF *inside the cache dir* (never next to
         the source, protecting the download endpoint's probe order),
         then rasterized. LibreOffice required.
  XLSX — identical to DOCX: soffice converts the workbook to PDF into
         the cache dir (paginated by the workbook's print setup), then
         rasterized. LibreOffice required.

Layers covered:

1. Pure units — format detection, cache-dir derivation, rasterizer probe,
   dispatch (pdf needs no soffice; docx/xlsx require it; unsupported → None).
2. API endpoints — /reports/{id}/slides?format=pdf|docx|xlsx manifest +
   /reports/{id}/slides/{n} image serving, via httpx ASGI transport.
   LibreOffice calls are mocked where a real soffice isn't wanted.
3. Download probe order — originals (.pptx/.docx/.xlsx) must win over a
   .pdf conversion artifact sitting next to them.
4. Real pipeline — gated on soffice/PyMuPDF being installed locally.
"""

import asyncio
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import reports  # noqa: E402
from app.services.integrations import libreoffice  # noqa: E402

fitz = pytest.importorskip("fitz", reason="PyMuPDF required for viewer tests")


# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════


def _make_pdf(path: Path, page_labels=("Page 1", "Page 2", "Page 3")) -> Path:
    """Build a small real PDF with PyMuPDF (one text line per page)."""
    doc = fitz.open()
    for label in page_labels:
        page = doc.new_page(width=595, height=842)  # A4
        page.insert_text((72, 96), label, fontsize=24)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    doc.close()
    return path


def _make_docx(path: Path, paragraphs=("Alpha intro", "Beta body")) -> Path:
    """Build a small real DOCX with python-docx."""
    from docx import Document

    doc = Document()
    doc.add_heading("Test Document", level=1)
    for p in paragraphs:
        doc.add_paragraph(p)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    return path


def _make_xlsx(
    path: Path,
    rows: int = 40,
    with_print_setup: bool = True,
) -> Path:
    """Build a small real XLSX with openpyxl (a table + header row).

    When ``with_print_setup`` is set, the sheet carries the same print
    defaults the excel_gen service writes (landscape A4, fit-to-width,
    repeated title rows) — mirroring a genuine generated deliverable.
    """
    from openpyxl import Workbook
    from openpyxl.worksheet.properties import PageSetupProperties

    wb = Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    headers = ["Month", "Payment", "Principal", "Interest", "Balance"]
    ws.append(headers)
    for i in range(rows):
        ws.append(
            [f"2026-{(i % 12) + 1:02d}", 766.23, 641.23, 125.00, 25000 - i * 641.23]
        )

    if with_print_setup:
        ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
        ws.page_setup.orientation = "landscape"
        ws.page_setup.paperSize = ws.PAPERSIZE_A4
        ws.page_setup.fitToWidth = 1
        ws.page_setup.fitToHeight = 0
        ws.freeze_panes = "A2"
        ws.print_title_rows = "1:1"

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    return path


def _make_client(tmp_data: Path, monkeypatch) -> "_SyncASGIClient":
    """httpx client wired to a FastAPI app whose reports data dir is tmp_data.

    Returns a SYNC facade: the tests use the plain ``with client:`` /
    ``client.get(...)`` idiom (the pre-0.24 httpx AsyncClient behavior).
    Modern httpx removed the sync context manager from AsyncClient, so
    the facade runs each request through asyncio.run() — the ASGI
    transport needs no real sockets, so per-request event loops are
    safe.
    """
    app = FastAPI()
    app.include_router(reports.router, prefix="/api")
    monkeypatch.setattr(reports, "_get_data_dir", lambda: tmp_data)
    return _SyncASGIClient(app)


class _SyncASGIClient:
    """Sync ``with``-able facade over httpx.AsyncClient + ASGITransport."""

    def __init__(self, app: FastAPI):
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://testserver",
        )

    def get(self, url: str, **kwargs) -> httpx.Response:
        return asyncio.run(self._client.get(url, **kwargs))

    def __enter__(self) -> "_SyncASGIClient":
        return self

    def __exit__(self, *exc) -> bool:
        try:
            asyncio.run(self._client.aclose())
        except RuntimeError:
            pass  # loop already closed — nothing to clean up
        return False


def _fake_manifest(count: int = 2) -> dict:
    return {
        "version": libreoffice.SLIDES_MANIFEST_VERSION,
        "count": count,
        "width": 1200,
        "height": 1600,
        "notes": [""] * count,
        "source_mtime": 0.0,
        "source_size": 0,
        "generated_at": time.time(),
    }


# ══════════════════════════════════════════════════════════════════════
# 1. Pure units — format detection + dispatch
# ══════════════════════════════════════════════════════════════════════


def test_viewer_format_of():
    assert libreoffice.viewer_format_of(Path("/x/a.pptx")) == "pptx"
    assert libreoffice.viewer_format_of(Path("/x/a.pdf")) == "pdf"
    assert libreoffice.viewer_format_of(Path("/x/a.docx")) == "docx"
    assert libreoffice.viewer_format_of(Path("/x/a.xlsx")) == "xlsx"
    assert libreoffice.viewer_format_of(Path("/x/a.PDF")) == "pdf"
    assert libreoffice.viewer_format_of(Path("/x/a.Docx")) == "docx"
    assert libreoffice.viewer_format_of(Path("/x/a.XLSX")) == "xlsx"
    assert libreoffice.viewer_format_of(Path("/x/a.txt")) is None
    assert libreoffice.viewer_format_of(Path("/x/a.xls")) is None  # legacy, unsupported
    assert libreoffice.viewer_format_of(Path("/x/a")) is None


def test_viewer_supported_formats_constant():
    assert set(libreoffice.VIEWER_SUPPORTED_FORMATS) == {
        "pptx",
        "pdf",
        "docx",
        "xlsx",
    }


def test_viewer_cache_dir_matches_slides_convention(tmp_path):
    for name in ("abc.pptx", "abc.pdf", "abc.docx", "abc.xlsx"):
        src = tmp_path / "reports" / name
        assert libreoffice.viewer_cache_dir(src) == tmp_path / "reports" / "abc_slides"


def test_can_rasterize_pdf_returns_bool():
    assert isinstance(libreoffice.can_rasterize_pdf(), bool)
    # fitz is importable in this suite (importorskip) → must be True.
    assert libreoffice.can_rasterize_pdf() is True


def test_convert_unsupported_format_returns_none(tmp_path):
    src = tmp_path / "a.txt"
    src.write_text("hello")
    assert asyncio.run(libreoffice.convert_document_to_page_images(src)) is None


def test_convert_missing_source_returns_none(tmp_path):
    assert (
        asyncio.run(libreoffice.convert_document_to_page_images(tmp_path / "nope.pdf"))
        is None
    )


def test_pdf_renders_without_libreoffice(tmp_path, monkeypatch):
    """PDF preview must NOT depend on LibreOffice — no conversion involved."""
    reports_dir = tmp_path / "reports"
    pdf = _make_pdf(reports_dir / "doc1.pdf")

    monkeypatch.setattr(libreoffice, "is_available", lambda: False)

    manifest = asyncio.run(libreoffice.convert_document_to_page_images(pdf))
    assert manifest is not None
    assert manifest["count"] == 3
    # Notes are PPTX-only → all empty strings for a PDF.
    assert manifest["notes"] == ["", "", ""]

    cache = libreoffice.viewer_cache_dir(pdf)
    for i in range(1, 4):
        assert (cache / libreoffice.slide_image_name(i)).is_file()
        assert (cache / libreoffice.slide_image_name(i, thumb=True)).is_file()


def test_pdf_cache_short_circuit(tmp_path, monkeypatch):
    """A fresh cache must serve without re-rendering (renderer would boom)."""
    reports_dir = tmp_path / "reports"
    pdf = _make_pdf(reports_dir / "doc2.pdf")

    m1 = asyncio.run(libreoffice.convert_document_to_page_images(pdf))
    assert m1 is not None

    def boom(*a, **kw):
        raise AssertionError("renderer must not run on a fresh cache")

    monkeypatch.setattr(libreoffice, "_render_pdf_pages_with_fitz", boom)
    monkeypatch.setattr(libreoffice, "_render_pdf_pages_with_pdftoppm", boom)

    m2 = asyncio.run(libreoffice.convert_document_to_page_images(pdf))
    assert m2 == m1


def test_pdf_cache_invalidated_on_source_change(tmp_path):
    reports_dir = tmp_path / "reports"
    pdf = _make_pdf(reports_dir / "doc3.pdf", page_labels=("Only page",))

    m1 = asyncio.run(libreoffice.convert_document_to_page_images(pdf))
    assert m1["count"] == 1

    # Regenerate the source with 2 pages (different mtime + size).
    _make_pdf(pdf, page_labels=("Page 1", "Page 2"))

    m2 = asyncio.run(libreoffice.convert_document_to_page_images(pdf))
    assert m2["count"] == 2


def test_docx_requires_libreoffice(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    docx = _make_docx(reports_dir / "doc4.docx")

    monkeypatch.setattr(libreoffice, "is_available", lambda: False)

    assert asyncio.run(libreoffice.convert_document_to_page_images(docx)) is None


def test_docx_conversion_failure_returns_none(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    docx = _make_docx(reports_dir / "doc5.docx")

    monkeypatch.setattr(libreoffice, "is_available", lambda: True)

    async def failing_soffice(args, timeout=None):
        return 1, "", "boom"

    monkeypatch.setattr(libreoffice, "_run_soffice", failing_soffice)

    assert asyncio.run(libreoffice.convert_document_to_page_images(docx)) is None


def test_xlsx_requires_libreoffice(tmp_path, monkeypatch):
    """XLSX preview needs soffice (workbook → PDF conversion) — like DOCX."""
    reports_dir = tmp_path / "reports"
    xlsx = _make_xlsx(reports_dir / "sheet.xlsx")

    monkeypatch.setattr(libreoffice, "is_available", lambda: False)

    assert asyncio.run(libreoffice.convert_document_to_page_images(xlsx)) is None


def test_xlsx_conversion_failure_returns_none(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    xlsx = _make_xlsx(reports_dir / "sheet2.xlsx")

    monkeypatch.setattr(libreoffice, "is_available", lambda: True)

    async def failing_soffice(args, timeout=None):
        return 1, "", "boom"

    monkeypatch.setattr(libreoffice, "_run_soffice", failing_soffice)

    assert asyncio.run(libreoffice.convert_document_to_page_images(xlsx)) is None


def test_pptx_delegates_to_pptx_pipeline(tmp_path, monkeypatch):
    """The generic entry point must reuse the specialized PPTX pipeline."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    pptx = reports_dir / "deck.pptx"
    pptx.write_bytes(b"fake")

    calls = []

    async def fake_pptx_pipeline(path, cache_dir=None, *a, **kw):
        calls.append(path)
        return _fake_manifest(4)

    monkeypatch.setattr(libreoffice, "convert_pptx_to_slide_images", fake_pptx_pipeline)

    manifest = asyncio.run(libreoffice.convert_document_to_page_images(pptx))
    assert manifest is not None
    assert manifest["count"] == 4
    assert calls == [pptx]


# ══════════════════════════════════════════════════════════════════════
# 2. API endpoints
# ══════════════════════════════════════════════════════════════════════


def test_slides_endpoint_pdf_manifest(tmp_path, monkeypatch):
    """?format=pdf renders a real PDF (fitz) and returns the manifest."""
    reports_dir = tmp_path / "reports"
    _make_pdf(reports_dir / "pdfdoc.pdf", page_labels=("A", "B"))

    monkeypatch.setattr(libreoffice, "is_available", lambda: False)  # no LO needed

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/pdfdoc/slides?format=pdf")
        assert r.status_code == 200
        data = r.json()

    assert data["report_id"] == "pdfdoc"
    assert data["count"] == 2
    assert len(data["slides"]) == 2
    for s in data["slides"]:
        assert s["notes"] == ""
        assert f"/api/reports/pdfdoc/slides/{s['index']}?v=" in s["url"]
        assert "variant=thumb" in s["thumb_url"]


def test_slides_endpoint_pdf_no_rasterizer(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    _make_pdf(reports_dir / "pdfdoc.pdf")

    monkeypatch.setattr(libreoffice, "can_rasterize_pdf", lambda: False)

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/pdfdoc/slides?format=pdf")
        assert r.status_code == 404
        assert "PDF preview unavailable" in r.json()["detail"]


def test_slides_endpoint_docx_missing_libreoffice(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    _make_docx(reports_dir / "worddoc.docx")

    monkeypatch.setattr(libreoffice, "is_available", lambda: False)
    monkeypatch.setattr(libreoffice, "can_rasterize_pdf", lambda: True)

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/worddoc/slides?format=docx")
        assert r.status_code == 404
        assert "LibreOffice" in r.json()["detail"]
        assert ".docx" in r.json()["detail"]


def test_slides_endpoint_xlsx_missing_libreoffice(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    _make_xlsx(reports_dir / "sheetdoc.xlsx")

    monkeypatch.setattr(libreoffice, "is_available", lambda: False)
    monkeypatch.setattr(libreoffice, "can_rasterize_pdf", lambda: True)

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/sheetdoc/slides?format=xlsx")
        assert r.status_code == 404
        assert "LibreOffice" in r.json()["detail"]
        assert ".xlsx" in r.json()["detail"]


def test_slides_endpoint_docx_manifest(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    docx = _make_docx(reports_dir / "worddoc.docx")

    monkeypatch.setattr(libreoffice, "is_available", lambda: True)

    async def fake_convert(source_path, cache_dir=None, *a, **kw):
        assert source_path == docx
        return _fake_manifest(2)

    monkeypatch.setattr(libreoffice, "convert_document_to_page_images", fake_convert)

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/worddoc/slides?format=docx")
        assert r.status_code == 200
        data = r.json()

    assert data["count"] == 2
    assert data["slides"][0]["index"] == 1
    assert data["slides"][1]["index"] == 2
    assert all(s["notes"] == "" for s in data["slides"])


def test_slides_endpoint_xlsx_manifest(tmp_path, monkeypatch):
    """?format=xlsx is accepted and flows through the same manifest path."""
    reports_dir = tmp_path / "reports"
    xlsx = _make_xlsx(reports_dir / "sheetdoc.xlsx")

    monkeypatch.setattr(libreoffice, "is_available", lambda: True)

    async def fake_convert(source_path, cache_dir=None, *a, **kw):
        assert source_path == xlsx
        return _fake_manifest(3)

    monkeypatch.setattr(libreoffice, "convert_document_to_page_images", fake_convert)

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/sheetdoc/slides?format=xlsx")
        assert r.status_code == 200
        data = r.json()

    assert data["report_id"] == "sheetdoc"
    assert data["count"] == 3
    assert len(data["slides"]) == 3
    assert all(s["notes"] == "" for s in data["slides"])  # notes are PPTX-only


def test_slides_endpoint_bad_format_rejected(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/whatever/slides?format=csv")
        assert r.status_code == 422


def test_slides_endpoint_default_format_is_pptx(tmp_path, monkeypatch):
    """Backward compat: no ?format= → pptx semantics (404 detail mentions PPTX)."""
    reports_dir = tmp_path / "reports"
    _make_pdf(reports_dir / "onlypdf.pdf")  # only a PDF exists

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/onlypdf/slides")
        assert r.status_code == 404
        assert "PPTX file not found" in r.json()["detail"]


def test_slide_image_endpoint_serves_pdf_page(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    _make_pdf(reports_dir / "pdfdoc.pdf", page_labels=("A",))

    monkeypatch.setattr(libreoffice, "is_available", lambda: False)

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/pdfdoc/slides?format=pdf")
        assert r.status_code == 200

        r = c.get("/api/reports/pdfdoc/slides/1")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content[:2] == b"\xff\xd8"  # JPEG magic

        r = c.get("/api/reports/pdfdoc/slides/1?variant=thumb")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"

        r = c.get("/api/reports/pdfdoc/slides/5")
        assert r.status_code == 404


def test_slides_endpoint_pdf_cache_short_circuit(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    _make_pdf(reports_dir / "pdfdoc.pdf", page_labels=("A",))

    monkeypatch.setattr(libreoffice, "is_available", lambda: False)

    with _make_client(tmp_path, monkeypatch) as c:
        r1 = c.get("/api/reports/pdfdoc/slides?format=pdf")
        assert r1.status_code == 200
        v1 = r1.json()["slides"][0]["url"]

        async def boom(*a, **kw):
            raise AssertionError("convert must not run on a fresh cache")

        monkeypatch.setattr(libreoffice, "convert_document_to_page_images", boom)

        r2 = c.get("/api/reports/pdfdoc/slides?format=pdf")
        assert r2.status_code == 200
        assert r2.json()["slides"][0]["url"] == v1


# ══════════════════════════════════════════════════════════════════════
# 3. Download endpoint probe order (originals before PDF artifacts)
# ══════════════════════════════════════════════════════════════════════


def test_download_prefers_pptx_over_pdf_artifact(tmp_path, monkeypatch):
    """After previewing a PPTX report, {id}.pdf exists next to {id}.pptx —
    the download button must still serve the original presentation."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "deck.pptx").write_bytes(b"original pptx bytes")
    (reports_dir / "deck.pdf").write_bytes(b"conversion artifact")

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/deck/download")
        assert r.status_code == 200
        assert "presentationml.presentation" in r.headers["content-type"]
        assert "report.pptx" in r.headers.get("content-disposition", "")
        assert r.content == b"original pptx bytes"


def test_download_prefers_docx_over_pdf_artifact(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "paper.docx").write_bytes(b"original docx bytes")
    (reports_dir / "paper.pdf").write_bytes(b"conversion artifact")

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/paper/download")
        assert r.status_code == 200
        assert "wordprocessingml.document" in r.headers["content-type"]
        assert "report.docx" in r.headers.get("content-disposition", "")
        assert r.content == b"original docx bytes"


def test_download_prefers_xlsx_over_pdf_artifact(tmp_path, monkeypatch):
    """After previewing an XLSX report, the conversion PDF lives inside the
    cache dir — but even if a stray {id}.pdf appeared, the download button
    must still serve the original workbook (xlsx is probed before pdf)."""
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "book.xlsx").write_bytes(b"original xlsx bytes")
    (reports_dir / "book.pdf").write_bytes(b"conversion artifact")

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/book/download")
        assert r.status_code == 200
        assert "spreadsheetml.sheet" in r.headers["content-type"]
        assert "report.xlsx" in r.headers.get("content-disposition", "")
        assert r.content == b"original xlsx bytes"


def test_download_pdf_report_still_works(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / "only.pdf").write_bytes(b"real pdf report")

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/only/download")
        assert r.status_code == 200
        assert r.headers["content-type"] == "application/pdf"
        assert "report.pdf" in r.headers.get("content-disposition", "")
        assert r.content == b"real pdf report"


def test_download_404_when_nothing_exists(tmp_path, monkeypatch):
    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/ghost/download")
        assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# 4. Real pipeline (gated on soffice + PyMuPDF)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.skipif(not libreoffice.is_available(), reason="LibreOffice not installed")
def test_docx_pdf_lands_in_cache_not_reports_dir(tmp_path):
    """The DOCX→PDF conversion must live INSIDE the cache dir so it can
    never hijack the download endpoint's extension probing."""
    reports_dir = tmp_path / "reports"
    docx = _make_docx(reports_dir / "real.docx")

    manifest = asyncio.run(libreoffice.convert_document_to_page_images(docx))
    assert manifest is not None
    assert manifest["count"] >= 1
    assert manifest["notes"] == [""] * manifest["count"]

    cache = libreoffice.viewer_cache_dir(docx)
    assert (cache / f"{docx.stem}.pdf").is_file()  # inside the cache
    assert not (reports_dir / f"{docx.stem}.pdf").exists()  # NOT in reports/


@pytest.mark.skipif(not libreoffice.is_available(), reason="LibreOffice not installed")
def test_docx_renders_non_blank_pages(tmp_path):
    reports_dir = tmp_path / "reports"
    docx = _make_docx(
        reports_dir / "real2.docx",
        paragraphs=tuple(
            f"Paragraph {i}: the quick brown fox jumps over the lazy dog. "
            "This line exists so the rendered page carries visible text."
            for i in range(1, 25)
        ),
    )

    manifest = asyncio.run(libreoffice.convert_document_to_page_images(docx))
    assert manifest is not None

    from PIL import Image

    img = Image.open(
        libreoffice.viewer_cache_dir(docx) / libreoffice.slide_image_name(1)
    )
    assert img.size[0] > 0
    # Non-blank check: pixel standard deviation must be meaningful.
    import statistics

    grayscale = img.convert("L")
    pixels = list(grayscale.getdata())[:: max(1, (img.size[0] * img.size[1]) // 5000)]
    assert statistics.pstdev(pixels) > 10


@pytest.mark.skipif(not libreoffice.is_available(), reason="LibreOffice not installed")
def test_docx_endpoint_e2e(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    _make_docx(reports_dir / "e2e.docx")

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/e2e/slides?format=docx")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 1
        assert all(s["notes"] == "" for s in data["slides"])

        r = c.get("/api/reports/e2e/slides/1")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"

        # The conversion artifact must NOT leak into data/reports/.
        assert not (reports_dir / "e2e.pdf").exists()


@pytest.mark.skipif(not libreoffice.is_available(), reason="LibreOffice not installed")
def test_pdf_endpoint_e2e(tmp_path, monkeypatch):
    reports_dir = tmp_path / "reports"
    _make_pdf(reports_dir / "e2e.pdf", page_labels=("Intro", "Body", "End"))

    monkeypatch.setattr(libreoffice, "is_available", lambda: False)

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/e2e/slides?format=pdf")
        assert r.status_code == 200
        assert r.json()["count"] == 3

        r = c.get("/api/reports/e2e/slides/3")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"


@pytest.mark.skipif(not libreoffice.is_available(), reason="LibreOffice not installed")
def test_xlsx_pdf_lands_in_cache_not_reports_dir(tmp_path):
    """The XLSX→PDF conversion must live INSIDE the cache dir so it can
    never hijack the download endpoint's extension probing (and a bare
    {id}.xlsx must always remain the original deliverable)."""
    reports_dir = tmp_path / "reports"
    xlsx = _make_xlsx(reports_dir / "real.xlsx")

    manifest = asyncio.run(libreoffice.convert_document_to_page_images(xlsx))
    assert manifest is not None
    assert manifest["count"] >= 1
    assert manifest["notes"] == [""] * manifest["count"]

    cache = libreoffice.viewer_cache_dir(xlsx)
    assert (cache / f"{xlsx.stem}.pdf").is_file()  # inside the cache
    assert not (reports_dir / f"{xlsx.stem}.pdf").exists()  # NOT in reports/


@pytest.mark.skipif(not libreoffice.is_available(), reason="LibreOffice not installed")
def test_xlsx_renders_non_blank_pages(tmp_path):
    """A 40-row table must render with visible content — and with the
    workbook's print setup (fit-to-width), a 5-column sheet must NOT be
    sliced across two column-pages: every page should be a wide
    (landscape) page carrying all five columns."""
    reports_dir = tmp_path / "reports"
    xlsx = _make_xlsx(reports_dir / "real2.xlsx", rows=40)

    manifest = asyncio.run(libreoffice.convert_document_to_page_images(xlsx))
    assert manifest is not None
    assert manifest["count"] >= 1

    from PIL import Image
    import statistics

    img = Image.open(
        libreoffice.viewer_cache_dir(xlsx) / libreoffice.slide_image_name(1)
    )
    assert img.size[0] > 0
    # Non-blank check: pixel standard deviation must be meaningful.
    grayscale = img.convert("L")
    pixels = list(grayscale.getdata())[:: max(1, (img.size[0] * img.size[1]) // 5000)]
    assert statistics.pstdev(pixels) > 10

    # fit-to-width contract: each rendered page is wider than tall
    # (landscape A4 ≈ 1.414 aspect). A portrait page would mean the
    # print setup was ignored and columns got sliced off.
    for i in range(1, manifest["count"] + 1):
        page = Image.open(
            libreoffice.viewer_cache_dir(xlsx) / libreoffice.slide_image_name(i)
        )
        assert (
            page.size[0] > page.size[1]
        ), f"page {i} is {page.size[0]}x{page.size[1]} — expected landscape"


@pytest.mark.skipif(not libreoffice.is_available(), reason="LibreOffice not installed")
def test_xlsx_endpoint_e2e(tmp_path, monkeypatch):
    """Full ?format=xlsx flow through the real API: manifest + page JPEGs."""
    reports_dir = tmp_path / "reports"
    _make_xlsx(reports_dir / "e2e.xlsx", rows=25)

    with _make_client(tmp_path, monkeypatch) as c:
        r = c.get("/api/reports/e2e/slides?format=xlsx")
        assert r.status_code == 200
        data = r.json()
        assert data["count"] >= 1
        assert all(s["notes"] == "" for s in data["slides"])

        r = c.get("/api/reports/e2e/slides/1")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert r.content[:2] == b"\xff\xd8"  # JPEG magic

        r = c.get("/api/reports/e2e/slides/1?variant=thumb")
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"

        # The conversion artifact must NOT leak into data/reports/.
        assert not (reports_dir / "e2e.pdf").exists()


@pytest.mark.skipif(not libreoffice.is_available(), reason="LibreOffice not installed")
def test_xlsx_multi_page_table_repeats_headers(tmp_path):
    """A table long enough to span several pages must carry the repeated
    title rows (print_title_rows) on every page — the header text has to
    appear on page 2 as well, not only on page 1."""
    import fitz as pymupdf

    reports_dir = tmp_path / "reports"
    xlsx = _make_xlsx(reports_dir / "long.xlsx", rows=120)

    manifest = asyncio.run(libreoffice.convert_document_to_page_images(xlsx))
    assert manifest is not None
    assert manifest["count"] >= 2, "120 rows should paginate into 2+ pages"

    # Inspect the intermediate PDF (inside the cache dir) — cheap and
    # more precise than OCR on the JPEGs.
    pdf_path = libreoffice.viewer_cache_dir(xlsx) / f"{xlsx.stem}.pdf"
    doc = pymupdf.open(str(pdf_path))
    with doc:
        page2_text = doc[1].get_text()
        assert (
            "Month" in page2_text
        ), "repeated header row (print_title_rows) missing on page 2"
