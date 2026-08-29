"""
Report generation service — LLM generates Markdown, then converts to
PDF (via WeasyPrint) or DOCX (via python-docx).

Pipeline:
  1. Call the chat model with a report-generation prompt (topic + outline)
     → produces a well-structured Markdown document.
  2. Convert Markdown → target format:
     - PDF: Markdown → HTML → a fully designed document with a full-bleed
       cover page, a table of contents (with dotted leaders + page numbers),
       numbered sections, styled tables/callouts, running headers and
       "Page X of Y" footers. All content pages share identical margins —
       only the cover is full-bleed, by design.
     - DOCX: Markdown → a styled Word document with a title page, a TOC
       field (auto-updates in Word), colored headings, zebra tables,
       page-number footer and code-block styling.
  3. Save to data/reports/{report_id}.{ext}

Both paths consume the same Markdown, so the LLM generation prompt is
identical regardless of output format.

Design system (shared by both formats):
  Navy   #16304F — headings, table headers, cover background
  Steel  #1B3A5C — sub-headings
  Gold   #C9A227 — accents (cover band, rules, section numbers, markers)
  Ink    #2A2F36 — body text     Muted #5C6470 — secondary text
  Zebra  #F4F6F9 — table striping / callout tint #FAF6EA

WeasyPrint requires system libs (pango, cairo) — see the backend
Dockerfile for the apt-get install. If WeasyPrint import fails at
runtime (e.g. missing libs), PDF generation degrades gracefully: the
tool returns an error instead of crashing the backend.
"""

from __future__ import annotations

import asyncio
import html as _html
import logging
import re
import time
import uuid
from datetime import date as _date
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[report_gen] {formatted}", flush=True)


# ── Data directory ───────────────────────────────────────────────────


def _get_reports_dir() -> Path:
    """Get the reports directory under data/. Creates it if missing."""
    candidates = [
        Path("/app/data/reports"),
        Path(__file__).resolve().parent.parent.parent / "data" / "reports",
    ]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    fallback = Path("/app/data/reports")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


# ── Design tokens (shared by PDF + DOCX) ─────────────────────────────

NAVY = "16304F"  # headings, table headers, cover
STEEL = "1B3A5C"  # sub-headings
GOLD = "C9A227"  # accents
INK = "2A2F36"  # body text
MUTED = "5C6470"  # secondary text
ZEBRA = "F4F6F9"  # table striping
CALLOUT_BG = "FAF6EA"  # blockquote tint
CODE_BG = "F1F3F6"  # code background


# ── LLM report generation prompt ─────────────────────────────────────

REPORT_SYSTEM_PROMPT = """You are a professional report writer. Generate a comprehensive, well-structured report in Markdown format about the given topic.

The report MUST include:
1. A title (use # heading) — make it descriptive and professional
2. Immediately after the title, a single-line italic subtitle (*like this*) that concisely describes the report in one sentence
3. An executive summary section (## Executive Summary) — 2-3 paragraphs summarizing the key findings
4. Multiple content sections (## Section Name) — each with substantial, informative content
5. A conclusion section (## Conclusion) — summarizing the main takeaways
6. Use sub-sections (### Sub-section) where appropriate for structure
7. Use bullet lists (- item) and numbered lists (1. item) where appropriate
8. Use tables (| Col1 | Col2 |) where they add value
9. Use **bold** for key terms and *italic* for emphasis where appropriate

Guidelines:
- Write in a professional, objective tone
- Be thorough and detailed — aim for at least 1000 words
- Use real information from your training data; do NOT make up facts
- If you don't know something, say so rather than inventing
- Do NOT include any preamble like "Here is the report:" — start directly with the # title
- Do NOT wrap the output in markdown code fences — output raw markdown
"""


async def _generate_markdown(topic: str, outline: Optional[str]) -> str:
    """Call the LLM to generate a Markdown report.

    Uses the resolved chat model (or default_utility if available).
    Falls back to the chat model if default_utility isn't configured.
    """
    model = settings.resolve_model(settings.MEMORY_EXTRACTION_MODEL_ROLE)
    ollama_url = settings.OLLAMA_BASE_URL

    if not ollama_url or not model:
        raise RuntimeError("No LLM model or URL configured for report generation")

    user_content = f"Topic: {topic}"
    if outline:
        user_content += f"\n\nSuggested outline (you may adapt it):\n{outline}"
    user_content += "\n\nGenerate the full report in Markdown now."

    messages = [
        {"role": "system", "content": REPORT_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    _log("generating markdown report: topic=%r model=%s", topic[:60], model)

    async with httpx.AsyncClient(timeout=600.0) as client:
        response = await client.post(
            f"{ollama_url}/api/chat",
            json={
                "model": model,
                "messages": messages,
                "stream": False,
                "think": False,
                "options": {"num_predict": 4096},
            },
        )
        response.raise_for_status()
        data = response.json()
        content = data.get("message", {}).get("content", "").strip()

    if not content:
        raise RuntimeError("LLM returned empty report content")

    # Strip markdown code fences if the model wrapped the output
    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    _log("markdown generated: %d chars", len(content))
    return content


# ── Markdown structure parsing ───────────────────────────────────────


def _strip_md_inline(text: str) -> str:
    """Remove inline markdown markers (**, *, `, __, _) from a plain string."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"_(.+?)_", r"\1", text)
    return text.replace("`", "").strip()


def _parse_title(markdown_content: str, topic: Optional[str] = None):
    """Split the LLM markdown into (title, subtitle, body_markdown).

    - title:    the first ``# `` heading (inline markers stripped);
                falls back to the topic, then "Report".
    - subtitle: a single-line *italic* paragraph right after the title
                (what the prompt asks for); falls back to the topic when
                it differs from the title, else "".
    - body:     everything after the title/subtitle block.
    """
    lines = markdown_content.strip().split("\n")

    title: Optional[str] = None
    subtitle: Optional[str] = None
    start = 0

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            title = _strip_md_inline(stripped[2:])
            start = i + 1
            break

    if title:
        # Optional italic subtitle directly under the title.
        j = start
        while j < len(lines) and not lines[j].strip():
            j += 1
        if j < len(lines):
            candidate = lines[j].strip()
            m = re.fullmatch(r"\*(.+?)\*|_(.+?)_", candidate)
            if m:
                subtitle = (m.group(1) or m.group(2) or "").strip()
                start = j + 1

    if not title:
        title = (topic or "Report").strip() or "Report"
    if not subtitle:
        topic_clean = (topic or "").strip()
        if topic_clean and topic_clean.lower() != title.lower():
            subtitle = topic_clean

    body = "\n".join(lines[start:]).strip()
    return title, subtitle or "", body


def _format_date() -> str:
    return _date.today().strftime("%d %B %Y")


# ── Markdown → PDF (WeasyPrint) ──────────────────────────────────────

# CSS for the designed PDF output.
#
# Page architecture:
#   @page       → content pages: consistent 22/20mm margins, running
#                 header (title left, date right), "Page X of Y" footer.
#   @page cover → the full-bleed cover page (margin 0, no header/footer).
#
# NOTE: there is deliberately NO `@page :first { margin: 0 }` rule —
# that was the old bug: it stripped margins from the FIRST CONTENT page
# because no real cover page existed. Now only `.cover` (which IS the
# cover) uses the full-bleed `cover` named page.
_PDF_CSS = """
@page {
    size: A4;
    margin: 22mm 20mm 22mm 20mm;

    @top-left {
        content: "__DOC_TITLE__";
        font-family: 'Liberation Sans', Helvetica, Arial, sans-serif;
        font-size: 8pt;
        color: #8A94A3;
        vertical-align: bottom;
        padding-bottom: 4mm;
    }
    @top-right {
        content: "__DOC_DATE__";
        font-family: 'Liberation Sans', Helvetica, Arial, sans-serif;
        font-size: 8pt;
        color: #8A94A3;
        vertical-align: bottom;
        padding-bottom: 4mm;
    }
    @bottom-center {
        content: "Page " counter(page) " of " counter(pages);
        font-family: 'Liberation Sans', Helvetica, Arial, sans-serif;
        font-size: 8.5pt;
        color: #8A94A3;
        vertical-align: top;
        padding-top: 4mm;
    }
}
@page cover {
    margin: 0;
    @top-left { content: none; }
    @top-right { content: none; }
    @bottom-center { content: none; }
}

html { font-size: 11pt; }
body {
    font-family: 'Liberation Sans', Helvetica, Arial, sans-serif;
    line-height: 1.55;
    color: #2A2F36;
    margin: 0;
    padding: 0;
}

/* ── Cover page (full-bleed, by design) ─────────────────────────── */
.cover {
    page: cover;
    position: relative;
    width: 210mm;
    height: 297mm;
    background: linear-gradient(160deg, #1B3A5C 0%, #16304F 45%, #0D1E31 100%);
    color: #FFFFFF;
    page-break-after: always;
}
.cover-band {
    position: absolute;
    top: 0; left: 0; right: 0;
    height: 7mm;
    background: #C9A227;
}
.cover-ring {
    position: absolute;
    right: -28mm; bottom: -28mm;
    width: 108mm; height: 108mm;
    border: 1.1mm solid rgba(201, 162, 39, 0.28);
    border-radius: 50%;
}
.cover-ring2 {
    right: -12mm; bottom: -42mm;
    width: 84mm; height: 84mm;
    border-color: rgba(255, 255, 255, 0.09);
}
.cover-glow {
    position: absolute;
    left: -34mm; top: -34mm;
    width: 110mm; height: 110mm;
    border-radius: 50%;
    background: rgba(255, 255, 255, 0.04);
}
.cover-body {
    position: absolute;
    top: 96mm; left: 24mm; right: 24mm;
}
.cover-eyebrow {
    font-size: 10pt;
    font-weight: 700;
    letter-spacing: 3pt;
    text-transform: uppercase;
    color: #C9A227;
    margin-bottom: 10mm;
}
.cover-title {
    font-family: 'Liberation Serif', Georgia, 'Times New Roman', serif;
    font-size: 30pt;
    font-weight: 700;
    line-height: 1.22;
    margin: 0 0 9mm 0;
}
.cover-rule {
    width: 26mm;
    height: 1.3mm;
    background: #C9A227;
    margin-bottom: 9mm;
}
.cover-subtitle {
    font-size: 13pt;
    line-height: 1.5;
    color: rgba(255, 255, 255, 0.85);
    margin: 0;
}
.cover-meta {
    position: absolute;
    bottom: 22mm; left: 24mm; right: 24mm;
    border-top: 0.35mm solid rgba(255, 255, 255, 0.25);
    padding-top: 6mm;
    font-size: 9.5pt;
    color: rgba(255, 255, 255, 0.75);
}
.cover-meta-topic { font-weight: 700; color: rgba(255, 255, 255, 0.92); }
.cover-meta-row { margin: 1mm 0; }

/* ── Table of contents ──────────────────────────────────────────── */
.toc {
    page-break-after: always;
}
.toc-title {
    font-family: 'Liberation Serif', Georgia, 'Times New Roman', serif;
    font-size: 21pt;
    font-weight: 700;
    color: #16304F;
    margin: 0 0 4mm 0;
    padding-bottom: 3mm;
    border-bottom: 0.9pt solid #C9A227;
}
.toc-list {
    list-style: none;
    margin: 8mm 0 0 0;
    padding: 0;
}
.toc-list li { margin: 3.2mm 0; }
.toc-list a {
    text-decoration: none;
    color: #26313F;
}
.toc-list li.lvl2 > a {
    font-weight: 700;
    font-size: 10.5pt;
}
.toc-list li.lvl3 > a {
    color: #5C6470;
    font-size: 9.5pt;
    padding-left: 7mm;
    font-weight: 400;
}
.toc-list a::after {
    content: leader(".") " " target-counter(attr(href), page);
    color: #8A94A3;
    font-weight: 400;
}

/* ── Content sections ───────────────────────────────────────────── */
.content { counter-reset: section; }
.content h1 {
    font-family: 'Liberation Serif', Georgia, 'Times New Roman', serif;
    font-size: 19pt;
    font-weight: 700;
    color: #16304F;
    margin: 20pt 0 8pt 0;
    padding-bottom: 3pt;
    border-bottom: 0.7pt solid #DDE3EA;
    break-after: avoid;
}
.content h2 {
    font-size: 14.5pt;
    font-weight: 700;
    color: #16304F;
    margin: 20pt 0 8pt 0;
    padding-bottom: 3pt;
    border-bottom: 0.7pt solid #DDE3EA;
    counter-increment: section;
    break-after: avoid;
}
.content h2::before {
    content: counter(section, decimal-leading-zero);
    color: #C9A227;
    font-size: 12pt;
    font-weight: 700;
    margin-right: 7pt;
}
.content h3 {
    font-size: 11.5pt;
    font-weight: 700;
    color: #1B3A5C;
    margin: 14pt 0 5pt 0;
    break-after: avoid;
}
.content h3::before {
    content: "";
    display: inline-block;
    width: 5pt; height: 5pt;
    background: #C9A227;
    margin-right: 5pt;
    vertical-align: 1pt;
}
.content p {
    margin: 6pt 0;
    text-align: justify;
}

/* Executive-summary callout (wrapped by _wrap_summary) */
.summary {
    background: #F4F6F9;
    border-left: 2.5pt solid #C9A227;
    padding: 9pt 12pt 3pt 12pt;
    margin: 10pt 0 12pt 0;
    break-inside: avoid;
}
.summary > p:first-child { margin-top: 0; }
.summary > p:last-child { margin-bottom: 3pt; }

/* Lists */
ul, ol { margin: 6pt 0 8pt 0; padding-left: 18pt; }
li { margin: 2.5pt 0; }
li::marker { color: #C9A227; }

/* Tables */
table {
    width: 100%;
    border-collapse: collapse;
    margin: 11pt 0 13pt 0;
    font-size: 9.5pt;
    line-height: 1.4;
}
thead th {
    background: #16304F;
    color: #FFFFFF;
    font-weight: 700;
    text-align: left;
    padding: 5.5pt 8pt;
    border: 0.5pt solid #16304F;
}
tbody td {
    border: 0.5pt solid #D7DDE5;
    padding: 5pt 8pt;
    vertical-align: top;
}
tbody tr:nth-child(even) { background: #F4F6F9; }
thead { break-inside: avoid; break-after: avoid; }
tr { break-inside: avoid; }

/* Blockquote → callout */
blockquote {
    border-left: 2.5pt solid #C9A227;
    background: #FAF6EA;
    color: #4A5462;
    margin: 10pt 0;
    padding: 8pt 12pt;
}
blockquote p { margin: 3pt 0; }

/* Code */
code {
    font-family: 'Liberation Mono', 'Courier New', monospace;
    font-size: 9pt;
    background: #F1F3F6;
    padding: 0.5pt 2.5pt;
    border-radius: 2pt;
}
pre {
    background: #F4F6F9;
    border: 0.5pt solid #E3E8EE;
    padding: 9pt 11pt;
    margin: 10pt 0;
    font-size: 8.5pt;
    line-height: 1.45;
    white-space: pre-wrap;
    break-inside: avoid;
}
pre code { background: none; padding: 0; }

/* Misc */
hr {
    border: none;
    border-top: 0.75pt solid #DDE3EA;
    margin: 14pt 0;
}
a { color: #16304F; }
strong { color: #1B3A5C; }
img { max-width: 100%; }
"""


def _markdown_to_html(md: str) -> str:
    """Convert Markdown to HTML using the `markdown` library.

    The `toc` extension assigns stable ids to every heading (used for the
    TOC hyperlinks). `nl2br` is deliberately NOT used — proper markdown
    paragraph semantics produce cleaner justified text.
    """
    import markdown as md_lib

    return md_lib.markdown(
        md,
        extensions=["tables", "fenced_code", "toc", "sane_lists"],
        output_format="html5",
    )


_H2_H3_RE = re.compile(r"<(h2|h3)\b([^>]*)>(.*?)</\1>", re.IGNORECASE | re.DOTALL)


def _extract_toc_entries(body_html: str) -> list[tuple[str, str, str]]:
    """Collect (level, anchor_id, text) for every h2/h3 that has an id.

    Used to build the PDF table of contents.
    """
    entries: list[tuple[str, str, str]] = []
    for m in _H2_H3_RE.finditer(body_html):
        level = m.group(1).lower()
        attrs = m.group(2) or ""
        id_match = re.search(r'id="([^"]+)"', attrs, re.IGNORECASE)
        if not id_match:
            continue
        text = re.sub(r"<[^>]+>", "", m.group(3)).strip()
        if text:
            entries.append((level, id_match.group(1), text))
    return entries


_SUMMARY_H2_HINTS = ("executive summary", "summary")


def _wrap_summary(body_html: str) -> str:
    """Wrap the Executive Summary section's content in a callout div.

    Finds the first h2 whose text looks like an executive summary, then
    wraps everything between it and the next h2 in ``<div class="summary">``
    so it renders as a highlighted callout. No-op when not found.
    """
    h2_re = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.IGNORECASE | re.DOTALL)
    matches = list(h2_re.finditer(body_html))

    target = None
    for m in matches:
        inner = re.sub(r"<[^>]+>", "", m.group(1)).strip().lower()
        if any(hint in inner for hint in _SUMMARY_H2_HINTS):
            target = m
            break
    if target is None:
        return body_html

    start = target.end()
    end = len(body_html)
    for m in matches:
        if m.start() >= start:
            end = m.start()
            break

    return (
        body_html[:start]
        + '<div class="summary">'
        + body_html[start:end]
        + "</div>"
        + body_html[end:]
    )


def _build_cover_html(title: str, subtitle: str, topic: str) -> str:
    esc = _html.escape
    topic_line = (
        f'<div class="cover-meta-row cover-meta-topic">{esc(topic)}</div>'
        if topic
        else ""
    )
    subtitle_html = f'<p class="cover-subtitle">{esc(subtitle)}</p>' if subtitle else ""
    return f"""
<div class="cover">
    <div class="cover-band"></div>
    <div class="cover-glow"></div>
    <div class="cover-ring"></div>
    <div class="cover-ring cover-ring2"></div>
    <div class="cover-body">
        <div class="cover-eyebrow">Report</div>
        <h1 class="cover-title">{esc(title)}</h1>
        <div class="cover-rule"></div>
        {subtitle_html}
    </div>
    <div class="cover-meta">
        {topic_line}
        <div class="cover-meta-row">{esc(_format_date())}</div>
    </div>
</div>
"""


def _build_toc_html(entries: list[tuple[str, str, str]]) -> str:
    """Build the Table of Contents page HTML from (level, id, text) entries."""
    if not entries:
        return ""
    items = []
    for level, anchor, text in entries:
        items.append(
            f'<li class="lvl{level}"><a href="#{_html.escape(anchor, quote=True)}">'
            f"{_html.escape(text)}</a></li>"
        )
    return (
        '<div class="toc">'
        '<h1 class="toc-title">Table of Contents</h1>'
        '<ul class="toc-list">' + "".join(items) + "</ul></div>"
    )


def _css_escape(value: str) -> str:
    """Escape a string for safe embedding inside a CSS double-quoted string."""
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", " ")
        .replace("\r", " ")
    )


def _generate_pdf(
    markdown_content: str,
    output_path: Path,
    topic: Optional[str] = None,
) -> None:
    """Convert Markdown → a designed PDF via WeasyPrint.

    Document layout: full-bleed cover page → table of contents → numbered
    content sections. Only the cover is full-bleed; every content page
    uses the same @page margins (this fixes the old bug where the first
    content page had no margins while the rest did).
    """
    try:
        from weasyprint import HTML, CSS  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "WeasyPrint is not installed or missing system dependencies "
            "(pango/cairo). Install with: pip install weasyprint && "
            "apt-get install libpango-1.0-0 libpangoft2-1.0-0"
        ) from e

    title, subtitle, body_md = _parse_title(markdown_content, topic)

    body_html = _markdown_to_html(body_md)
    toc_entries = _extract_toc_entries(body_html)
    body_html = _wrap_summary(body_html)

    full_html = (
        "<!DOCTYPE html>"
        '<html lang="en"><head><meta charset="utf-8">'
        f"<title>{_html.escape(title)}</title>"
        "</head><body>"
        + _build_cover_html(title, subtitle, topic or "")
        + _build_toc_html(toc_entries)
        + f'<div class="content">{body_html}</div>'
        + "</body></html>"
    )

    header_title = _css_escape(title if len(title) <= 60 else title[:57] + "...")
    css = _PDF_CSS.replace("__DOC_TITLE__", header_title).replace(
        "__DOC_DATE__", _css_escape(_format_date())
    )

    HTML(string=full_html).write_pdf(str(output_path), stylesheets=[CSS(string=css)])


# ── Markdown → DOCX (python-docx) ────────────────────────────────────


def _oxml(tag: str):
    from docx.oxml import OxmlElement

    return OxmlElement(tag)


def _rgb(hex_color: str):
    from docx.shared import RGBColor

    return RGBColor.from_string(hex_color)


def _setup_docx_styles(doc) -> None:
    """Restyle Normal + Heading 1-3 with the report design system."""
    from docx.shared import Pt

    normal = doc.styles["Normal"]
    normal.font.name = "Calibri"
    normal.font.size = Pt(11)
    normal.font.color.rgb = _rgb(INK)
    normal.paragraph_format.space_after = Pt(8)
    normal.paragraph_format.line_spacing = 1.18

    h1 = doc.styles["Heading 1"]
    h1.font.name = "Cambria"
    h1.font.size = Pt(20)
    h1.font.bold = True
    h1.font.color.rgb = _rgb(NAVY)
    h1.paragraph_format.space_before = Pt(22)
    h1.paragraph_format.space_after = Pt(8)

    h2 = doc.styles["Heading 2"]
    h2.font.name = "Cambria"
    h2.font.size = Pt(15)
    h2.font.bold = True
    h2.font.color.rgb = _rgb(NAVY)
    h2.paragraph_format.space_before = Pt(16)
    h2.paragraph_format.space_after = Pt(6)
    _style_paragraph_border(h2, color=GOLD, size="12", position="bottom")

    h3 = doc.styles["Heading 3"]
    h3.font.name = "Cambria"
    h3.font.size = Pt(12.5)
    h3.font.bold = True
    h3.font.color.rgb = _rgb(STEEL)
    h3.paragraph_format.space_before = Pt(12)
    h3.paragraph_format.space_after = Pt(4)


def _style_paragraph_border(
    paragraph_or_style, color: str = GOLD, size: str = "12", position: str = "bottom"
) -> None:
    """Add a border to a paragraph or style (size is in eighths of a point)."""
    from docx.oxml.ns import qn

    # paragraph_format wraps an existing <w:pPr> for both paragraphs & styles
    pPr = paragraph_or_style.paragraph_format._element
    pBdr = pPr.find(qn("w:pBdr"))
    if pBdr is None:
        pBdr = _oxml("w:pBdr")
        pPr.append(pBdr)
    el = _oxml(f"w:{position}")
    el.set(qn("w:val"), "single")
    el.set(qn("w:sz"), size)
    el.set(qn("w:space"), "4")
    el.set(qn("w:color"), color)
    pBdr.append(el)


def _shade_paragraph(paragraph, fill: str) -> None:
    """Apply background shading to a paragraph."""
    from docx.oxml.ns import qn

    pPr = paragraph._p.get_or_add_pPr()
    shd = _oxml("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    pPr.append(shd)


def _shade_cell(cell, fill: str) -> None:
    from docx.oxml.ns import qn

    tcPr = cell._tc.get_or_add_tcPr()
    shd = _oxml("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), fill)
    tcPr.append(shd)


def _style_table_borders(table, color: str = "D7DDE5") -> None:
    from docx.oxml.ns import qn

    tblPr = table._tbl.tblPr
    borders = _oxml("w:tblBorders")
    for edge in ("top", "left", "bottom", "right", "insideH", "insideV"):
        el = _oxml(f"w:{edge}")
        el.set(qn("w:val"), "single")
        el.set(qn("w:sz"), "4")
        el.set(qn("w:space"), "0")
        el.set(qn("w:color"), color)
        borders.append(el)
    tblPr.append(borders)


def _add_docx_title_page(doc, title: str, subtitle: str, topic: str) -> None:
    """A clean typographic title page (no header/footer on page 1)."""
    from docx.shared import Pt

    # Eyebrow
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(140)
    p.paragraph_format.space_after = Pt(14)
    run = p.add_run("R E P O R T")
    run.font.bold = True
    run.font.size = Pt(10)
    run.font.color.rgb = _rgb(GOLD)

    # Title
    p = doc.add_paragraph()
    p.paragraph_format.space_after = Pt(10)
    run = p.add_run(title)
    run.font.name = "Cambria"
    run.font.size = Pt(28)
    run.font.bold = True
    run.font.color.rgb = _rgb(NAVY)

    # Gold rule
    rule = doc.add_paragraph()
    rule.paragraph_format.space_after = Pt(14)
    _style_paragraph_border(rule, color=GOLD, size="18")

    # Subtitle
    if subtitle:
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(6)
        run = p.add_run(subtitle)
        run.font.italic = True
        run.font.size = Pt(12.5)
        run.font.color.rgb = _rgb(MUTED)

    # Meta — topic + date, pushed toward the bottom
    p = doc.add_paragraph()
    p.paragraph_format.space_before = Pt(220)
    p.paragraph_format.space_after = Pt(2)
    if topic:
        run = p.add_run(topic)
        run.font.bold = True
        run.font.size = Pt(10.5)
        run.font.color.rgb = _rgb(STEEL)

    p = doc.add_paragraph()
    run = p.add_run(_format_date())
    run.font.size = Pt(10)
    run.font.color.rgb = _rgb(MUTED)

    doc.add_page_break()


def _add_docx_field_run(paragraph, field_type: str, dirty: bool = False) -> None:
    """Append a fldChar run (begin/separate/end) to a paragraph."""
    from docx.oxml.ns import qn

    r = paragraph.add_run()
    fld = _oxml("w:fldChar")
    fld.set(qn("w:fldCharType"), field_type)
    if dirty:
        fld.set(qn("w:dirty"), "true")
    r._r.append(fld)


def _add_docx_instr_run(paragraph, instr: str) -> None:
    """Append a field-instruction run (e.g. ' TOC \\o "1-3" ') to a paragraph."""
    from docx.oxml.ns import qn

    r = paragraph.add_run()
    instr_text = _oxml("w:instrText")
    instr_text.set(qn("xml:space"), "preserve")
    instr_text.text = instr
    r._r.append(instr_text)


def _add_docx_toc(doc, body_md: str) -> None:
    """Insert a real Word TOC field with a human-readable cached result.

    The cached content lists every h2/h3 section title (styled), so every
    viewer — including LibreOffice — shows a meaningful table of contents
    immediately. Because the field is marked dirty, Word regenerates it
    (page numbers + hyperlinks) automatically when the document opens.
    """
    from docx.shared import Pt

    heading = doc.add_paragraph()
    run = heading.add_run("Table of Contents")
    run.font.name = "Cambria"
    run.font.size = Pt(18)
    run.font.bold = True
    run.font.color.rgb = _rgb(NAVY)
    heading.paragraph_format.space_after = Pt(10)
    _style_paragraph_border(heading, color=GOLD, size="12")

    # Collect h2/h3 section titles from the body markdown.
    entries: list[tuple[int, str]] = []
    for line in body_md.split("\n"):
        stripped = line.strip()
        if stripped.startswith("### "):
            entries.append((3, _strip_md_inline(stripped[4:])))
        elif stripped.startswith("## "):
            entries.append((2, _strip_md_inline(stripped[3:])))

    if not entries:
        p = doc.add_paragraph()
        _add_docx_field_run(p, "begin", dirty=True)
        _add_docx_instr_run(p, r' TOC \o "1-3" \h \z \u ')
        _add_docx_field_run(p, "separate")
        _add_docx_field_run(p, "end")
        doc.add_page_break()
        return

    for idx, (level, text) in enumerate(entries):
        p = doc.add_paragraph()
        if idx == 0:
            # Field begins here; cached result follows the separator.
            _add_docx_field_run(p, "begin", dirty=True)
            _add_docx_instr_run(p, r' TOC \o "1-3" \h \z \u ')
            _add_docx_field_run(p, "separate")

        entry_run = p.add_run(text)
        if level == 2:
            entry_run.bold = True
            entry_run.font.size = Pt(10.5)
            entry_run.font.color.rgb = _rgb(NAVY)
            p.paragraph_format.space_before = Pt(6)
            p.paragraph_format.space_after = Pt(2)
        else:
            entry_run.font.size = Pt(10)
            entry_run.font.color.rgb = _rgb(MUTED)
            p.paragraph_format.left_indent = Pt(18)
            p.paragraph_format.space_after = Pt(2)

        if idx == len(entries) - 1:
            # Field ends after the last cached entry.
            _add_docx_field_run(p, "end")

    doc.add_page_break()


def _add_docx_footer(doc) -> None:
    """Centered 'Page X of Y' footer using PAGE / NUMPAGES fields."""
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    section = doc.sections[0]
    section.different_first_page_header_footer = True  # cover: no footer

    footer = section.footer
    footer.is_linked_to_previous = False
    p = footer.paragraphs[0] if footer.paragraphs else footer.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(0)

    def _field(paragraph, instr: str) -> None:
        from docx.oxml.ns import qn

        r1 = paragraph.add_run()
        fld1 = _oxml("w:fldChar")
        fld1.set(qn("w:fldCharType"), "begin")
        r1._r.append(fld1)

        r2 = paragraph.add_run()
        instr_text = _oxml("w:instrText")
        instr_text.set(qn("xml:space"), "preserve")
        instr_text.text = instr
        r2._r.append(instr_text)

        r3 = paragraph.add_run()
        fld3 = _oxml("w:fldChar")
        fld3.set(qn("w:fldCharType"), "end")
        r3._r.append(fld3)

    def _styled(paragraph) -> None:
        for run in paragraph.runs:
            run.font.size = Pt(9)
            run.font.color.rgb = _rgb(MUTED)

    p.add_run("Page ")
    _field(p, " PAGE ")
    p.add_run(" of ")
    _field(p, " NUMPAGES ")
    _styled(p)


def _setup_docx_page(doc) -> None:
    """A4 page with consistent 2.2/2.0cm margins."""
    from docx.shared import Cm

    section = doc.sections[0]
    section.page_width = Cm(21.0)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.2)
    section.bottom_margin = Cm(2.2)
    section.left_margin = Cm(2.0)
    section.right_margin = Cm(2.0)


def _add_formatted_text(paragraph, text: str) -> None:
    """Add text with inline **bold**, *italic* and `code` markdown."""
    from docx.shared import Pt

    pattern = re.compile(
        r"(\*\*.+?\*\*|`[^`]+`|(?<!\*)\*(?!\*).+?(?<!\*)\*(?!\*))", re.DOTALL
    )
    for part in pattern.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            run = paragraph.add_run(part[2:-2])
            run.bold = True
            run.font.color.rgb = _rgb(STEEL)
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            run = paragraph.add_run(part[1:-1])
            run.font.name = "Consolas"
            run.font.size = Pt(10)
            run.font.color.rgb = _rgb("8A4B08")
        elif (
            part.startswith("*")
            and part.endswith("*")
            and len(part) > 2
            and not part.startswith("**")
        ):
            run = paragraph.add_run(part[1:-1])
            run.italic = True
        else:
            paragraph.add_run(part)


def _flush_table(doc, rows: list[list[str]]) -> None:
    """Add a styled table: navy header row, zebra striping, hairline borders."""
    if not rows:
        return

    from docx.shared import Pt

    n_cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=n_cols)
    table.style = "Table Grid"
    _style_table_borders(table)

    for r_idx, row in enumerate(rows):
        for c_idx in range(n_cols):
            cell_text = row[c_idx] if c_idx < len(row) else ""
            cell = table.cell(r_idx, c_idx)
            # Fresh cells contain a single empty paragraph — write into it
            # directly so runs[0] is always the styled content run.
            paragraph = cell.paragraphs[0]
            paragraph.paragraph_format.space_after = Pt(2)

            if r_idx == 0:
                # Header row — navy background, bold white text
                _shade_cell(cell, NAVY)
                run = paragraph.add_run(_strip_md_inline(cell_text))
                run.bold = True
                run.font.color.rgb = _rgb("FFFFFF")
                run.font.size = Pt(10)
            else:
                if r_idx % 2 == 0:
                    _shade_cell(cell, ZEBRA)
                _add_formatted_text(paragraph, cell_text)
                for run in paragraph.runs:
                    run.font.size = Pt(10)

    # Spacer after the table
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(4)


def _add_code_block(doc, code_lines: list[str]) -> None:
    """Render a fenced code block as shaded monospace paragraphs."""
    from docx.shared import Pt

    if not code_lines:
        return
    for i, line in enumerate(code_lines):
        p = doc.add_paragraph()
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.space_before = Pt(4) if i == 0 else Pt(0)
        run = p.add_run(line if line.strip() else " ")
        run.font.name = "Consolas"
        run.font.size = Pt(9.5)
        run.font.color.rgb = _rgb("3A4753")
        _shade_paragraph(p, CODE_BG)
    spacer = doc.add_paragraph()
    spacer.paragraph_format.space_after = Pt(2)


def _add_blockquote(doc, text: str) -> None:
    """Render a blockquote as an indented, gold-accented callout."""
    from docx.shared import Pt

    p = doc.add_paragraph()
    p.paragraph_format.left_indent = Pt(18)
    p.paragraph_format.space_before = Pt(6)
    p.paragraph_format.space_after = Pt(6)
    _style_paragraph_border(p, color=GOLD, size="16", position="left")
    _shade_paragraph(p, CALLOUT_BG)
    run = p.add_run(text)
    run.italic = True
    run.font.color.rgb = _rgb("4A5462")


def _generate_docx(
    markdown_content: str,
    output_path: Path,
    topic: Optional[str] = None,
) -> None:
    """Convert Markdown → a styled DOCX by parsing headings, lists, tables,
    code blocks and paragraphs into a designed Word document."""
    from docx import Document
    from docx.shared import Pt

    title, subtitle, body_md = _parse_title(markdown_content, topic)

    doc = Document()
    _setup_docx_styles(doc)
    _setup_docx_page(doc)
    _add_docx_title_page(doc, title, subtitle, topic or "")
    _add_docx_toc(doc, body_md)

    lines = body_md.split("\n")
    i = 0
    in_table = False
    table_rows: list[list[str]] = []
    in_code = False
    code_lines: list[str] = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Fenced code blocks
        if stripped.startswith("```"):
            if in_code:
                _add_code_block(doc, code_lines)
                code_lines = []
                in_code = False
            else:
                if in_table and table_rows:
                    _flush_table(doc, table_rows)
                    table_rows = []
                    in_table = False
                in_code = True
            i += 1
            continue
        if in_code:
            code_lines.append(line)
            i += 1
            continue

        # Skip empty lines
        if not stripped:
            if in_table and table_rows:
                _flush_table(doc, table_rows)
                table_rows = []
                in_table = False
            i += 1
            continue

        # Table detection: line starts with |
        if stripped.startswith("|"):
            in_table = True
            if re.match(r"^\|[\s\-:|]+\|?$", stripped):
                i += 1
                continue
            cells = [c.strip() for c in stripped.strip("|").split("|")]
            table_rows.append(cells)
            i += 1
            continue
        elif in_table:
            _flush_table(doc, table_rows)
            table_rows = []
            in_table = False

        # Headings (strip any inline markdown from the heading text)
        if stripped.startswith("### "):
            doc.add_heading(_strip_md_inline(stripped[4:]), level=3)
        elif stripped.startswith("## "):
            doc.add_heading(_strip_md_inline(stripped[3:]), level=2)
        elif stripped.startswith("# "):
            doc.add_heading(_strip_md_inline(stripped[2:]), level=1)

        # Bullet lists
        elif stripped.startswith("- ") or stripped.startswith("* "):
            p = doc.add_paragraph(style="List Bullet")
            p.paragraph_format.space_after = Pt(3)
            _add_formatted_text(p, stripped[2:])
        # Numbered lists
        elif re.match(r"^\d+\.\s", stripped):
            text = re.sub(r"^\d+\.\s", "", stripped)
            p = doc.add_paragraph(style="List Number")
            p.paragraph_format.space_after = Pt(3)
            _add_formatted_text(p, text)
        # Blockquote
        elif stripped.startswith("> "):
            _add_blockquote(doc, stripped[2:])
        # Horizontal rule
        elif stripped in ("---", "***", "___"):
            rule = doc.add_paragraph()
            _style_paragraph_border(rule, color="DDE3EA", size="6")
        # Regular paragraph
        else:
            p = doc.add_paragraph()
            _add_formatted_text(p, stripped)

        i += 1

    # Flush any remaining blocks
    if in_code and code_lines:
        _add_code_block(doc, code_lines)
    if in_table and table_rows:
        _flush_table(doc, table_rows)

    _add_docx_footer(doc)
    doc.save(str(output_path))


# ── Public API ───────────────────────────────────────────────────────


async def generate_report(
    topic: str,
    outline: Optional[str] = None,
    format: str = "pdf",
) -> dict:
    """Generate a report and save it to disk.

    Args:
        topic: What the report is about.
        outline: Optional section outline to guide structure.
        format: "pdf" or "docx" (default "pdf").

    Returns:
        Dict with: report_id, filename, file_path, download_url, format
    """
    fmt = format.lower() if format else "pdf"
    if fmt not in ("pdf", "docx"):
        fmt = "pdf"

    report_id = str(uuid.uuid4())
    reports_dir = _get_reports_dir()

    # 1. Generate Markdown via LLM
    markdown_content = await _generate_markdown(topic, outline)

    # 2. Convert to target format (run in thread pool — these are sync/CPU-bound)
    ext = "pdf" if fmt == "pdf" else "docx"
    output_path = reports_dir / f"{report_id}.{ext}"

    if fmt == "pdf":
        await asyncio.to_thread(_generate_pdf, markdown_content, output_path, topic)
    else:
        await asyncio.to_thread(_generate_docx, markdown_content, output_path, topic)

    # Verify the file was created
    if not output_path.exists():
        raise RuntimeError(f"Report file was not created: {output_path}")

    file_size = output_path.stat().st_size
    _log("report saved: %s (%d bytes)", output_path.name, file_size)

    # Build a clean filename from the topic
    safe_topic = re.sub(r"[^\w\s-]", "", topic)[:50].strip()
    safe_topic = re.sub(r"[\s-]+", "_", safe_topic) or "report"
    filename = f"{safe_topic}.{ext}"

    # Relative path for storage (relative to data/)
    rel_path = f"reports/{report_id}.{ext}"

    return {
        "type": "report",
        "format": fmt,
        "filename": filename,
        "file_path": rel_path,
        "download_url": f"/api/reports/{report_id}/download",
        "report_id": report_id,
        "created_at": int(time.time()),
    }
