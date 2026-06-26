"""
Report generation service — LLM generates Markdown, then converts to
PDF (via WeasyPrint) or DOCX (via python-docx).

Pipeline:
  1. Call the chat model with a report-generation prompt (topic + outline)
     → produces a well-structured Markdown document.
  2. Convert Markdown → target format:
     - PDF: Markdown → HTML (via `markdown` lib) → PDF (via WeasyPrint
       with CSS for title page, TOC, headers/footers, page numbers)
     - DOCX: Markdown → DOCX (via python-docx, parsing headings/lists/
       tables/paragraphs)
  3. Save to data/reports/{report_id}.{ext}

Both paths consume the same Markdown, so the LLM generation prompt is
identical regardless of output format.

WeasyPrint requires system libs (pango, cairo) — see the backend
Dockerfile for the apt-get install. If WeasyPrint import fails at
runtime (e.g. missing libs), PDF generation degrades gracefully: the
tool returns an error instead of crashing the backend.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
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


# ── LLM report generation prompt ─────────────────────────────────────

REPORT_SYSTEM_PROMPT = """You are a professional report writer. Generate a comprehensive, well-structured report in Markdown format about the given topic.

The report MUST include:
1. A title (use # heading) — make it descriptive and professional
2. An executive summary section (## Executive Summary) — 2-3 paragraphs summarizing the key findings
3. Multiple content sections (## Section Name) — each with substantial, informative content
4. A conclusion section (## Conclusion) — summarizing the main takeaways
5. Use sub-sections (### Sub-section) where appropriate for structure
6. Use bullet lists (- item) and numbered lists (1. item) where appropriate
7. Use tables (| Col1 | Col2 |) where they add value
8. Use **bold** for key terms and *italic* for emphasis where appropriate

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


# ── Markdown → PDF (WeasyPrint) ──────────────────────────────────────


# CSS for professional PDF output: title page, page numbers, TOC, etc.
_PDF_CSS = """
@page {
    size: A4;
    margin: 2.5cm;
    @bottom-center {
        content: counter(page);
        font-size: 10px;
        color: #666;
    }
    @top-right {
        content: "Report";
        font-size: 9px;
        color: #999;
    }
}
@page :first {
    margin: 0;
    @bottom-center { content: ""; }
    @top-right { content: ""; }
}
body {
    font-family: 'Helvetica', 'Arial', sans-serif;
    font-size: 11pt;
    line-height: 1.6;
    color: #222;
}
h1 {
    font-size: 22pt;
    color: #1a1a2e;
    border-bottom: 2px solid #16213e;
    padding-bottom: 8px;
    margin-top: 0;
}
h2 {
    font-size: 16pt;
    color: #16213e;
    margin-top: 24px;
    border-bottom: 1px solid #ccc;
    padding-bottom: 4px;
}
h3 {
    font-size: 13pt;
    color: #0f3460;
    margin-top: 18px;
}
p { margin: 8px 0; }
ul, ol { margin: 8px 0; padding-left: 24px; }
li { margin: 4px 0; }
table {
    width: 100%;
    border-collapse: collapse;
    margin: 12px 0;
    font-size: 10pt;
}
th, td {
    border: 1px solid #ddd;
    padding: 6px 10px;
    text-align: left;
}
th { background-color: #f0f0f5; font-weight: bold; }
tr:nth-child(even) { background-color: #fafafa; }
blockquote {
    border-left: 3px solid #16213e;
    margin: 12px 0;
    padding: 8px 16px;
    color: #555;
    background: #f9f9f9;
}
code {
    background: #f0f0f0;
    padding: 2px 5px;
    border-radius: 3px;
    font-size: 10pt;
    font-family: 'Courier New', monospace;
}
pre {
    background: #f5f5f5;
    padding: 12px;
    border-radius: 5px;
    overflow-x: auto;
    font-size: 9pt;
}
pre code { background: none; padding: 0; }
img { max-width: 100%; }
"""


def _markdown_to_html(md: str) -> str:
    """Convert Markdown to HTML using the `markdown` library."""
    import markdown as md_lib

    html = md_lib.markdown(
        md,
        extensions=["tables", "fenced_code", "toc", "nl2br"],
        output_format="html5",
    )
    return html


def _generate_pdf(markdown_content: str, output_path: Path) -> None:
    """Convert Markdown → HTML → PDF via WeasyPrint."""
    try:
        from weasyprint import HTML, CSS  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "WeasyPrint is not installed or missing system dependencies "
            "(pango/cairo). Install with: pip install weasyprint && "
            "apt-get install libpango-1.0-0 libpangoft2-1.0-0"
        ) from e

    html_body = _markdown_to_html(markdown_content)
    full_html = f"<!DOCTYPE html><html><head><meta charset='utf-8'></head><body>{html_body}</body></html>"

    HTML(string=full_html).write_pdf(
        str(output_path), stylesheets=[CSS(string=_PDF_CSS)]
    )


# ── Markdown → DOCX (python-docx) ────────────────────────────────────


def _generate_docx(markdown_content: str, output_path: Path) -> None:
    """Convert Markdown → DOCX by parsing headings, lists, tables, paragraphs."""
    from docx import Document
    from docx.shared import Pt

    doc = Document()

    # Set default font
    style = doc.styles["Normal"]
    font = style.font
    font.name = "Calibri"
    font.size = Pt(11)

    lines = markdown_content.split("\n")
    i = 0
    in_table = False
    table_rows = []

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

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
            # Skip separator rows like |---|---|
            if re.match(r"^\|[\s\-:]+\|", stripped):
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

        # Headings
        if stripped.startswith("### "):
            doc.add_heading(stripped[4:], level=3)
        elif stripped.startswith("## "):
            doc.add_heading(stripped[3:], level=2)
        elif stripped.startswith("# "):
            doc.add_heading(stripped[2:], level=1)
        # Bullet lists
        elif stripped.startswith("- ") or stripped.startswith("* "):
            doc.add_paragraph(stripped[2:], style="List Bullet")
        elif re.match(r"^\d+\.\s", stripped):
            text = re.sub(r"^\d+\.\s", "", stripped)
            doc.add_paragraph(text, style="List Number")
        # Blockquote
        elif stripped.startswith("> "):
            p = doc.add_paragraph(stripped[2:])
            p.style = (
                "Intense Quote"
                if "Intense Quote" in [s.name for s in doc.styles]
                else p.style
            )
        # Horizontal rule
        elif stripped in ("---", "***", "___"):
            doc.add_paragraph("─────────────────────────")
        else:
            # Regular paragraph — handle inline bold/italic
            p = doc.add_paragraph()
            _add_formatted_text(p, stripped)

        i += 1

    # Flush any remaining table
    if in_table and table_rows:
        _flush_table(doc, table_rows)

    doc.save(str(output_path))


def _flush_table(doc, rows: list[list[str]]):
    """Add a table to the docx from parsed rows."""
    if not rows:
        return

    n_cols = max(len(r) for r in rows)
    table = doc.add_table(rows=len(rows), cols=n_cols, style="Table Grid")
    for r_idx, row in enumerate(rows):
        for c_idx, cell_text in enumerate(row):
            if c_idx < n_cols:
                cell = table.cell(r_idx, c_idx)
                cell.text = cell_text
                # Bold the header row
                if r_idx == 0:
                    for paragraph in cell.paragraphs:
                        for run in paragraph.runs:
                            run.bold = True


def _add_formatted_text(paragraph, text: str):
    """Add text with inline **bold** and *italic* markdown formatting."""
    # Simple approach: split on ** for bold, then * for italic
    parts = text.split("**")
    for i, part in enumerate(parts):
        if i % 2 == 1:
            # Bold
            run = paragraph.add_run(part)
            run.bold = True
        else:
            # Check for italic within this part
            italic_parts = part.split("*")
            for j, ipart in enumerate(italic_parts):
                if j % 2 == 1:
                    run = paragraph.add_run(ipart)
                    run.italic = True
                else:
                    if ipart:
                        paragraph.add_run(ipart)


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
        await asyncio.to_thread(_generate_pdf, markdown_content, output_path)
    else:
        await asyncio.to_thread(_generate_docx, markdown_content, output_path)

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
