"""
PowerPoint presentation generation service — LLM generates slide-structured
Markdown, then we build a .pptx using a template.

Two generation strategies:
  1. SLIDE DUPLICATION (preferred): If the template has pre-existing slides
     (like SlidesCarnival templates), we deep-copy them — preserving all
     images, shapes, and decorative elements — and replace only the text.
     This gives natural layout variety and keeps background images.
  2. LAYOUT-BASED (fallback): If the template has no slides (pure layout
     templates like our corporate/modern/elegant), we use add_slide(layout)
     and set text on placeholders. Dynamic layout scanning finds the best
     layouts across all available ones.

Content-to-layout matching: when duplicating, we categorize template slides
by their visual role (title, content-1-column, content-2-column, section,
image-heavy) and match each generated slide to the best-fitting template
slide. Content slides rotate through variants for visual variety.

Asset replacement: images in duplicated slides named "REPLACEABLE_*" (or
with alt-text starting with "REPLACEABLE_") are tagged for future
replacement by user-uploaded assets or Google-fetched images.
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
        "title_color": RGBColor(0xFF, 0xFF, 0xFF),
        "body_color": RGBColor(0x33, 0x33, 0x33),
        "accent_color": RGBColor(0x3D, 0x8B, 0xFD),
        "title_bg_color": RGBColor(0x0F, 0x2A, 0x4A),
    }

    try:
        prs = Presentation(str(template_path))

        # Try extracting from existing slides first (for slide-duplication templates)
        if len(prs.slides) > 0:
            for slide in prs.slides:
                for shape in slide.shapes:
                    if shape.is_placeholder and shape.placeholder_format.idx == 0:
                        p = shape.text_frame.paragraphs[0]
                        if p.font.color and p.font.color.type:
                            defaults["title_color"] = p.font.color.rgb
                        break
                break  # Only check first slide

        # Then check layouts (for layout-based templates)
        for layout in prs.slide_layouts:
            for ph in layout.placeholders:
                if ph.placeholder_format.idx == 0:
                    p = ph.text_frame.paragraphs[0]
                    if p.font.color and p.font.color.type:
                        defaults["title_color"] = p.font.color.rgb
                elif ph.placeholder_format.idx == 1:
                    p = ph.text_frame.paragraphs[0]
                    if p.font.color and p.font.color.type:
                        defaults["body_color"] = p.font.color.rgb
    except Exception as e:
        _log("theme extraction failed, using defaults: %s", e)

    return defaults


# ── LLM prompt ───────────────────────────────────────────────────────

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


# ── Slide duplication (deep copy with relationships) ─────────────────


def _duplicate_slide(prs, source_slide):
    """Deep-copy a slide including all its shapes, images, and relationships.

    This is the core of the slide-duplication strategy. It:
    1. Creates a new blank slide
    2. Copies all shapes from the source slide (via XML deep-copy)
    3. Re-creates image relationships so the new slide references the
       same image parts — with proper rId remapping to avoid dangling refs
    4. Copies the slide's background (with rId remapping)
    5. Returns the new slide

    Key correctness fixes (vs. naive deepcopy):
    - Skips the notesSlide relationship to prevent orphaned source slides
    - Builds an old_rId → new_rId mapping and rewrites all r:embed/r:link
      attributes in the deepcopied XML, so shape references match the new
      slide's relationship IDs
    - Same rId remapping for background elements

    The new slide inherits all decorative elements (background images,
    shapes, colors) from the source — this is what makes it work with
    templates like SlidesCarnival where the visual design lives on the
    slide, not the layout.
    """
    from pptx.oxml.ns import qn
    from copy import deepcopy

    # Relationship type for notes slides — we skip this to prevent the
    # new slide from pointing at the source slide's notes (which would
    # keep the source slide reachable via notesSlide→slide back-ref,
    # creating an orphaned part that triggers PowerPoint repair).
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT

    SKIP_RELTYPES = {RT.NOTES_SLIDE}

    # Use the source slide's layout
    source_layout = source_slide.slide_layout
    new_slide = prs.slides.add_slide(source_layout)

    # Remove default placeholders that add_slide creates
    for shape in list(new_slide.shapes):
        sp = shape._element
        sp.getparent().remove(sp)

    # ── Step 1: Copy relationships and build rId mapping ──
    # We need an old_rId → new_rId mapping because get_or_add() assigns
    # new sequential rIds, but deepcopy preserves the source rId values
    # in the XML. Without remapping, r:embed="rId4" might point to a
    # non-existent relationship if the source had gaps in its rId sequence.
    rid_map: dict[str, str] = {}

    for rel in source_slide.part.rels.values():
        # Skip notesSlide — prevents orphaned source slides
        if rel.reltype in SKIP_RELTYPES:
            continue

        if rel.is_external:
            new_rId = new_slide.part.rels.get_or_add_ext_rel(
                rel.reltype, rel.target_ref
            )
        else:
            new_rId = new_slide.part.rels.get_or_add(rel.reltype, rel.target_part)

        rid_map[rel.rId] = new_rId

    _log("rId mapping for duplicated slide: %s", rid_map)

    # ── Step 2: Rewrite r:embed and r:link attributes in deepcopied shapes ──
    # Namespace for relationship references
    R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    EMBED_ATTR = f"{{{R_NS}}}embed"
    LINK_ATTR = f"{{{R_NS}}}link"

    def _rewrite_rids(element):
        """Recursively rewrite r:embed and r:link attributes in an XML element."""
        # Check this element's attributes
        for attr_name in (EMBED_ATTR, LINK_ATTR):
            if attr_name in element.attrib:
                old_rid = element.attrib[attr_name]
                if old_rid in rid_map:
                    element.attrib[attr_name] = rid_map[old_rid]
                else:
                    _log("WARNING: rId %s not found in mapping (dangling ref)", old_rid)
        # Recurse into children
        for child in element:
            _rewrite_rids(child)

    # ── Step 3: Copy all shapes from source to new slide (with rId rewriting) ──
    for shape in source_slide.shapes:
        el = shape._element
        new_el = deepcopy(el)
        _rewrite_rids(new_el)
        new_slide.shapes._spTree.append(new_el)

    # ── Step 4: Copy the slide's background (with rId rewriting) ──
    source_cSld = source_slide._element.find(qn("p:cSld"))
    if source_cSld is not None:
        source_bg = source_cSld.find(qn("p:bg"))
        if source_bg is not None:
            new_cSld = new_slide._element.find(qn("p:cSld"))
            if new_cSld is not None:
                existing_bg = new_cSld.find(qn("p:bg"))
                if existing_bg is not None:
                    new_cSld.remove(existing_bg)
                new_bg = deepcopy(source_bg)
                _rewrite_rids(new_bg)
                # Insert bg as first child of cSld
                new_cSld.insert(0, new_bg)

    return new_slide


# ── Template slide categorization ────────────────────────────────────


class TemplateSlideInfo:
    """Metadata about a template slide for matching."""

    def __init__(self, index: int):
        self.index = index
        self.role: str = "content"  # "title", "content", "section", "closing"
        self.num_bullets: int = 0
        self.has_title_ph: bool = False
        self.has_content_ph: bool = False
        self.has_images: bool = False
        self.num_text_shapes: int = 0
        self.is_first: bool = False
        self.is_last: bool = False


def _categorize_template_slides(prs) -> list[TemplateSlideInfo]:
    """Categorize each slide in the template by its visual role.

    Roles:
    - "title": First slide, typically has a title + subtitle
    - "content": Has title + content placeholders, bullet-like text
    - "section": Mostly visual, minimal text (section divider)
    - "closing": Last slide (thank you, key takeaways)
    """
    infos = []
    total = len(prs.slides)

    for i, slide in enumerate(prs.slides):
        info = TemplateSlideInfo(i)
        info.is_first = i == 0
        info.is_last = i == total - 1

        # Check placeholders
        for ph in slide.placeholders:
            if ph.placeholder_format.idx == 0:
                info.has_title_ph = True
            if ph.placeholder_format.idx == 1:
                info.has_content_ph = True

        # Count shapes
        for shape in slide.shapes:
            if shape.is_placeholder:
                continue
            if shape.shape_type == 13:  # PICTURE
                info.has_images = True
            if shape.has_text_frame:
                info.num_text_shapes += 1

        # Count bullet-like text in content placeholder
        if info.has_content_ph:
            for ph in slide.placeholders:
                if ph.placeholder_format.idx == 1 and ph.has_text_frame:
                    text = ph.text_frame.text
                    info.num_bullets = text.count("\n") + 1 if text.strip() else 0

        # Determine role
        if info.is_first:
            info.role = "title"
        elif info.is_last:
            info.role = "closing"
        elif not info.has_content_ph and (info.has_images or info.num_text_shapes <= 1):
            info.role = "section"
        else:
            info.role = "content"

        infos.append(info)

    return infos


def _find_best_template_slide(
    slide_data: Slide,
    slide_index: int,
    template_infos: list[TemplateSlideInfo],
    used_indices: set,
) -> Optional[TemplateSlideInfo]:
    """Find the best matching template slide for a generated slide.

    Matching logic:
    - First generated slide → title template slide
    - Section dividers (is_section=True) → section template slides
    - Last generated slide → closing template slide
    - Content slides → content template slides, rotated for variety,
      with preference for slides that have a similar number of bullet
      placeholders
    """
    # total_generated = len(template_infos)
    is_first = slide_index == 0
    is_last = slide_index == -1  # caller should set this; we use -1 as sentinel

    # Title slide
    if is_first:
        for info in template_infos:
            if info.role == "title" and info.index not in used_indices:
                return info
        # Fallback: first available
        for info in template_infos:
            if info.index not in used_indices:
                return info

    # Section divider
    if slide_data.is_section:
        for info in template_infos:
            if info.role == "section" and info.index not in used_indices:
                return info
        # Fallback: any content slide
        for info in template_infos:
            if info.role == "content" and info.index not in used_indices:
                return info

    # Closing slide
    if is_last:
        for info in template_infos:
            if info.role == "closing" and info.index not in used_indices:
                return info

    # Content slide — match by bullet count, rotate for variety
    content_candidates = [
        info
        for info in template_infos
        if info.role == "content" and info.index not in used_indices
    ]
    if not content_candidates:
        # All used — reset and reuse (rotation)
        content_candidates = [info for info in template_infos if info.role == "content"]

    if not content_candidates:
        # No content slides — use any available
        for info in template_infos:
            if info.index not in used_indices:
                return info
        return template_infos[0] if template_infos else None

    # Match: prefer slides whose bullet count is closest to ours
    num_bullets = len(slide_data.bullets)
    best = min(content_candidates, key=lambda info: abs(info.num_bullets - num_bullets))
    return best


# ── Text replacement on duplicated slides ────────────────────────────


def _replace_text_on_slide(slide, slide_data: Slide, slide_index: int, theme: dict):
    """Replace text content on a duplicated slide.

    Finds title and content placeholders and replaces their text with
    the generated content. Preserves all shapes, images, and formatting.

    For templates that use freeform shapes instead of placeholders (like
    SlidesCarnival), we identify "title-like" and "content-like" text
    shapes by their position and size on the slide:
    - Title: the largest text shape in the upper portion of the slide
    - Content: the largest text shape in the lower portion
    All other non-placeholder text shapes are cleared (they contain
    template example text that shouldn't appear in the output).
    """
    from pptx.enum.text import PP_ALIGN
    from pptx.util import Pt

    # ── Strategy: try placeholders first, then fall back to text shapes ──

    title_replaced = False
    content_replaced = False

    # Try placeholder-based replacement first
    if slide_data.title:
        title_ph = None
        for ph in slide.placeholders:
            if ph.placeholder_format.idx == 0:
                title_ph = ph
                break

        if title_ph:
            title_ph.text = slide_data.title
            tf = title_ph.text_frame
            tf.word_wrap = True
            for para in tf.paragraphs:
                para.alignment = PP_ALIGN.LEFT
                for run in para.runs:
                    run.font.size = Pt(44 if slide_index == 0 else 28)
                    run.font.bold = True
                    run.font.color.rgb = theme["title_color"]
                    run.font.name = "Calibri"
            title_replaced = True

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
                for run in p.runs:
                    run.font.size = Pt(18)
                    run.font.color.rgb = theme["body_color"]
                    run.font.name = "Calibri"
                p.space_after = Pt(12)
            content_replaced = True

    # If placeholders didn't work, use text-shape-based replacement
    # (for SlidesCarnival-style templates with freeform shapes)
    if not title_replaced or not content_replaced:
        # Collect all non-placeholder text shapes with their positions
        text_shapes = []
        for shape in slide.shapes:
            if shape.is_placeholder:
                continue
            if not shape.has_text_frame:
                continue
            text = shape.text_frame.text.strip()
            if not text:
                continue
            # Calculate area as a measure of importance
            area = (shape.width or 0) * (shape.height or 0)
            text_shapes.append(
                {
                    "shape": shape,
                    "top": shape.top or 0,
                    "left": shape.left or 0,
                    "width": shape.width or 0,
                    "height": shape.height or 0,
                    "area": area,
                    "text": text,
                    "text_len": len(text),
                }
            )

        if text_shapes:
            # Get slide height from the presentation
            try:
                slide_height = (
                    slide.part.package.presentation_part.presentation.slide_height
                )
            except Exception:
                slide_height = 6858000  # default 7.5" in EMU

            # Title: largest text shape in the upper 60% of the slide
            if not title_replaced and slide_data.title:
                upper_shapes = [s for s in text_shapes if s["top"] < slide_height * 0.6]
                if upper_shapes:
                    # Prefer shapes with larger text content (likely the title)
                    best_title = max(upper_shapes, key=lambda s: s["text_len"])
                    shape = best_title["shape"]
                    tf = shape.text_frame
                    # Preserve font formatting from first run
                    first_run_font = None
                    if tf.paragraphs and tf.paragraphs[0].runs:
                        first_run_font = tf.paragraphs[0].runs[0].font
                    tf.clear()
                    p = tf.paragraphs[0]
                    p.text = slide_data.title
                    if first_run_font:
                        for run in p.runs:
                            run.font.size = first_run_font.size or Pt(44)
                            run.font.bold = True
                            run.font.color.rgb = theme["title_color"]
                            run.font.name = first_run_font.name or "Calibri"
                    else:
                        p.font.size = Pt(44 if slide_index == 0 else 28)
                        p.font.bold = True
                        p.font.color.rgb = theme["title_color"]
                        p.font.name = "Calibri"
                    title_replaced = True
                    # Remove this shape from the pool
                    text_shapes = [s for s in text_shapes if s["shape"] is not shape]

            # Content: largest text shape in the lower 50% or remaining largest
            if (
                not content_replaced
                and slide_data.bullets
                and not slide_data.is_section
            ):
                lower_shapes = [
                    s for s in text_shapes if s["top"] >= slide_height * 0.3
                ]
                candidates = lower_shapes if lower_shapes else text_shapes
                if candidates:
                    best_content = max(candidates, key=lambda s: s["area"])
                    shape = best_content["shape"]
                    tf = shape.text_frame
                    # Preserve font formatting
                    first_run_font = None
                    if tf.paragraphs and tf.paragraphs[0].runs:
                        first_run_font = tf.paragraphs[0].runs[0].font
                    tf.clear()
                    for j, bullet in enumerate(slide_data.bullets):
                        if j == 0:
                            p = tf.paragraphs[0]
                        else:
                            p = tf.add_paragraph()
                        p.text = bullet
                        p.level = 0
                        for run in p.runs:
                            run.font.size = (
                                first_run_font.size
                                if first_run_font and first_run_font.size
                                else Pt(18)
                            )
                            run.font.color.rgb = theme["body_color"]
                            run.font.name = (
                                first_run_font.name
                                if first_run_font and first_run_font.name
                                else "Calibri"
                            )
                        p.space_after = Pt(12)
                    content_replaced = True
                    text_shapes = [s for s in text_shapes if s["shape"] is not shape]

            # Clear remaining text shapes (template example text)
            for s in text_shapes:
                shape = s["shape"]
                if shape.has_text_frame:
                    shape.text_frame.clear()

    # Set speaker notes
    if slide_data.notes:
        notes_slide = slide.notes_slide
        notes_tf = notes_slide.notes_text_frame
        notes_tf.text = slide_data.notes
        for para in notes_tf.paragraphs:
            for run in para.runs:
                run.font.size = Pt(14)
                run.font.name = "Calibri"


def _replace_text_in_first_textbox(slide, text: str, size, color):
    """Replace text in the first non-placeholder text shape on a slide."""
    from pptx.enum.text import PP_ALIGN

    for shape in slide.shapes:
        if shape.is_placeholder:
            continue
        if shape.has_text_frame:
            tf = shape.text_frame
            tf.clear()
            p = tf.paragraphs[0]
            p.text = text
            p.font.size = size
            p.font.bold = True
            p.font.color.rgb = color
            p.font.name = "Calibri"
            p.alignment = PP_ALIGN.LEFT
            tf.word_wrap = True
            return  # Only replace the first one


def _replace_bullets_in_textbox(slide, bullets: list[str], theme: dict):
    """Replace text in the second non-placeholder text shape with bullets."""
    from pptx.util import Pt

    found_first = False
    for shape in slide.shapes:
        if shape.is_placeholder:
            continue
        if shape.has_text_frame:
            if not found_first:
                found_first = True
                continue  # Skip the first text box (title)
            tf = shape.text_frame
            tf.word_wrap = True
            tf.clear()
            for j, bullet in enumerate(bullets):
                if j == 0:
                    p = tf.paragraphs[0]
                else:
                    p = tf.add_paragraph()
                p.text = bullet
                p.level = 0
                for run in p.runs:
                    run.font.size = Pt(18)
                    run.font.color.rgb = theme["body_color"]
                    run.font.name = "Calibri"
                p.space_after = Pt(12)
            return


# ── Tag replaceable images ───────────────────────────────────────────


def _tag_replaceable_images(slide):
    """Tag images on the slide for future asset replacement.

    Images whose name or alt-text starts with "REPLACEABLE_" are
    considered replaceable. We set a custom attribute on the shape's
    XML element so the future asset-replacement feature can find them.

    For now, we tag ALL non-placeholder pictures as potentially
    replaceable. The user can later name specific images
    "REPLACEABLE_logo", "REPLACEABLE_hero", etc. in PowerPoint to
    control which ones get replaced.
    """
    from pptx.enum.shapes import MSO_SHAPE_TYPE

    for shape in slide.shapes:
        if shape.is_placeholder:
            continue
        try:
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                # Check if already tagged via name
                name = shape.name or ""
                if name.startswith("REPLACEABLE_"):
                    # Already explicitly tagged — mark it
                    _mark_replaceable(shape)
                else:
                    # Auto-tag: mark as "auto_replaceable" so the future
                    # feature can optionally replace it
                    _mark_auto_replaceable(shape)
        except Exception:
            pass


def _mark_replaceable(shape):
    """Mark a shape as explicitly replaceable (user-named REPLACEABLE_*).

    Uses the 'descr' attribute on cNvPr (which is schema-legal for pictures)
    to store a REPLACEABLE_ marker. We don't use custom XML attributes
    because they're not in the OOXML schema and would trigger PowerPoint
    repair. The 'descr' attribute is the standard way to add alt-text /
    metadata to pictures.
    """
    from pptx.oxml.ns import qn

    # Pictures use <p:nvPicPr>, not <p:nvSpPr> (which is for shapes/autoshapes)
    for tag in ("p:nvPicPr", "p:nvSpPr", "p:nvGrpSpPr", "p:nvCxnSpPr"):
        nvPr = shape._element.find(qn(tag))
        if nvPr is not None:
            cNvPr = nvPr.find(qn("p:cNvPr"))
            if cNvPr is not None:
                # Use descr (alt-text) to store the marker — schema-legal
                cNvPr.set("descr", "REPLACEABLE_EXPLICIT")
                return


def _mark_auto_replaceable(shape):
    """Mark a shape as auto-replaceable (future feature may replace it).

    Uses the 'descr' attribute — same approach as _mark_replaceable.
    """
    from pptx.oxml.ns import qn

    for tag in ("p:nvPicPr", "p:nvSpPr", "p:nvGrpSpPr", "p:nvCxnSpPr"):
        nvPr = shape._element.find(qn(tag))
        if nvPr is not None:
            cNvPr = nvPr.find(qn("p:cNvPr"))
            if cNvPr is not None:
                cNvPr.set("descr", "REPLACEABLE_AUTO")
                return


# ── Post-processing (repair-prompt fix) ──────────────────────────────


# Namespace map for the OOXML XML we rewrite below.
_NS = {
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "vt": "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}

# Relationship type URI for printerSettings — a Windows-only binary blob
# that ships inside templates created by PowerPoint-on-Windows. Opening
# such a file on macOS PowerPoint or Google Slides can trigger a repair
# prompt, so we strip the part and its relationship.
_PRINTER_SETTINGS_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/printerSettings"


def _count_words(slides: list[Slide]) -> int:
    """Rough word count across every slide's title + bullets + notes."""
    total = 0
    for s in slides:
        if s.title:
            total += len(s.title.split())
        for b in s.bullets:
            total += len(b.split())
        if s.notes:
            total += len(s.notes.split())
    return total


def _count_paragraphs(slides: list[Slide]) -> int:
    """Rough paragraph count (one per bullet + one per title)."""
    total = 0
    for s in slides:
        total += 1 if s.title else 0
        total += len(s.bullets)
    return total


def _build_app_xml_titles_vector(slides: list[Slide]) -> tuple[list[str], int]:
    """Build the <vt:vector> contents for <TitlesOfParts>.

    PowerPoint expects the first entry to be the theme name, followed by
    one entry per slide (the slide title, or "" for untitled slides).
    Returns (list_of_lpstr_values, total_size).
    """
    titles = ["Office Theme"]
    for s in slides:
        titles.append(s.title or "")
    return titles, len(titles)


def _fix_app_xml(data: bytes, slides: list[Slide]) -> bytes:
    """Rewrite docProps/app.xml so its counts match the actual file content.

    python-pptx does NOT touch docProps/app.xml when slides are added. A
    template ships with <Slides>0</Slides> and a 1-element <TitlesOfParts>,
    so a generated deck ends up advertising 0 slides while presentation.xml
    lists N. PowerPoint detects this inconsistency and offers to "repair"
    the file — which deletes components and breaks the theme/layout.

    We resync <Slides>, <Notes>, <Paragraphs>, <Words>, the HeadingPairs
    "Slide Titles" count, and the TitlesOfParts vector. The Application
    string is also bumped so it doesn't claim to be a stale PowerPoint build.
    """
    from lxml import etree

    tree = etree.fromstring(data)
    ep_ns = _NS["ep"]
    vt_ns = _NS["vt"]

    slide_count = len(slides)
    notes_count = sum(1 for s in slides if s.notes)

    def _set_text(tag_local: str, value: str) -> None:
        el = tree.find(f"{{{ep_ns}}}{tag_local}")
        if el is None:
            el = etree.SubElement(tree, f"{{{ep_ns}}}{tag_local}")
        el.text = value

    _set_text("Slides", str(slide_count))
    _set_text("Notes", str(notes_count))
    _set_text("HiddenSlides", "0")
    _set_text("Words", str(_count_words(slides)))
    _set_text("Paragraphs", str(_count_paragraphs(slides)))
    _set_text("MMClips", "0")
    _set_text("ScaleCrop", "false")
    _set_text("LinksUpToDate", "false")
    _set_text("SharedDoc", "false")
    _set_text("HyperlinksChanged", "false")
    # Application stays as-is (it records the originating app), but we do
    # not strip it — some viewers sanity-check that it's present.

    # ── HeadingPairs: (Theme, 1), (Slide Titles, N) ──
    titles, total_size = _build_app_xml_titles_vector(slides)
    heading_pairs = tree.find(f"{{{ep_ns}}}HeadingPairs")
    if heading_pairs is not None:
        vector = heading_pairs.find(f"{{{vt_ns}}}vector")
        if vector is not None:
            # Expected layout: [lpstr "Theme", i4 1, lpstr "Slide Titles", i4 N]
            variants = vector.findall(f"{{{vt_ns}}}variant")
            # Update the 4th variant (the slide-titles count)
            if len(variants) >= 4:
                i4 = variants[3].find(f"{{{vt_ns}}}i4")
                if i4 is not None:
                    i4.text = str(slide_count)
            vector.set("size", "4")

    # ── TitlesOfParts: theme name + one entry per slide ──
    titles_of_parts = tree.find(f"{{{ep_ns}}}TitlesOfParts")
    if titles_of_parts is not None:
        vector = titles_of_parts.find(f"{{{vt_ns}}}vector")
        if vector is not None:
            # Clear existing lpstr children and rebuild.
            for child in list(vector):
                vector.remove(child)
            for title in titles:
                lpstr = etree.SubElement(vector, f"{{{vt_ns}}}lpstr")
                lpstr.text = title
            vector.set("size", str(total_size))
            vector.set("baseType", "lpstr")

    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)


def _strip_printer_settings_rels(data: bytes) -> bytes:
    """Drop printerSettings <Relationship> entries from a .rels file.

    Returns the rewritten XML. If no printerSettings rel is present the
    input is returned unchanged (modulo re-serialization).
    """
    from lxml import etree

    tree = etree.fromstring(data)
    rel_ns = _NS["rel"]
    removed = 0
    for rel in list(tree.findall(f"{{{rel_ns}}}Relationship")):
        if rel.get("Type") == _PRINTER_SETTINGS_REL_TYPE:
            tree.remove(rel)
            removed += 1
    if removed:
        _log("stripped %d printerSettings relationship(s)", removed)
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)


def _strip_printer_settings_content_type(data: bytes) -> bytes:
    """Drop the printerSettings <Default> entry from [Content_Types].xml.

    Leaving a Default for an extension with no matching parts is harmless,
    but we strip it for cleanliness so the package looks minimal.
    """
    from lxml import etree

    tree = etree.fromstring(data)
    ct_ns = _NS["ct"]
    for default in list(tree.findall(f"{{{ct_ns}}}Default")):
        if default.get("Extension") == "bin":
            tree.remove(default)
            _log(
                "stripped printerSettings <Default Extension=bin> from [Content_Types].xml"
            )
            break
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)


def _post_process_pptx(output_path: Path, slides: list[Slide]) -> None:
    """Rewrite a saved .pptx in place so it opens without a repair prompt.

    Two fixes:
      1. docProps/app.xml — resync <Slides>/<Notes>/<TitlesOfParts>/... to
         the actual slide content. This is the primary repair trigger: the
         template ships with 0 slides, python-pptx adds N but never touches
         app.xml, so PowerPoint sees "advertised 0, actual N" and offers to
         repair (deleting components in the process).
      2. printerSettings*.bin — strip these Windows-only binary blobs and
         their relationship references. Non-Windows PowerPoint and Google
         Slides can choke on them.

    The rewrite is atomic: we write to a sibling .tmp file, then move it
    over the original so a crash never leaves a half-written deck.
    """
    import shutil
    import zipfile

    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    with zipfile.ZipFile(output_path, "r") as zin:
        with zipfile.ZipFile(
            tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zout:
            for item in zin.infolist():
                name = item.filename

                # Skip Windows-only printerSettings binaries entirely.
                if "printerSettings" in name:
                    continue

                data = zin.read(name)

                if name == "docProps/app.xml":
                    data = _fix_app_xml(data, slides)
                elif name == "ppt/_rels/presentation.xml.rels":
                    data = _strip_printer_settings_rels(data)
                elif name == "[Content_Types].xml":
                    data = _strip_printer_settings_content_type(data)

                # Preserve the original ZipInfo so flags (e.g. compression
                # type, date) stay consistent across the rewritten package.
                zout.writestr(item, data)

    shutil.move(str(tmp_path), str(output_path))
    _log(
        "post-processed %s: app.xml resynced (%d slides, %d notes), printerSettings stripped",
        output_path.name,
        len(slides),
        sum(1 for s in slides if s.notes),
    )


# ── Main PPTX builder ────────────────────────────────────────────────


def _build_pptx(slides: list[Slide], template_path: Path, output_path: Path) -> None:
    """Build a .pptx from parsed slides using a template.

    Strategy:
    1. If the template has pre-existing slides → use SLIDE DUPLICATION:
       deep-copy template slides and replace their text. This preserves
       all images, shapes, and decorative elements.
    2. If the template has no slides (pure layout templates) → use
       LAYOUT-BASED creation: add_slide(layout) and set text on
       placeholders.

    After python-pptx saves, we run _post_process_pptx() to fix the
    OOXML inconsistencies that make PowerPoint/Google Slides prompt
    for repair (stale docProps/app.xml counts + Windows printerSettings).
    """
    from pptx import Presentation

    prs = Presentation(str(template_path))
    theme = _extract_theme_colors(template_path)

    has_template_slides = len(prs.slides) > 0

    if has_template_slides:
        _build_pptx_slide_duplication(prs, slides, theme)
    else:
        _build_pptx_layout_based(prs, slides, theme)

    prs.save(str(output_path))

    # Repair-bug fix: rewrite stale metadata + strip Windows-only parts so
    # PowerPoint and Google Slides open the file without a repair prompt.
    try:
        _post_process_pptx(output_path, slides)
    except Exception as e:
        _log("post-process warning (non-fatal): %s", e)


def _build_pptx_slide_duplication(prs, slides: list[Slide], theme: dict):
    """Build PPTX by duplicating existing template slides.

    1. Categorize template slides by role (title, content, section, closing)
    2. For each generated slide, find the best matching template slide
    3. Deep-copy it (preserving images/shapes/decorations)
    4. Replace text content
    5. Tag replaceable images
    6. Remove all original template slides
    """
    from pptx.oxml.ns import qn

    # Categorize template slides
    template_infos = _categorize_template_slides(prs)
    _log("template slides categorized: %s", [(i.role, i.index) for i in template_infos])

    # Track which template slides we've used (for rotation)
    used_indices: set = set()
    new_slides = []

    total_generated = len(slides)

    for idx, slide_data in enumerate(slides):
        is_last = idx == total_generated - 1

        # Find the best template slide to duplicate
        # Pass is_last info by temporarily setting it on slide_data
        best_info = _find_best_template_slide(
            slide_data, idx, template_infos, used_indices
        )

        if best_info is None:
            _log("no template slide found for generated slide %d — skipping", idx)
            continue

        # If this is the last slide and the best is a content slide,
        # try to find a closing slide instead
        if is_last:
            for info in template_infos:
                if info.role == "closing" and info.index not in used_indices:
                    best_info = info
                    break

        source_slide = prs.slides[best_info.index]
        used_indices.add(best_info.index)

        # Deep-copy the template slide
        new_slide = _duplicate_slide(prs, source_slide)
        new_slides.append(new_slide)

        # Replace text content
        _replace_text_on_slide(new_slide, slide_data, idx, theme)

        # Tag replaceable images
        _tag_replaceable_images(new_slide)

    # Remove all original template slides (keep only our duplicates)
    # The new slides were appended after the originals, so we remove
    # the first len(template_infos) slides.
    sldIdLst = prs.slides._sldIdLst
    original_count = len(template_infos)
    sldId_elements = list(sldIdLst)
    rIds_to_drop = []

    for i in range(original_count):
        if i < len(sldId_elements):
            sldId = sldId_elements[i]
            rId = sldId.get(qn("r:id"))
            if rId:
                rIds_to_drop.append(rId)
            sldIdLst.remove(sldId)

    for rId in rIds_to_drop:
        try:
            prs.part.drop_rel(rId)
        except Exception:
            pass

    _log(
        "slide duplication complete: %d new slides, %d originals removed",
        len(new_slides),
        original_count,
    )


# OOXML vertical-text values for <a:bodyPr vert="...">. PowerPoint renders
# any of these as vertical (rotated) text. "horz" is the normal horizontal
# default; anything else here means the placeholder flows text vertically.
_VERTICAL_VERT_VALUES = frozenset(
    {"eaVert", "vert", "vert270", "wordArtVert", "wordArtVertRtl", "mongolianVert"}
)


def _layout_has_vertical_text(layout) -> bool:
    """Return True if any placeholder in the layout flows text vertically.

    A placeholder is vertical if EITHER:
      - its <p:ph> element carries orient="vert" (the standard marker that
        PowerPoint's "Title and Vertical Text" / "Vertical Title and Text"
        layouts ship with), OR
      - its <a:bodyPr> element carries a vert attribute set to one of the
        vertical values (eaVert/vert/vert270/wordArtVert/...). The built-in
        vertical layouts set BOTH, but we check each independently so we
        also catch hand-rolled templates that only set one.

    This is the root-cause guard for the "text rotated 90 degrees" bug:
    the previous code rotated through every layout that had a title (idx=0)
    + body (idx=1) placeholder, which included the two vertical layouts that
    ship with every stock template — so ~1 in 5 slides ended up vertical.
    """
    from pptx.oxml.ns import qn

    for ph in layout.placeholders:
        ph_el = ph._element
        # <p:ph orient="vert"/> lives inside <p:nvSpPr><p:nvPr>...
        for p_ph in ph_el.iter(qn("p:ph")):
            if p_ph.get("orient") == "vert":
                return True
        # <a:bodyPr vert="eaVert"/> lives inside <p:txBody>...
        for body_pr in ph_el.iter(qn("a:bodyPr")):
            vert = body_pr.get("vert")
            if vert and vert in _VERTICAL_VERT_VALUES:
                return True
    return False


def _build_pptx_layout_based(prs, slides: list[Slide], theme: dict):
    """Build PPTX using add_slide(layout) for templates without pre-existing slides.

    This is the fallback for pure layout templates (corporate/modern/elegant).
    Vertical-text layouts (e.g. "Title and Vertical Text", "Vertical Title
    and Text") are explicitly excluded from the rotation pool so generated
    slides never render text sideways.
    """
    from pptx.dml.color import RGBColor
    from pptx.enum.text import PP_ALIGN, MSO_ANCHOR, MSO_AUTO_SIZE

    # Dynamically find the best layout indices. Defaults point at the two
    # standard stock layouts ("Title Slide" #0 and "Title and Content" #1)
    # which are guaranteed horizontal.
    title_layout_idx = 0
    content_layout_idx = 1
    blank_layout_idx = None

    # Collect every HORIZONTAL content layout for variety. Vertical layouts
    # are skipped — see _layout_has_vertical_text() above.
    content_layout_indices = []

    for li, layout in enumerate(prs.slide_layouts):
        if _layout_has_vertical_text(layout):
            _log("skipping vertical-text layout %d ('%s')", li, layout.name)
            continue

        has_title_ph = any(ph.placeholder_format.idx == 0 for ph in layout.placeholders)
        has_content_ph = any(
            ph.placeholder_format.idx == 1 for ph in layout.placeholders
        )
        has_no_ph = not list(layout.placeholders)

        if has_title_ph and has_content_ph:
            # Remember the first valid content layout as the fallback.
            if content_layout_idx is None:
                content_layout_idx = li
            content_layout_indices.append(li)
        if has_title_ph and not has_content_ph and title_layout_idx is None:
            title_layout_idx = li
        if has_no_ph and blank_layout_idx is None:
            blank_layout_idx = li

    if not content_layout_indices:
        content_layout_indices = [content_layout_idx]
    if blank_layout_idx is None:
        # Fall back to the last layout — but never a vertical one.
        blank_layout_idx = len(prs.slide_layouts) - 1
        if _layout_has_vertical_text(prs.slide_layouts[blank_layout_idx]):
            blank_layout_idx = 6 if len(prs.slide_layouts) > 6 else content_layout_idx

    content_rotation = 0

    for i, slide_data in enumerate(slides):
        if i == 0:
            layout_idx = title_layout_idx
        elif slide_data.is_section:
            layout_idx = blank_layout_idx
        else:
            # Rotate through content layouts for variety
            layout_idx = content_layout_indices[
                content_rotation % len(content_layout_indices)
            ]
            content_rotation += 1

        if layout_idx >= len(prs.slide_layouts):
            layout_idx = content_layout_idx

        layout = prs.slide_layouts[layout_idx]
        slide = prs.slides.add_slide(layout)

        layout_has_title_ph = any(
            ph.placeholder_format.idx == 0 for ph in layout.placeholders
        )

        # Set title
        if slide_data.title:
            if not layout_has_title_ph:
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
                p.font.color.rgb = theme["title_color"]
                p.font.name = "Calibri"
                p.alignment = PP_ALIGN.LEFT
            else:
                title_ph = None
                for ph in slide.placeholders:
                    if ph.placeholder_format.idx == 0:
                        title_ph = ph
                        break

                if title_ph:
                    title_ph.text = slide_data.title
                    tf = title_ph.text_frame
                    tf.word_wrap = True
                    for para in tf.paragraphs:
                        para.alignment = PP_ALIGN.LEFT
                        for run in para.runs:
                            run.font.size = Pt(44 if i == 0 else 28)
                            run.font.bold = True
                            run.font.color.rgb = theme["title_color"]
                            run.font.name = "Calibri"
                else:
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

        # Set subtitle on title slide
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

        # Set bullets
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
                    for run in p.runs:
                        run.font.size = Pt(18)
                        run.font.color.rgb = theme["body_color"]
                        run.font.name = "Calibri"
                    p.space_after = Pt(12)
            else:
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

        # Speaker notes
        if slide_data.notes:
            notes_slide = slide.notes_slide
            notes_tf = notes_slide.notes_text_frame
            notes_tf.text = slide_data.notes
            for para in notes_tf.paragraphs:
                for run in para.runs:
                    run.font.size = Pt(14)
                    run.font.name = "Calibri"

    _log("layout-based build complete: %d slides", len(slides))


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

    # Note: No LibreOffice cleaning step needed — the _duplicate_slide
    # function now properly handles rId remapping and skips the notesSlide
    # relationship, producing PowerPoint-compatible files directly from
    # python-pptx. This works even without LibreOffice installed.

    # ── Generate thumbnail via LibreOffice (if available) ──
    # The thumbnail URL is always exposed; the endpoint generates on-demand
    # if the cached file is missing (e.g. LibreOffice was installed after
    # the presentation was created, or generation failed at the time).
    thumb_path = reports_dir / f"{report_id}_thumb.jpg"
    try:
        from app.services.integrations import libreoffice

        if libreoffice.is_available():
            _log("generating thumbnail via LibreOffice...")
            cached = await libreoffice.generate_and_cache_thumbnail(
                output_path, thumb_path, max_width=400
            )
            if cached:
                _log("thumbnail generated at creation time")
            else:
                _log("thumbnail generation failed — will retry on-demand")
        else:
            _log("LibreOffice not available — skipping thumbnail generation")
    except Exception as e:
        _log("thumbnail generation error (non-fatal): %s", e)

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
        "thumbnail_url": f"/api/reports/{report_id}/thumbnail",
        "report_id": report_id,
        "created_at": int(time.time()),
        "slide_count": len(slides),
        "template": template_path.stem,
    }
