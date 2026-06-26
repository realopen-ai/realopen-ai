"""
PowerPoint presentation generation service — LLM generates slide-structured
Markdown, then we parse it into slides and build a .pptx using one of the
bundled templates.
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
from pptx.util import Inches, Pt

from app.config import settings

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[pptx_gen] {formatted}", flush=True)


def _get_reports_dir() -> Path:
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


def _get_templates_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "templates" / "pptx"


AVAILABLE_TEMPLATES = ["corporate", "modern", "elegant"]
DEFAULT_TEMPLATE = "corporate"


async def _get_available_templates_from_db() -> list[str]:
    """Query the DB for all template slugs. Falls back to hardcoded list if DB unavailable."""
    try:
        from app.db.session import async_session_factory
        from app.db.models import Template
        from sqlalchemy import select

        async with async_session_factory() as db:
            result = await db.execute(select(Template.slug))
            slugs = [r[0] for r in result.all()]
            if slugs:
                return slugs
    except Exception as e:
        _log("failed to query templates from DB, using hardcoded: %s", e)
    return AVAILABLE_TEMPLATES


async def _get_templates_with_descriptions() -> list[tuple[str, str]]:
    """Query the DB for all templates as (slug, description) tuples.

    Falls back to hardcoded list if DB unavailable. Used by the tool's
    get_dynamic_description() so the LLM can see template names and
    their descriptions.
    """
    try:
        from app.db.session import async_session_factory
        from app.db.models import Template
        from sqlalchemy import select

        async with async_session_factory() as db:
            result = await db.execute(select(Template.slug, Template.description))
            rows = result.all()
            if rows:
                return [(r[0], r[1] or "") for r in rows]
    except Exception as e:
        _log("failed to query templates with descriptions from DB: %s", e)
    return [(slug, "") for slug in AVAILABLE_TEMPLATES]


def _resolve_template_path(template_name: Optional[str]) -> Path:
    """Resolve a template name to its .pptx file path.

    First checks the DB for the template slug, then falls back to the
    filesystem. If the named template doesn't exist, falls back to the
    default template.
    """
    templates_dir = _get_templates_dir()
    name = (template_name or DEFAULT_TEMPLATE).lower().strip()
    tpl_path = templates_dir / f"{name}.pptx"
    if name not in AVAILABLE_TEMPLATES:
        tpl_path = templates_dir / "custom" / f"{name}.pptx"
    if not tpl_path.exists():
        _log("template '%s' not found, falling back to '%s'", name, DEFAULT_TEMPLATE)
        tpl_path = templates_dir / f"{DEFAULT_TEMPLATE}.pptx"
    return tpl_path


# ── Theme color extraction from template ─────────────────────────────


def _extract_theme_colors(template_path: Path) -> dict:
    """Extract color values from the template by inspecting its layout shapes.

    Returns a dict with keys: title_color, body_color, accent_color.
    Falls back to sensible defaults if extraction fails.
    """
    from pptx import Presentation
    from pptx.dml.color import RGBColor

    defaults = {
        "title_color": RGBColor(0xFF, 0xFF, 0xFF),  # White
        "body_color": RGBColor(0x33, 0x33, 0x33),  # Dark gray
        "accent_color": RGBColor(0x3D, 0x8B, 0xFD),  # Blue
        "title_bg_color": RGBColor(0x0F, 0x2A, 0x4A),  # Navy
    }

    try:
        prs = Presentation(str(template_path))
        # Look at the title slide layout (layout 0) for the title color
        title_layout = prs.slide_layouts[0]
        for ph in title_layout.placeholders:
            if ph.placeholder_format.idx == 0:
                p = ph.text_frame.paragraphs[0]
                if p.font.color and p.font.color.type:
                    defaults["title_color"] = p.font.color.rgb
                break

        # Look at content layout (layout 1) for body color and title bar color
        content_layout = prs.slide_layouts[1]
        for ph in content_layout.placeholders:
            if ph.placeholder_format.idx == 0:
                p = ph.text_frame.paragraphs[0]
                if p.font.color and p.font.color.type:
                    defaults["title_color"] = p.font.color.rgb
            elif ph.placeholder_format.idx == 1:
                p = ph.text_frame.paragraphs[0]
                if p.font.color and p.font.color.type:
                    defaults["body_color"] = p.font.color.rgb

        # Try to get the title bar background color from layout shapes
        for shape in content_layout.shapes:
            if not shape.is_placeholder and hasattr(shape, "fill"):
                try:
                    if shape.fill.type is not None:
                        fill_color = shape.fill.fore_color.rgb
                        # The title bar is the widest rectangle near the top
                        if shape.top < Inches(1) and shape.width > Inches(10):
                            defaults["title_bg_color"] = fill_color
                        # The accent line is thin
                        elif shape.height < Inches(0.2) and shape.width > Inches(10):
                            defaults["accent_color"] = fill_color
                except Exception:
                    pass
    except Exception as e:
        _log("theme extraction failed, using defaults: %s", e)

    return defaults


# ── LLM presentation generation prompt ───────────────────────────────

PPTX_SYSTEM_PROMPT = """You are a professional presentation designer. Create a concise, visually-oriented slide deck in a simple Markdown-like format.

FORMAT RULES:
- Each slide starts with a # heading (the slide title)
- Use "---" on its own line to separate slides
- Use "- " for bullet points (max 6 per slide)
- Keep each bullet under 12 words — be concise!
- Use ">" at the start of a line for speaker notes (detailed talking points the presenter will say)
- Use "## " for section divider slides (just the title, no bullets)
- Start with a title slide (the presentation title)

- End with a "Key Takeaways" or "Thank You" slide

CONTENT GUIDELINES:
- Aim for 8-15 slides total
- One main idea per slide
- Every content slide MUST have at least 2 bullet points — never leave a content slide empty
- Use real information from your training data; do NOT make up facts
- If you don't know something, say so rather than inventing
- Speaker notes should be 3-5 sentences of detail that expands on the slide bullets
- Keep slide titles SHORT (under 50 characters) — they must fit on one line

EXAMPLE:
# Introduction to Machine Learning
> Welcome everyone. Today we'll explore the fundamentals of machine learning.

---
# What is Machine Learning?
- A subset of artificial intelligence
- Enables systems to learn from data
- Improves performance without explicit programming
- Powers recommendations, predictions, and automation
> Machine learning is fundamentally about pattern recognition in data.

---
## Core Concepts
> Now let's dive into the three main types of ML.

---
# Types of Machine Learning
- Supervised learning: labeled training data
- Unsupervised learning: find hidden patterns
- Reinforcement learning: learn through feedback
> Each type has different use cases in business.

---
# Key Takeaways
- ML is a powerful subset of AI
- Three main types: supervised, unsupervised, reinforcement
- Drives real-world applications across industries
> Thank you for your attention. Questions?

Do NOT include any preamble. Do NOT wrap in code fences. Start directly with the # title.
"""


async def _generate_slide_markdown(topic: str, outline: Optional[str]) -> str:
    model = settings.resolve_model(settings.MEMORY_EXTRACTION_MODEL_ROLE)
    ollama_url = settings.OLLAMA_BASE_URL

    if not ollama_url or not model:
        raise RuntimeError("No LLM model or URL configured for presentation generation")

    user_content = f"Topic: {topic}"
    if outline:
        user_content += f"\n\nSuggested outline (you may adapt it):\n{outline}"
    user_content += "\n\nGenerate the slide deck now."

    messages = [
        {"role": "system", "content": PPTX_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    _log("generating slide markdown: topic=%r model=%s", topic[:60], model)

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
        raise RuntimeError("LLM returned empty presentation content")

    if content.startswith("```"):
        content = content.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    _log("slide markdown generated: %d chars", len(content))
    return content


# ── Slide parsing ────────────────────────────────────────────────────


class Slide:
    def __init__(self):
        self.title: str = ""
        self.is_section: bool = False
        self.bullets: list[str] = []
        self.notes: str = ""


def _parse_slides(markdown_content: str) -> list[Slide]:
    raw_slides = re.split(r"^\s*---\s*$", markdown_content, flags=re.MULTILINE)

    slides = []
    for raw in raw_slides:
        raw = raw.strip()
        if not raw:
            continue

        slide = Slide()
        lines = raw.split("\n")

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue

            if stripped.startswith("## "):
                slide.title = stripped[3:].strip()
                slide.is_section = True
            elif stripped.startswith("# "):
                slide.title = stripped[2:].strip()
            elif stripped.startswith("> "):
                slide.notes += (slide.notes and "\n") + stripped[2:].strip()
            elif stripped.startswith(">"):
                slide.notes += (slide.notes and "\n") + stripped[1:].strip()
            elif stripped.startswith("- ") or stripped.startswith("* "):
                bullet = stripped[2:].strip()
                bullet = bullet.replace("**", "").replace("*", "")
                if bullet:
                    slide.bullets.append(bullet)
            elif re.match(r"^\d+\.\s", stripped):
                bullet = re.sub(r"^\d+\.\s", "", stripped).strip()
                bullet = bullet.replace("**", "").replace("*", "")
                if bullet:
                    slide.bullets.append(bullet)
            else:
                if not slide.title:
                    slide.title = stripped
                else:
                    text = stripped.replace("**", "").replace("*", "")
                    if text:
                        slide.bullets.append(text)

        # FIX: If a content slide has no bullets but has notes, extract
        # a summary bullet from the notes so the slide isn't empty.
        if not slide.is_section and not slide.bullets and slide.notes:
            # Take the first sentence of the notes as a bullet
            first_sentence = slide.notes.split(".")[0].strip()
            if first_sentence:
                slide.bullets.append(first_sentence + ".")

        if slide.title or slide.bullets:
            slides.append(slide)

    return slides


# ── PPTX building ────────────────────────────────────────────────────


def _build_pptx(slides: list[Slide], template_path: Path, output_path: Path) -> None:
    """Build a .pptx from parsed slides using a template.

    FIXES:
    - Explicitly sets font properties on every run (layout styling isn't
      inherited by slide placeholders in python-pptx).
    - Enables word_wrap on all text frames.
    - Section divider slides get a properly positioned, wrapped, styled
      text box (not using the blank layout's non-existent placeholders).
    - Title placeholders get auto-size to fit text within the title bar.
    """
    from pptx import Presentation
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR, MSO_AUTO_SIZE

    prs = Presentation(str(template_path))
    theme = _extract_theme_colors(template_path)

    LAYOUT_TITLE = 0
    LAYOUT_CONTENT = 1
    LAYOUT_BLANK = 6

    for i, slide_data in enumerate(slides):
        # Choose layout
        if i == 0:
            layout_idx = LAYOUT_TITLE
        elif slide_data.is_section:
            layout_idx = LAYOUT_BLANK
        else:
            layout_idx = LAYOUT_CONTENT

        if layout_idx >= len(prs.slide_layouts):
            layout_idx = 1 if len(prs.slide_layouts) > 1 else 0

        layout = prs.slide_layouts[layout_idx]
        slide = prs.slides.add_slide(layout)

        # ── Set title ──
        if slide_data.title:
            if layout_idx == LAYOUT_BLANK:
                # Section divider: add a properly positioned text box
                # on the dark background, styled with the accent color
                left = Inches(0.8)
                top = Inches(2.5)
                width = Inches(11.5)
                height = Inches(2.0)
                txBox = slide.shapes.add_textbox(left, top, width, height)
                tf = txBox.text_frame
                tf.word_wrap = True
                tf.auto_size = MSO_AUTO_SIZE.TEXT_TO_FIT_SHAPE
                tf.vertical_anchor = MSO_ANCHOR.MIDDLE
                p = tf.paragraphs[0]
                p.text = slide_data.title
                p.font.size = Pt(40)
                p.font.bold = True
                p.font.color.rgb = theme["title_color"]  # Gold/white on dark
                p.font.name = "Calibri"
                p.alignment = PP_ALIGN.LEFT
            else:
                # Title or content slide: use the title placeholder
                title_ph = None
                for ph in slide.placeholders:
                    if ph.placeholder_format.idx == 0:
                        title_ph = ph
                        break

                if title_ph:
                    title_ph.text = slide_data.title
                    tf = title_ph.text_frame
                    tf.word_wrap = True
                    # FIX: Explicitly set font on the run (layout styling
                    # isn't inherited by slide placeholders)
                    for para in tf.paragraphs:
                        para.alignment = PP_ALIGN.LEFT
                        for run in para.runs:
                            run.font.size = Pt(44 if i == 0 else 28)
                            run.font.bold = True
                            run.font.color.rgb = theme["title_color"]
                            run.font.name = "Calibri"
                else:
                    # Fallback: add a text box
                    left = Inches(0.5)
                    top = Inches(0.15)
                    width = Inches(11.5)
                    height = Inches(0.9)
                    txBox = slide.shapes.add_textbox(left, top, width, height)
                    tf = txBox.text_frame
                    tf.word_wrap = True
                    p = tf.paragraphs[0]
                    p.text = slide_data.title
                    p.font.size = Pt(28)
                    p.font.bold = True
                    p.font.color.rgb = theme["title_color"]
                    p.font.name = "Calibri"

        # ── Set subtitle on title slide ──
        if i == 0 and not slide_data.bullets:
            for ph in slide.placeholders:
                if ph.placeholder_format.idx == 1:
                    if slide_data.notes:
                        ph.text = slide_data.notes
                        tf = ph.text_frame
                        tf.word_wrap = True
                        for para in tf.paragraphs:
                            for run in para.runs:
                                run.font.size = Pt(20)
                                run.font.color.rgb = theme.get(
                                    "subtitle", RGBColor(0x88, 0x99, 0xBB)
                                )
                                run.font.name = "Calibri"
                    break

        # ── Set bullets on content slides ──
        if slide_data.bullets and not slide_data.is_section:
            content_ph = None
            for ph in slide.placeholders:
                if ph.placeholder_format.idx == 1:
                    content_ph = ph
                    break

            if content_ph:
                tf = content_ph.text_frame
                tf.word_wrap = True
                tf.clear()
                for j, bullet in enumerate(slide_data.bullets):
                    if j == 0:
                        p = tf.paragraphs[0]
                    else:
                        p = tf.add_paragraph()
                    p.text = bullet
                    p.level = 0
                    # FIX: Explicitly set font on every run
                    for run in p.runs:
                        run.font.size = Pt(18)
                        run.font.color.rgb = theme["body_color"]
                        run.font.name = "Calibri"
                    p.space_after = Pt(12)
            else:
                # Fallback: add a text box with bullets
                left = Inches(0.8)
                top = Inches(1.5)
                width = Inches(11.5)
                height = Inches(5.5)
                txBox = slide.shapes.add_textbox(left, top, width, height)
                tf = txBox.text_frame
                tf.word_wrap = True
                for j, bullet in enumerate(slide_data.bullets):
                    if j == 0:
                        p = tf.paragraphs[0]
                    else:
                        p = tf.add_paragraph()
                    p.text = f"• {bullet}"
                    p.font.size = Pt(18)
                    p.font.color.rgb = theme["body_color"]
                    p.font.name = "Calibri"
                    p.space_after = Pt(12)

        # ── Set speaker notes ──
        if slide_data.notes:
            notes_slide = slide.notes_slide
            notes_tf = notes_slide.notes_text_frame
            notes_tf.text = slide_data.notes
            # Style the notes text
            for para in notes_tf.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(14)
                    run.font.name = "Calibri"

    prs.save(str(output_path))


# ── Public API ───────────────────────────────────────────────────────


async def generate_presentation(
    topic: str,
    outline: Optional[str] = None,
    template: Optional[str] = None,
) -> dict:
    report_id = str(uuid.uuid4())
    reports_dir = _get_reports_dir()
    template_path = _resolve_template_path(template)

    markdown_content = await _generate_slide_markdown(topic, outline)
    slides = _parse_slides(markdown_content)
    if not slides:
        raise RuntimeError("No slides parsed from LLM output")
    _log("parsed %d slides from markdown", len(slides))

    output_path = reports_dir / f"{report_id}.pptx"
    await asyncio.to_thread(_build_pptx, slides, template_path, output_path)

    if not output_path.exists():
        raise RuntimeError(f"PPTX file was not created: {output_path}")

    file_size = output_path.stat().st_size
    _log(
        "presentation saved: %s (%d bytes, %d slides)",
        output_path.name,
        file_size,
        len(slides),
    )

    safe_topic = re.sub(r"[^\w\s-]", "", topic)[:50].strip()
    safe_topic = re.sub(r"[\s-]+", "_", safe_topic) or "presentation"
    filename = f"{safe_topic}.pptx"

    rel_path = f"reports/{report_id}.pptx"

    return {
        "type": "presentation",
        "format": "pptx",
        "filename": filename,
        "file_path": rel_path,
        "download_url": f"/api/reports/{report_id}/download",
        "report_id": report_id,
        "created_at": int(time.time()),
        "slide_count": len(slides),
        "template": template_path.stem,
    }
