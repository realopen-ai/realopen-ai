"""
Tests for the rewritten report generation service.

Covers four layers:

1. Markdown structure units — title/subtitle/body splitting, inline-marker
   stripping (no external deps).
2. HTML helpers — TOC entry extraction, executive-summary callout wrapping.
3. PDF e2e (gated on weasyprint + markdown + pymupdf) — full pipeline with
   the MARGIN REGRESSION TEST: every page's text must respect the page
   margins (the old bug: first content page was full-bleed while the rest
   had margins).
4. DOCX e2e (gated on python-docx) — title page, TOC field with cached
   entries, styled headings/tables, page-number footer.
5. Public API — generate_report with a mocked LLM returns the exact same
   contract as before (drop-in compatibility with the agent tool).
"""

import sys
import zipfile
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import report_gen  # noqa: E402

SAMPLE_MD = """# The State of Renewable Energy in 2026
*A comprehensive analysis of global trends and technology maturity*

## Executive Summary
Renewable energy has reached an inflection point. Solar and wind now account for **over 30% of new capacity** additions globally.

Investment flows have shifted decisively toward storage and grid infrastructure.

## Global Market Landscape
The global market has grown at a compound annual rate of 8.7%.

### Regional Capacity Share
- **Asia-Pacific**: 48% of installed capacity
- **Europe**: 27% of installed capacity

## Investment Flows
| Region | 2025 Investment | YoY Growth |
|--------|-----------------|------------|
| Asia-Pacific | $214B | +12% |
| Europe | $156B | +9% |

> The energy transition is now an infrastructure program.

## Technology Deep Dive
### Solar Photovoltaics
Module prices have declined 89% since 2010.

```
capacity_factor = 0.52
lcoe = 38.5
```

## Conclusion
The sector has fundamentally transformed.
"""


# ══════════════════════════════════════════════════════════════════════
# 1. Markdown structure units
# ══════════════════════════════════════════════════════════════════════


def test_parse_title_extracts_title_subtitle_body():
    title, subtitle, body = report_gen._parse_title(SAMPLE_MD, "topic")
    assert title == "The State of Renewable Energy in 2026"
    assert subtitle == (
        "A comprehensive analysis of global trends and technology maturity"
    )
    assert not body.startswith("# ")
    assert body.startswith("## Executive Summary")


def test_parse_title_falls_back_to_topic_without_h1():
    title, subtitle, body = report_gen._parse_title(
        "## Only Section\ncontent", "Fallback Topic"
    )
    assert title == "Fallback Topic"
    # title fell back TO the topic (they're equal) → no subtitle.
    assert subtitle == ""
    assert body.startswith("## Only Section")


def test_parse_title_topic_as_subtitle_when_differs():
    title, subtitle, body = report_gen._parse_title("# Real Title\nbody", "My Topic")
    assert title == "Real Title"
    assert subtitle == "My Topic"


def test_parse_title_no_subtitle_when_topic_equals_title():
    title, subtitle, _ = report_gen._parse_title("# X\n\nbody", "X")
    assert subtitle == ""


def test_parse_title_default_when_nothing():
    title, subtitle, body = report_gen._parse_title("just text", None)
    assert title == "Report"
    assert subtitle == ""
    assert body == "just text"


def test_parse_title_strips_inline_markers():
    md = "# **Bold** and `code` title\n\n*sub*\n\nbody"
    title, subtitle, _ = report_gen._parse_title(md, None)
    assert title == "Bold and code title"
    assert subtitle == "sub"


def test_strip_md_inline():
    assert report_gen._strip_md_inline("**bold**") == "bold"
    assert report_gen._strip_md_inline("*it*") == "it"
    assert report_gen._strip_md_inline("`code`") == "code"
    assert report_gen._strip_md_inline("plain") == "plain"


# ══════════════════════════════════════════════════════════════════════
# 2. HTML helpers
# ══════════════════════════════════════════════════════════════════════


def _markdown_available() -> bool:
    try:
        import markdown  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _markdown_available(), reason="markdown not installed")
def test_extract_toc_entries():
    _, _, body = report_gen._parse_title(SAMPLE_MD, None)
    html = report_gen._markdown_to_html(body)
    entries = report_gen._extract_toc_entries(html)

    levels = [e[0] for e in entries]
    texts = [e[2] for e in entries]
    anchors = [e[1] for e in entries]

    assert "Executive Summary" in texts
    assert "Global Market Landscape" in texts
    assert "Regional Capacity Share" in texts
    assert "Conclusion" in texts
    assert set(levels) <= {"h2", "h3"}
    assert all(anchors), "toc extension must assign ids to headings"


@pytest.mark.skipif(not _markdown_available(), reason="markdown not installed")
def test_wrap_summary_wraps_executive_summary():
    _, _, body = report_gen._parse_title(SAMPLE_MD, None)
    html = report_gen._markdown_to_html(body)
    wrapped = report_gen._wrap_summary(html)

    assert 'class="summary"' in wrapped
    # The callout must start right after the Executive Summary h2 …
    idx_h2 = wrapped.find("Executive Summary</h2>")
    idx_div = wrapped.find('<div class="summary">')
    assert 0 < idx_h2 < idx_div
    # … and must end before the next h2 (Global Market Landscape).
    idx_next = wrapped.find("Global Market Landscape")
    idx_div_end = wrapped.find("</div>", idx_div)
    assert idx_div_end < idx_next


@pytest.mark.skipif(not _markdown_available(), reason="markdown not installed")
def test_wrap_summary_noop_without_summary():
    html = report_gen._markdown_to_html("## Intro\nstuff\n## Outro\nmore")
    assert report_gen._wrap_summary(html) == html


def test_css_escape():
    assert report_gen._css_escape('say "hi"') == 'say \\"hi\\"'
    assert report_gen._css_escape("back\\slash") == "back\\\\slash"
    assert report_gen._css_escape("line\nbreak") == "line break"


def test_pdf_css_has_no_first_page_margin_zero():
    """The regression guard: the old bug came from `@page :first { margin: 0 }`
    applying to the FIRST CONTENT page (there was no cover). The new CSS must
    only give the named `cover` page zero margins — never `:first`."""
    assert "@page :first" not in report_gen._PDF_CSS
    assert "@page cover" in report_gen._PDF_CSS
    cover_rule = report_gen._PDF_CSS.split("@page cover")[1].split("}")[0]
    assert "margin: 0" in cover_rule


def test_design_tokens_defined():
    for token in (report_gen.NAVY, report_gen.GOLD, report_gen.INK):
        assert re_fullmatch_hex(token)


def re_fullmatch_hex(value: str) -> bool:
    import re

    return bool(re.fullmatch(r"[0-9A-Fa-f]{6}", value))


# ══════════════════════════════════════════════════════════════════════
# 3. PDF e2e (gated)
# ══════════════════════════════════════════════════════════════════════


def _pdf_stack_available() -> bool:
    try:
        import markdown  # noqa: F401
        import weasyprint  # noqa: F401

        return True
    except ImportError:
        return False


def _fitz_available() -> bool:
    try:
        import fitz  # noqa: F401

        return True
    except ImportError:
        return False


PDF_GATED = pytest.mark.skipif(
    not _pdf_stack_available(), reason="weasyprint/markdown not installed"
)
FITZ_GATED = pytest.mark.skipif(not _fitz_available(), reason="PyMuPDF not installed")


@PDF_GATED
@FITZ_GATED
def test_pdf_structure_cover_toc_content(tmp_path):
    """Cover page → TOC page → numbered content sections."""
    import fitz

    out = tmp_path / "r.pdf"
    report_gen._generate_pdf(SAMPLE_MD, out, topic="Renewable Energy 2026")

    doc = fitz.open(str(out))
    assert len(doc) >= 3

    page1 = " ".join(doc[0].get_text().split())  # normalize line wraps
    assert "The State of Renewable Energy in 2026" in page1
    assert "Renewable Energy 2026" in page1  # cover meta topic

    page2 = " ".join(doc[1].get_text().split())
    assert "Table of Contents" in page2
    assert "Executive Summary" in page2

    page3 = doc[2].get_text()
    assert "01" in page3 and "Executive Summary" in page3
    doc.close()


@PDF_GATED
@FITZ_GATED
def test_pdf_margins_regression(tmp_path):
    """THE user-reported bug: the first content page had no margins while
    the rest did. Now EVERY page (including the cover, whose text sits at
    24mm) must keep its text inside the margins."""
    import fitz

    out = tmp_path / "r.pdf"
    report_gen._generate_pdf(SAMPLE_MD, out, topic="Renewable Energy 2026")

    doc = fitz.open(str(out))
    assert len(doc) >= 3

    # 20mm side margins @ 72dpi ≈ 56.7pt. Allow a small glyph-bearing
    # tolerance, but anything < 45pt means full-bleed text (the old bug
    # produced x0 ≈ 6pt).
    x0s = []
    for i, page in enumerate(doc):
        blocks = [b for b in page.get_text("blocks") if b[4].strip()]
        assert blocks, f"page {i + 1} has no text"
        x0 = min(b[0] for b in blocks)
        x1 = max(b[2] for b in blocks)
        assert x0 >= 45, f"page {i + 1} text starts at x0={x0:.0f} (< 45) — margin bug!"
        assert (
            x1 <= page.rect.width - 45
        ), f"page {i + 1} text ends at x1={x1:.0f} — right margin missing"
        x0s.append(x0)

    # Content pages must share consistent margins (±10pt).
    content_x0s = x0s[1:]  # cover text is positioned at 24mm — its own design
    assert (
        max(content_x0s) - min(content_x0s) <= 10
    ), f"inconsistent content margins: {content_x0s}"
    doc.close()


@PDF_GATED
@FITZ_GATED
def test_pdf_toc_has_page_numbers(tmp_path):
    """TOC entries carry leader dots and target page numbers."""
    import fitz
    import re

    out = tmp_path / "r.pdf"
    report_gen._generate_pdf(SAMPLE_MD, out, topic="Renewable Energy 2026")

    doc = fitz.open(str(out))
    toc_text = doc[1].get_text()
    assert "Table of Contents" in toc_text
    # leader dots
    assert "....." in toc_text
    # entries end with a page number (digit)
    lines = [line.strip() for line in toc_text.split("\n") if line.strip()]
    number_lines = [line for line in lines if re.fullmatch(r"\d+", line)]
    assert len(number_lines) >= 4, f"TOC page numbers missing: {lines}"
    doc.close()


@PDF_GATED
@FITZ_GATED
def test_pdf_header_footer_and_metadata(tmp_path):
    """Content pages carry the running header (title + date) and a
    'Page X of Y' footer; the PDF title metadata is set."""
    import fitz

    out = tmp_path / "r.pdf"
    report_gen._generate_pdf(SAMPLE_MD, out, topic="Renewable Energy 2026")

    doc = fitz.open(str(out))
    assert doc.metadata.get("title") == "The State of Renewable Energy in 2026"

    last = doc[len(doc) - 1].get_text()
    assert f"Page {len(doc)} of {len(doc)}" in last
    assert "The State of Renewable Energy in 2026" in last  # running header

    # The cover must NOT have a footer/header
    cover = doc[0].get_text()
    assert "Page 1 of" not in cover
    doc.close()


@PDF_GATED
@FITZ_GATED
def test_pdf_table_and_sections_present(tmp_path):
    import fitz

    out = tmp_path / "r.pdf"
    report_gen._generate_pdf(SAMPLE_MD, out, topic="Renewable Energy 2026")

    all_text = "".join(p.get_text() for p in fitz.open(str(out)))
    assert "Asia-Pacific" in all_text
    assert "$214B" in all_text
    assert "05" in all_text  # section numbering reaches 05 Conclusion
    assert "capacity_factor = 0.52" in all_text  # code block rendered


@PDF_GATED
def test_pdf_generated_without_topic(tmp_path):
    """Topic is optional — subtitle falls back to nothing."""
    out = tmp_path / "r.pdf"
    report_gen._generate_pdf(SAMPLE_MD, out)
    assert out.exists() and out.stat().st_size > 5000


# ══════════════════════════════════════════════════════════════════════
# 4. DOCX e2e (gated)
# ══════════════════════════════════════════════════════════════════════


def _docx_available() -> bool:
    try:
        import docx  # noqa: F401

        return True
    except ImportError:
        return False


DOCX_GATED = pytest.mark.skipif(
    not _docx_available(), reason="python-docx not installed"
)


@DOCX_GATED
def test_docx_title_page(tmp_path):
    from docx import Document

    out = tmp_path / "r.docx"
    report_gen._generate_docx(SAMPLE_MD, out, topic="Renewable Energy 2026")

    doc = Document(str(out))
    texts = [p.text for p in doc.paragraphs if p.text.strip()]
    assert texts[0] == "R E P O R T"
    assert "The State of Renewable Energy in 2026" in texts[1]
    assert any("A comprehensive analysis" in t for t in texts[:6])
    assert "Renewable Energy 2026" in texts
    assert "Table of Contents" in texts


@DOCX_GATED
def test_docx_toc_field_structure(tmp_path):
    """Real TOC field: begin(dirty) → instr → separate → cached entries → end."""
    out = tmp_path / "r.docx"
    report_gen._generate_docx(SAMPLE_MD, out, topic="t")

    xml = zipfile.ZipFile(str(out)).read("word/document.xml").decode()
    assert 'fldCharType="begin"' in xml
    assert 'w:dirty="true"' in xml
    assert "TOC \\o" in xml
    assert 'fldCharType="separate"' in xml
    assert 'fldCharType="end"' in xml

    # Cached entries are visible as plain text
    from docx import Document

    doc = Document(str(out))
    texts = [p.text for p in doc.paragraphs]
    toc_zone = texts.index("Table of Contents")
    after_toc = [t for t in texts[toc_zone:] if t.strip()][:8]
    assert "Executive Summary" in after_toc
    assert "Conclusion" in after_toc


@DOCX_GATED
def test_docx_heading_styles_styled(tmp_path):
    from docx import Document

    out = tmp_path / "r.docx"
    report_gen._generate_docx(SAMPLE_MD, out, topic="t")

    doc = Document(str(out))
    h2 = doc.styles["Heading 2"]
    assert str(h2.font.color.rgb) == report_gen.NAVY
    assert h2.font.bold is True

    normal = doc.styles["Normal"]
    assert normal.font.size is not None


@DOCX_GATED
def test_docx_table_styling(tmp_path):
    from docx import Document
    from docx.oxml.ns import qn

    out = tmp_path / "r.docx"
    report_gen._generate_docx(SAMPLE_MD, out, topic="t")

    doc = Document(str(out))
    assert len(doc.tables) == 1
    table = doc.tables[0]

    header_cell = table.cell(0, 0)
    shd = header_cell._tc.get_or_add_tcPr().find(qn("w:shd"))
    assert shd is not None and shd.get(qn("w:fill")) == report_gen.NAVY

    # Zebra striping on an even data row (row index 2)
    zebra_cell = table.cell(2, 0)
    shd2 = zebra_cell._tc.get_or_add_tcPr().find(qn("w:shd"))
    assert shd2 is not None and shd2.get(qn("w:fill")) == report_gen.ZEBRA

    # Header text is bold white
    header_run = header_cell.paragraphs[0].runs[0]
    assert header_run.bold is True
    assert str(header_run.font.color.rgb) == "FFFFFF"


@DOCX_GATED
def test_docx_footer_page_fields(tmp_path):
    out = tmp_path / "r.docx"
    report_gen._generate_docx(SAMPLE_MD, out, topic="t")

    xml = zipfile.ZipFile(str(out)).read("word/footer1.xml").decode()
    assert "PAGE" in xml
    assert "NUMPAGES" in xml
    assert "Page" in xml


@DOCX_GATED
def test_docx_different_first_page(tmp_path):
    """The cover page (first page) must not show the page-number footer."""
    from docx import Document

    out = tmp_path / "r.docx"
    report_gen._generate_docx(SAMPLE_MD, out, topic="t")

    doc = Document(str(out))
    section = doc.sections[0]
    assert section.different_first_page_header_footer is True


@DOCX_GATED
def test_docx_code_block_and_quote(tmp_path):
    from docx import Document

    out = tmp_path / "r.docx"
    report_gen._generate_docx(SAMPLE_MD, out, topic="t")

    doc = Document(str(out))
    texts = [p.text for p in doc.paragraphs]
    assert "capacity_factor = 0.52" in texts

    mono = [p for p in doc.paragraphs if p.runs and p.runs[0].font.name == "Consolas"]
    assert mono, "code block must use a monospace font"

    quote = [p for p in doc.paragraphs if "infrastructure program" in p.text]
    assert quote, "blockquote content must be present"


@DOCX_GATED
def test_docx_inline_formatting(tmp_path):
    from docx import Document

    out = tmp_path / "r.docx"
    report_gen._generate_docx(SAMPLE_MD, out, topic="t")

    doc = Document(str(out))
    bold_runs = [
        r
        for p in doc.paragraphs
        for r in p.runs
        if r.bold and r.text.strip() == "over 30% of new capacity"
    ]
    assert bold_runs, "inline **bold** must become a bold run"


# ══════════════════════════════════════════════════════════════════════
# 5. Public API (LLM mocked — no network)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_generate_report_pdf_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(report_gen, "_generate_markdown", _fake_markdown)
    monkeypatch.setattr(report_gen, "_get_reports_dir", lambda: tmp_path)

    result = await report_gen.generate_report(
        topic="Test Topic!", outline=None, format="pdf"
    )

    assert result["type"] == "report"
    assert result["format"] == "pdf"
    assert result["filename"] == "Test_Topic.pdf"
    assert result["report_id"]
    assert result["file_path"] == f"reports/{result['report_id']}.pdf"
    assert result["download_url"] == f"/api/reports/{result['report_id']}/download"
    assert isinstance(result["created_at"], int)
    assert (tmp_path / f"{result['report_id']}.pdf").exists()


@pytest.mark.asyncio
async def test_generate_report_docx_contract(tmp_path, monkeypatch):
    monkeypatch.setattr(report_gen, "_generate_markdown", _fake_markdown)
    monkeypatch.setattr(report_gen, "_get_reports_dir", lambda: tmp_path)

    result = await report_gen.generate_report(topic="Another Topic", format="docx")
    assert result["format"] == "docx"
    assert result["filename"] == "Another_Topic.docx"
    assert (tmp_path / f"{result['report_id']}.docx").exists()


@pytest.mark.asyncio
async def test_generate_report_invalid_format_defaults_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(report_gen, "_generate_markdown", _fake_markdown)
    monkeypatch.setattr(report_gen, "_get_reports_dir", lambda: tmp_path)

    result = await report_gen.generate_report(topic="X", format="pptx")
    assert result["format"] == "pdf"


async def _fake_markdown(topic: str, outline) -> str:
    return SAMPLE_MD
