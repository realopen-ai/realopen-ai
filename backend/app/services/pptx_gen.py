"""
PowerPoint presentation generation service — THEME ENGINE (v3, full rewrite).

LLM generates slide-structured Markdown; we render a .pptx programmatically
with a *theme* (palette + fonts + decorative style).

WHY A REWRITE — the previous two strategies both triggered PowerPoint's
"repair" prompt:

  * SLIDE DUPLICATION (deep-copying template slides) produced dangling
    relationship ids / orphaned parts.
  * LAYOUT-BASED generation inherited placeholders from the shipped
    templates — including the two stock *vertical-text* layouts (the
    90°-rotated-text bug) — and the templates themselves carried a
    Windows printerSettings blob from the python-pptx default template.

THE NEW APPROACH — "render from a pristine base, never inherit":

  1. We ALWAYS start from python-pptx's bundled default presentation
     (a minimal, guaranteed-valid package) and set 16:9.
  2. Slides are added on the *Blank* layout and drawn entirely with
     explicit textboxes / autoshapes via the high-level python-pptx API.
     No placeholders are inherited → no vertical text, no geometry
     surprises, no layout quirks. The 90°-rotation bug is impossible
     by construction.
  3. The three built-in themes (corporate / modern / elegant) are plain
     Python data — no .pptx template files participate in generation,
     so template-file corruption can never leak into a deck.
  4. Custom uploaded templates are used AS A THEME: we extract their
     colour palette + fonts (theme1.xml / master background) and feed
     the same renderer. Their XML never enters the output.
  5. Every string that touches XML passes through _sanitize_text(),
     stripping all XML-invalid characters (the classic LLM-output
     repair trigger).
  6. After saving, a safe post-process pass (a) resyncs docProps/app.xml
     counts in place — element order is preserved because we only ever
     UPDATE existing elements in the pristine default's app.xml — and
     (b) strips the Windows printerSettings part shipped inside the
     python-pptx default template (a known repair trigger on
     PowerPoint-for-Mac and Google Slides).

LibreOffice is OPTIONAL: it is used only to render a thumbnail (and the
reports API uses it for on-demand PDF preview). Generation itself is
pure python-pptx and produces identical output with or without it.

Public API (unchanged — the use_pptx_gen agent tool and the reports API
depend on it):
    generate_presentation(topic, outline, template) -> dict
    AVAILABLE_TEMPLATES, DEFAULT_TEMPLATE
    _get_available_templates_from_db(), _get_templates_with_descriptions()
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx
from pptx.dml.color import RGBColor
from pptx.util import Emu, Inches, Pt

from app.config import settings

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[pptx_gen] {formatted}", flush=True)


# ── Dirs / template registry (public surface preserved) ──────────────


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

# Hard cap so a runaway LLM cannot produce an unusable 60-slide deck.
MAX_SLIDES = 25


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
    return [(slug, desc) for slug, desc in _builtin_theme_catalog()]


# ── Text sanitization — the LLM-output repair killer ─────────────────
#
# XML 1.0 (which OOXML uses) only allows: #x9 | #xA | #xD | #x20-#xD7FF |
# #xE000-#xFFFD | #x10000-#x10FFFF.  LLM output occasionally contains
# control characters (vertical tab, form feed, C1 controls, lone
# surrogates …).  python-pptx happily serializes some of them, and
# PowerPoint then refuses the file → "repair" prompt.  We strip them all
# before any string reaches the XML layer.

# Characters that are XML-valid but render unpredictably in text boxes
# (zero-width joiners, bidi marks, line/paragraph separators, BOM, …).
_WEIRD_WS_RE = re.compile(
    "[\u200b\u200c\u200d\u200e\u200f\u2028\u2029\u202a-\u202f" "\u205f-\u206f\ufeff]"
)

# Everything not allowed in presentation text. Stricter than the raw XML
# Char production: we also drop #x7F-#x9F (DEL + C1 controls) and lone
# surrogates — they are XML-tolerated but hostile to OOXML parsers.
_XML_INVALID_RE = re.compile(
    "[^\t\n\r\x20-\x7e\u00a0-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]"
)


def _sanitize_text(text: Optional[str]) -> str:
    """Return an XML-safe, presentation-safe version of *text*."""
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\t", " ")
    text = _WEIRD_WS_RE.sub(" ", text)
    text = _XML_INVALID_RE.sub("", text)
    # Collapse runs of spaces created by the substitutions above.
    text = re.sub(r" {3,}", "  ", text)
    return text.strip()


def _first_sentence(text: str, limit: int = 120) -> str:
    """First sentence of *text*, hard-truncated to *limit* chars."""
    text = _sanitize_text(text)
    if not text:
        return ""
    for sep in (". ", "! ", "? "):
        idx = text.find(sep)
        if 0 < idx < limit:
            return text[: idx + 1].strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if " " in cut[80:]:
        cut = cut[: cut.rfind(" ")]
    return cut.rstrip(",;:—-") + "…"


# ── Colour helpers ────────────────────────────────────────────────────


def _hex(s: str) -> RGBColor:
    s = s.lstrip("#")
    return RGBColor(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _to_int(c: RGBColor) -> int:
    return (int(c[0]) << 16) | (int(c[1]) << 8) | int(c[2])


def _luminance(c: RGBColor) -> float:
    """Relative luminance in [0, 1] (ITU-R BT.601 approximation)."""
    return (0.299 * int(c[0]) + 0.587 * int(c[1]) + 0.114 * int(c[2])) / 255.0


def _blend(c1: RGBColor, c2: RGBColor, t: float) -> RGBColor:
    """Blend *c1* over *c2* with ratio *t* ∈ [0,1] (t=0 → c2, t=1 → c1)."""
    t = max(0.0, min(1.0, t))
    return RGBColor(
        int(round(int(c1[0]) * t + int(c2[0]) * (1 - t))),
        int(round(int(c1[1]) * t + int(c2[1]) * (1 - t))),
        int(round(int(c1[2]) * t + int(c2[2]) * (1 - t))),
    )


def _is_dark(c: RGBColor) -> bool:
    return _luminance(c) < 0.45


# ── Theme specification ───────────────────────────────────────────────


@dataclass(frozen=True)
class ThemeSpec:
    """A rendering theme: palette + fonts + decorative style."""

    name: str
    display_name: str
    bg: RGBColor  # slide background
    title: RGBColor  # heading text on bg
    body: RGBColor  # body text on bg
    muted: RGBColor  # secondary text (footers, slide numbers)
    accent: RGBColor  # primary accent
    accent2: RGBColor  # secondary accent
    on_accent: RGBColor  # text placed on accent surfaces
    title_font: str
    body_font: str
    decor: str = "bars"  # "bars" | "circles" | "frame"
    dark: bool = False  # background is dark

    # Derived, blended lazily per render (kept out of __eq__ noise)
    def soft_accent(self) -> RGBColor:
        """Accent blended into the background — subtle decorative fills."""
        return _blend(self.accent, self.bg, 0.22)

    def soft_accent2(self) -> RGBColor:
        return _blend(self.accent2, self.bg, 0.16)

    def faint_accent(self) -> RGBColor:
        """Very subtle — big background circles etc."""
        return _blend(self.accent, self.bg, 0.12)

    def on_accent_soft(self) -> RGBColor:
        """Text on accent surfaces, slightly recessed (section numbers)."""
        return _blend(self.on_accent, self.accent, 0.45)


def _builtin_theme_catalog() -> list[tuple[str, str]]:
    """(slug, description) list mirroring the DB seed metadata in main.py."""
    return [
        (t.name, t.display_name + " — " + _BUILTIN_DESCRIPTIONS[t.name])
        for t in BUILTIN_THEMES.values()
    ]


_BUILTIN_DESCRIPTIONS = {
    "corporate": "Navy blue professional theme",
    "modern": "Teal and orange vibrant theme",
    "elegant": "Dark purple and gold sophisticated theme",
}


BUILTIN_THEMES: dict[str, ThemeSpec] = {
    # Deep navy + amber — boardroom classic.
    "corporate": ThemeSpec(
        name="corporate",
        display_name="Corporate",
        bg=_hex("0F2740"),
        title=_hex("FFFFFF"),
        body=_hex("C7D3E2"),
        muted=_hex("7E93AC"),
        accent=_hex("F2A33C"),
        accent2=_hex("3E7CB1"),
        on_accent=_hex("0F2740"),
        title_font="Calibri",
        body_font="Calibri",
        decor="bars",
        dark=True,
    ),
    # White + teal/orange — startup energy.
    "modern": ThemeSpec(
        name="modern",
        display_name="Modern",
        bg=_hex("FFFFFF"),
        title=_hex("0E3A36"),
        body=_hex("3C4A48"),
        muted=_hex("8AA09C"),
        accent=_hex("0D9488"),
        accent2=_hex("F97316"),
        on_accent=_hex("FFFFFF"),
        title_font="Trebuchet MS",
        body_font="Calibri",
        decor="circles",
        dark=False,
    ),
    # Aubergine + gold — evening keynote.
    "elegant": ThemeSpec(
        name="elegant",
        display_name="Elegant",
        bg=_hex("251B33"),
        title=_hex("F3ECD9"),
        body=_hex("CFC4DE"),
        muted=_hex("9C8EB4"),
        accent=_hex("D4AF37"),
        accent2=_hex("8E7CC3"),
        on_accent=_hex("251B33"),
        title_font="Georgia",
        body_font="Georgia",
        decor="frame",
        dark=True,
    ),
}


# ── Custom-template theme extraction ──────────────────────────────────
#
# Uploaded templates are used AS A THEME: we read their theme palette
# and fonts, then render with the same bullet-proof engine.  Their XML
# never enters the generated file, so even a quirky upload cannot
# corrupt a deck.

_SYSCLR_MAP = {"windowText": "000000", "window": "FFFFFF", "none": "000000"}


def _theme_from_template(template_path: Path) -> ThemeSpec:
    """Extract a ThemeSpec from a .pptx template's theme1.xml + master."""
    from pptx import Presentation

    fallback = BUILTIN_THEMES[DEFAULT_TEMPLATE]
    try:
        prs = Presentation(str(template_path))
        master = prs.slide_masters[0]

        # ── Locate the theme part through the master's relationships ──
        theme_el = None
        for rel in master.part.rels.values():
            if rel.reltype.endswith("/theme"):
                from lxml import etree

                theme_el = etree.fromstring(rel.target_part.blob)
                break
        if theme_el is None:
            return fallback

        A = "http://schemas.openxmlformats.org/drawingml/2006/main"

        def _clr(tag: str) -> Optional[RGBColor]:
            el = theme_el.find(f".//{{{A}}}clrScheme/{{{A}}}{tag}")
            if el is None:
                return None
            srgb = el.find(f"{{{A}}}srgbClr")
            if srgb is not None and re.fullmatch(
                r"[0-9A-Fa-f]{6}", srgb.get("val", "")
            ):
                return _hex(srgb.get("val"))
            sysc = el.find(f"{{{A}}}sysClr")
            if sysc is not None:
                val = sysc.get("lastClr") or _SYSCLR_MAP.get(sysc.get("val", ""), "")
                if val and re.fullmatch(r"[0-9A-Fa-f]{6}", val):
                    return _hex(val)
            return None

        # ── Fonts ──
        major = minor = None
        fs = theme_el.find(f".//{{{A}}}fontScheme")
        if fs is not None:
            mj = fs.find(f".//{{{A}}}majorFont/{{{A}}}latin")
            mn = fs.find(f".//{{{A}}}minorFont/{{{A}}}latin")
            major = mj.get("typeface") if mj is not None else None
            minor = mn.get("typeface") if mn is not None else None
        title_font = (major or "").strip() or "Calibri"
        body_font = (minor or "").strip() or title_font

        # ── Palette ──
        dk1 = _clr("dk1") or _hex("1A1A1A")
        lt1 = _clr("lt1") or _hex("FFFFFF")
        dk2 = _clr("dk2") or dk1
        lt2 = _clr("lt2") or lt1
        accent = _clr("accent1") or _hex("3E7CB1")
        accent2 = _clr("accent2") or accent

        # ── Master background (explicit solid fill wins) ──
        from pptx.oxml.ns import qn

        bg: Optional[RGBColor] = None
        cSld = master._element.find(qn("p:cSld"))
        if cSld is not None:
            bgPr = cSld.find(f"{qn('p:bg')}/{qn('p:bgPr')}")
            if bgPr is not None:
                srgb = bgPr.find(f"{qn('a:solidFill')}/{qn('a:srgbClr')}")
                if srgb is not None and re.fullmatch(
                    r"[0-9A-Fa-f]{6}", srgb.get("val", "")
                ):
                    bg = _hex(srgb.get("val"))

        if bg is None:
            # No explicit master background → light deck on lt1.
            bg = lt1 if not _is_dark(lt1) else _blend(lt1, _hex("FFFFFF"), 0.6)
        dark = _is_dark(bg)

        # Contrast-aware text colours.
        if dark:
            title_c = lt1 if _luminance(lt1) > 0.5 else _hex("FFFFFF")
            body_c = _blend(title_c, bg, 0.75)
            muted_c = _blend(title_c, bg, 0.45)
        else:
            title_c = dk1 if _luminance(dk1) < 0.5 else _hex("1A1A1A")
            body_c = _blend(title_c, bg, 0.82)
            muted_c = _blend(title_c, bg, 0.5)

        # Text on accent surfaces: maximise contrast against the accent.
        on_accent = _hex("FFFFFF") if _luminance(accent) < 0.55 else _hex("111111")

        # Decorative style: detect a full-width outline rect ("frame")
        # on the title layout; otherwise bars on dark / circles on light.
        decor = "bars" if dark else "circles"
        try:
            layout0 = prs.slide_layouts[0]
            slide_w = int(prs.slide_width or 0)
            for shape in layout0.shapes:
                if shape.is_placeholder:
                    continue
                spPr = shape._element.find(qn("p:spPr"))
                if spPr is None:
                    continue
                prst = spPr.find(qn("a:prstGeom"))
                if prst is None or prst.get("prst") != "rect":
                    continue
                if spPr.find(qn("a:noFill")) is None or spPr.find(qn("a:ln")) is None:
                    continue
                ext = spPr.find(f"{qn('a:xfrm')}/{qn('a:ext')}")
                if (
                    ext is not None
                    and slide_w
                    and int(ext.get("cx", "0")) > 0.8 * slide_w
                ):
                    decor = "frame"
                    break
        except Exception:
            pass

        return ThemeSpec(
            name=template_path.stem.lower(),
            display_name=template_path.stem.replace("_", " ").title(),
            bg=bg,
            title=title_c,
            body=body_c,
            muted=muted_c,
            accent=accent,
            accent2=accent2,
            on_accent=on_accent,
            title_font=title_font,
            body_font=body_font,
            decor=decor,
            dark=dark,
        )
    except Exception as e:
        _log(
            "theme extraction failed for %s (%s) — using default", template_path.name, e
        )
        return fallback


def _resolve_theme(
    template_name: Optional[str],
) -> tuple[str, ThemeSpec]:
    """Resolve a template slug to (slug, ThemeSpec).

    Built-ins come from code. Anything else is looked up on disk
    (templates dir, then custom/ subdir) and used AS A THEME. Unknown
    slugs fall back to the default theme.
    """
    slug = (template_name or DEFAULT_TEMPLATE).lower().strip()
    if slug in BUILTIN_THEMES:
        return slug, BUILTIN_THEMES[slug]

    templates_dir = _get_templates_dir()
    for candidate in (
        templates_dir / f"{slug}.pptx",
        templates_dir / "custom" / f"{slug}.pptx",
    ):
        if candidate.exists():
            _log("using custom template as theme: %s", candidate.name)
            return slug, _theme_from_template(candidate)

    _log("template '%s' not found — falling back to '%s'", slug, DEFAULT_TEMPLATE)
    return DEFAULT_TEMPLATE, BUILTIN_THEMES[DEFAULT_TEMPLATE]


# ── LLM prompt ────────────────────────────────────────────────────────

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


# ── Slide parsing ─────────────────────────────────────────────────────


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

        # If a content slide has no bullets but has notes, extract
        # a summary bullet from the notes so the slide isn't empty.
        if not slide.is_section and not slide.bullets and slide.notes:
            first_sentence = slide.notes.split(".")[0].strip()
            if first_sentence:
                slide.bullets.append(first_sentence + ".")

        if slide.title or slide.bullets:
            slides.append(slide)

    return slides


_CLOSING_RE = re.compile(
    r"thank\s*you|questions\b|conclusion|key\s*takeaways?|summary|q&a",
    re.IGNORECASE,
)


def _is_closing_slide(slide: Slide, index: int, total: int) -> bool:
    """True when the last slide is a classic 'thank you' style closer."""
    if index != total - 1 or total < 3:
        return False
    return bool(_CLOSING_RE.search(slide.title or ""))


# ── The renderer ──────────────────────────────────────────────────────
#
# 16:9 canvas: 13.333in × 7.5in.  All geometry is explicit — nothing is
# inherited from layouts — so every theme renders pixel-identically in
# PowerPoint, Google Slides and LibreOffice.

_SLIDE_W = Emu(12192000)  # 13.333 in
_SLIDE_H = Emu(6858000)  # 7.5 in

_MARGIN = Inches(0.7)


def _blank_layout(prs):
    """The Blank layout of the pristine default template (no content placeholders)."""
    for layout in prs.slide_layouts:
        if layout.name and layout.name.lower() == "blank":
            return layout
    # Robust fallback: first layout with no title/body/object placeholders.
    for layout in prs.slide_layouts:
        content_types = {
            "TITLE (1)",
            "CENTER_TITLE (3)",
            "OBJECT (7)",
            "BODY (2)",
            "SUBTITLE (4)",
        }
        if not any(
            str(ph.placeholder_format.type) in content_types
            for ph in layout.placeholders
        ):
            return layout
    return prs.slide_layouts[6]


def _set_bg(slide, color: RGBColor) -> None:
    """Slide background via the supported high-level API (schema-safe)."""
    fill = slide.background.fill
    fill.solid()
    fill.fore_color.rgb = color


def _add_rect(
    slide,
    x,
    y,
    w,
    h,
    color: RGBColor,
    *,
    line: bool = False,
    line_color: Optional[RGBColor] = None,
    line_w: float = 1.25,
):
    """Add a flat rectangle. line=True → outline only, no fill."""
    from pptx.enum.shapes import MSO_SHAPE

    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, w, h)
    shape.shadow.inherit = False
    if line:
        shape.fill.background()
        if line_color is not None:
            shape.line.color.rgb = line_color
        shape.line.width = Pt(line_w)
    else:
        shape.fill.solid()
        shape.fill.fore_color.rgb = color
        shape.line.fill.background()
    return shape


def _add_ellipse(slide, x, y, d, color: RGBColor):
    from pptx.enum.shapes import MSO_SHAPE

    shape = slide.shapes.add_shape(MSO_SHAPE.OVAL, x, y, d, d)
    shape.shadow.inherit = False
    shape.fill.solid()
    shape.fill.fore_color.rgb = color
    shape.line.fill.background()
    return shape


def _add_text(
    slide,
    x,
    y,
    w,
    h,
    text: str,
    *,
    size: float,
    color: RGBColor,
    font: str,
    bold: bool = False,
    align: str = "left",
    anchor: str = "top",
    line_spacing: Optional[float] = None,
    italic: bool = False,
):
    """Add a single-paragraph textbox. Returns the textbox shape."""
    from pptx.enum.text import MSO_ANCHOR, PP_ALIGN

    box = slide.shapes.add_textbox(x, y, w, h)
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0
    tf.vertical_anchor = {
        "top": MSO_ANCHOR.TOP,
        "middle": MSO_ANCHOR.MIDDLE,
        "bottom": MSO_ANCHOR.BOTTOM,
    }[anchor]

    p = tf.paragraphs[0]
    p.alignment = {
        "left": PP_ALIGN.LEFT,
        "center": PP_ALIGN.CENTER,
        "right": PP_ALIGN.RIGHT,
    }[align]
    if line_spacing:
        p.line_spacing = line_spacing

    run = p.add_run()
    run.text = text
    f = run.font
    f.size = Pt(size)
    f.bold = bold
    f.italic = italic
    f.color.rgb = color
    f.name = font
    return box


def _font_for(theme: ThemeSpec, *, title: bool = False) -> str:
    return theme.title_font if title else theme.body_font


# ── Slide renderers, one per visual role ──────────────────────────────


def _draw_title_slide(
    slide, sd: Slide, theme: ThemeSpec, topic: str, closing: bool = False
) -> None:
    """Title / closing slide: big statement + accent rule + subtitle."""
    _set_bg(slide, theme.bg)

    title = _sanitize_text(sd.title) or ("Thank You" if closing else topic)
    subtitle = _first_sentence(sd.notes) or (
        "" if closing else _first_sentence(topic, limit=90)
    )

    tsize = 48 if len(title) <= 40 else (42 if len(title) <= 55 else 36)

    if theme.decor == "frame":
        # Elegant: thin gold frame + centered composition.
        _add_rect(
            slide,
            Inches(0.42),
            Inches(0.42),
            _SLIDE_W - Inches(0.84),
            _SLIDE_H - Inches(0.84),
            theme.accent,
            line=True,
            line_color=theme.accent,
            line_w=1.25,
        )
        _add_rect(
            slide,
            _SLIDE_W / 2 - Inches(0.09),
            Inches(1.55),
            Inches(0.18),
            Inches(0.18),
            theme.accent,
        )
        _add_text(
            slide,
            Inches(1.2),
            Inches(2.35),
            _SLIDE_W - Inches(2.4),
            Inches(1.7),
            title,
            size=tsize,
            color=theme.title,
            font=_font_for(theme, title=True),
            bold=True,
            align="center",
            anchor="middle",
        )
        _add_rect(
            slide,
            _SLIDE_W / 2 - Inches(0.8),
            Inches(4.25),
            Inches(1.6),
            Inches(0.045),
            theme.accent,
        )
        if subtitle:
            _add_text(
                slide,
                Inches(1.6),
                Inches(4.55),
                _SLIDE_W - Inches(3.2),
                Inches(1.0),
                subtitle,
                size=16,
                color=theme.muted,
                font=_font_for(theme),
                align="center",
                italic=True,
                line_spacing=1.15,
            )
        return

    if theme.decor == "circles":
        # Modern: playful overlapping circles, left-aligned type.
        _add_ellipse(
            slide, Inches(9.7), Inches(-1.5), Inches(5.4), theme.faint_accent()
        )
        _add_ellipse(slide, Inches(11.15), Inches(0.65), Inches(1.15), theme.accent2)
        _add_ellipse(slide, Inches(-0.9), Inches(5.6), Inches(2.4), theme.soft_accent())
    else:
        # Corporate: authoritative left bar + grounded base strip.
        _add_rect(slide, 0, 0, Inches(0.28), _SLIDE_H, theme.accent)
        _add_ellipse(slide, Inches(10.4), Inches(4.6), Inches(3.6), theme.soft_accent())
        _add_ellipse(
            slide, Inches(11.6), Inches(3.7), Inches(1.5), theme.soft_accent2()
        )

    _add_text(
        slide,
        Inches(1.05),
        Inches(2.15),
        Inches(9.6),
        Inches(2.1),
        title,
        size=tsize,
        color=theme.title,
        font=_font_for(theme, title=True),
        bold=True,
        line_spacing=1.02,
    )
    _add_rect(
        slide, Inches(1.08), Inches(4.45), Inches(1.35), Inches(0.06), theme.accent
    )
    if subtitle:
        _add_text(
            slide,
            Inches(1.05),
            Inches(4.75),
            Inches(9.2),
            Inches(1.1),
            subtitle,
            size=17,
            color=theme.muted,
            font=_font_for(theme),
            line_spacing=1.2,
        )


def _draw_section_slide(slide, sd: Slide, theme: ThemeSpec, number: int) -> None:
    """Section divider: full-bleed accent panel + oversized number."""
    _set_bg(slide, theme.bg)

    title = _sanitize_text(sd.title) or "Section"
    tsize = 34 if len(title) <= 44 else 28
    num = f"{number:02d}"

    if theme.decor == "frame":
        # Elegant: centered champagne title between gold rules.
        _add_text(
            slide,
            Inches(1.0),
            Inches(2.62),
            _SLIDE_W - Inches(2.0),
            Inches(0.5),
            f"SECTION {num}",
            size=13,
            color=theme.accent,
            font=_font_for(theme),
            bold=True,
            align="center",
        )
        _add_rect(
            slide,
            _SLIDE_W / 2 - Inches(1.3),
            Inches(3.25),
            Inches(2.6),
            Inches(0.03),
            theme.accent,
        )
        _add_text(
            slide,
            Inches(1.2),
            Inches(3.55),
            _SLIDE_W - Inches(2.4),
            Inches(1.4),
            title,
            size=tsize,
            color=theme.title,
            font=_font_for(theme, title=True),
            bold=True,
            align="center",
        )
        _add_rect(
            slide,
            _SLIDE_W / 2 - Inches(1.3),
            Inches(5.05),
            Inches(2.6),
            Inches(0.03),
            theme.accent,
        )
        return

    # bars / circles: accent panel on the left third.
    panel_w = Inches(4.6)
    _add_rect(slide, 0, 0, panel_w, _SLIDE_H, theme.accent)
    _add_text(
        slide,
        Inches(0.75),
        Inches(2.05),
        Inches(3.4),
        Inches(2.4),
        num,
        size=110,
        color=theme.on_accent_soft(),
        font=_font_for(theme, title=True),
        bold=True,
    )
    _add_text(
        slide,
        panel_w + Inches(0.75),
        Inches(2.9),
        _SLIDE_W - panel_w - Inches(1.5),
        Inches(1.9),
        title,
        size=tsize,
        color=theme.title,
        font=_font_for(theme, title=True),
        bold=True,
        anchor="middle",
        line_spacing=1.05,
    )
    _add_rect(
        slide,
        panel_w + Inches(0.77),
        Inches(2.62),
        Inches(1.0),
        Inches(0.05),
        theme.accent,
    )


def _bullet_metrics(bullets: list[str]) -> tuple[float, float]:
    """(font size pt, space-after pt) adapted to bullet count + length."""
    n = len(bullets)
    longest = max((len(b) for b in bullets), default=0)
    if n <= 3:
        size, space = 20.0, 16.0
    elif n <= 5:
        size, space = 18.0, 13.0
    elif n <= 7:
        size, space = 16.0, 10.0
    else:
        size, space = 14.5, 8.0
    if longest > 105:
        size = max(12.0, size - 2.0)
    elif longest > 78:
        size = max(13.0, size - 1.0)
    return size, space


_BULLET_MARKER = {"bars": "▪", "circles": "▪", "frame": "•"}


def _draw_content_slide(slide, sd: Slide, theme: ThemeSpec) -> None:
    """Standard content slide: title + accent underline + bullet stack."""
    _set_bg(slide, theme.bg)

    title = _sanitize_text(sd.title) or ""
    bullets = [_sanitize_text(b) for b in sd.bullets]
    bullets = [b for b in bullets if b]

    tsize = 30 if len(title) <= 46 else 26
    _add_text(
        slide,
        _MARGIN,
        Inches(0.48),
        _SLIDE_W - 2 * _MARGIN,
        Inches(0.85),
        title,
        size=tsize,
        color=theme.title,
        font=_font_for(theme, title=True),
        bold=True,
    )
    _add_rect(
        slide,
        _MARGIN + Inches(0.02),
        Inches(1.32),
        Inches(1.0),
        Inches(0.05),
        theme.accent,
    )

    if not bullets:
        return

    size, space = _bullet_metrics(bullets)
    marker = _BULLET_MARKER.get(theme.decor, "▪")

    box = slide.shapes.add_textbox(
        _MARGIN + Inches(0.2),
        Inches(1.75),
        _SLIDE_W - 2 * _MARGIN - Inches(0.2),
        _SLIDE_H - Inches(2.55),
    )
    tf = box.text_frame
    tf.word_wrap = True
    tf.margin_left = 0
    tf.margin_right = 0
    tf.margin_top = 0
    tf.margin_bottom = 0

    from pptx.enum.text import PP_ALIGN

    for i, bullet in enumerate(bullets):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.alignment = PP_ALIGN.LEFT
        p.space_after = Pt(space)
        p.line_spacing = 1.06

        m = p.add_run()
        m.text = marker + "  "
        m.font.size = Pt(size)
        m.font.bold = True
        m.font.color.rgb = theme.accent
        m.font.name = _font_for(theme)

        r = p.add_run()
        r.text = bullet
        r.font.size = Pt(size)
        r.font.color.rgb = theme.body
        r.font.name = _font_for(theme)

    # Decorative corner accent (kept clear of the text column).
    if theme.decor == "circles":
        _add_ellipse(
            slide, Inches(11.75), Inches(-1.05), Inches(2.6), theme.faint_accent()
        )
    elif theme.decor == "bars":
        _add_rect(slide, 0, 0, _SLIDE_W, Inches(0.075), theme.accent)
    else:  # frame
        _add_rect(
            slide,
            _SLIDE_W - Inches(1.15),
            Inches(0.52),
            Inches(0.45),
            Inches(0.45),
            theme.accent,
        )


def _draw_footer(slide, theme: ThemeSpec, topic: str, page: int, total: int) -> None:
    """Small footer: topic label left, page x/y right (muted)."""
    label = _sanitize_text(topic).upper()
    if len(label) > 42:
        label = label[:41].rstrip() + "…"
    if label:
        _add_text(
            slide,
            _MARGIN,
            _SLIDE_H - Inches(0.42),
            Inches(8.0),
            Inches(0.3),
            label,
            size=9,
            color=theme.muted,
            font=_font_for(theme),
        )
    _add_text(
        slide,
        _SLIDE_W - _MARGIN - Inches(2.0),
        _SLIDE_H - Inches(0.42),
        Inches(2.0),
        Inches(0.3),
        f"{page} / {total}",
        size=10,
        color=theme.muted,
        font=_font_for(theme),
        align="right",
    )


def _set_notes(slide, notes: str) -> None:
    """Speaker notes via the supported high-level API."""
    notes = _sanitize_text(notes)
    if not notes:
        return
    tf = slide.notes_slide.notes_text_frame
    tf.text = notes
    for para in tf.paragraphs:
        for run in para.runs:
            run.font.size = Pt(12)
            run.font.name = "Calibri"


def _render_deck(
    slides: list[Slide], theme: ThemeSpec, topic: str, output_path: Path
) -> None:
    """Render the whole deck from the pristine python-pptx default."""
    from pptx import Presentation

    prs = Presentation()
    prs.slide_width = _SLIDE_W
    prs.slide_height = _SLIDE_H

    blank = _blank_layout(prs)
    total = len(slides)
    section_no = 0

    for i, sd in enumerate(slides):
        slide = prs.slides.add_slide(blank)

        if i == 0:
            _draw_title_slide(slide, sd, theme, topic)
        elif sd.is_section:
            section_no += 1
            _draw_section_slide(slide, sd, theme, section_no)
        elif _is_closing_slide(sd, i, total):
            _draw_title_slide(slide, sd, theme, topic, closing=True)
        else:
            _draw_content_slide(slide, sd, theme)

        if i != 0:
            _draw_footer(slide, theme, topic, i + 1, total)
        if sd.notes:
            _set_notes(slide, sd.notes)

    # Atomic-ish save: write to a sibling temp file, then move over.
    tmp = output_path.with_suffix(output_path.suffix + ".tmp")
    prs.save(str(tmp))
    tmp.replace(output_path)


# ── Post-processing: app.xml resync + printerSettings strip ───────────
#
# The ONLY two things we touch after python-pptx saves:
#   1. docProps/app.xml — python-pptx never updates it. We resync the
#      slide/word/paragraph counts and the TitlesOfParts vector, updating
#      EXISTING elements only (the pristine default contains them all in
#      schema order), so element order — and therefore schema validity —
#      is preserved.
#   2. printerSettings*.bin — the python-pptx default template ships
#      this Windows-only blob; it triggers repair prompts in
#      PowerPoint-for-Mac and Google Slides. We drop the part, its
#      relationship, and the <Default Extension="bin"> content type.

_NS = {
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ep": "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
    "vt": "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes",
}

_PRINTER_SETTINGS_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/printerSettings"


def _count_words(slides: list[Slide]) -> int:
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
    total = 0
    for s in slides:
        total += 1 if s.title else 0
        total += len(s.bullets)
    return total


def _fix_app_xml(data: bytes, slides: list[Slide], theme_name: str) -> bytes:
    """Resync docProps/app.xml counts IN PLACE (schema order preserved)."""
    from lxml import etree

    tree = etree.fromstring(data)
    ep = _NS["ep"]
    vt = _NS["vt"]

    slide_count = len(slides)
    notes_count = sum(1 for s in slides if s.notes)

    def _set_existing(tag_local: str, value: str) -> None:
        el = tree.find(f"{{{ep}}}{tag_local}")
        if el is not None:
            el.text = value

    # Update ONLY elements that already exist (the pristine default's
    # app.xml has all of them) — no SubElement appends that could break
    # the CT_ExtendedProperties element sequence.
    _set_existing("Slides", str(slide_count))
    _set_existing("Notes", str(notes_count))
    _set_existing("Words", str(_count_words(slides)))
    _set_existing("Paragraphs", str(_count_paragraphs(slides)))
    _set_existing("HiddenSlides", "0")
    _set_existing("MMClips", "0")

    # HeadingPairs: [Theme, 1, Slide Titles, N] — update the count variant.
    hp = tree.find(f"{{{ep}}}HeadingPairs")
    if hp is not None:
        vector = hp.find(f"{{{vt}}}vector")
        if vector is not None:
            variants = vector.findall(f"{{{vt}}}variant")
            if len(variants) >= 4:
                i4 = variants[3].find(f"{{{vt}}}i4")
                if i4 is not None:
                    i4.text = str(slide_count)

    # TitlesOfParts: theme display name + one entry per slide title.
    top = tree.find(f"{{{ep}}}TitlesOfParts")
    if top is not None:
        vector = top.find(f"{{{vt}}}vector")
        if vector is not None:
            for child in list(vector):
                vector.remove(child)
            titles = [theme_name] + [_sanitize_text(s.title) or "" for s in slides]
            for t in titles:
                lpstr = etree.SubElement(vector, f"{{{vt}}}lpstr")
                lpstr.text = t
            vector.set("size", str(len(titles)))
            vector.set("baseType", "lpstr")

    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)


def _strip_printer_settings_rels(data: bytes) -> bytes:
    from lxml import etree

    tree = etree.fromstring(data)
    rel_ns = _NS["rel"]
    for rel in list(tree.findall(f"{{{rel_ns}}}Relationship")):
        if rel.get("Type") == _PRINTER_SETTINGS_REL_TYPE:
            tree.remove(rel)
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)


def _strip_bin_content_type(data: bytes) -> bytes:
    from lxml import etree

    tree = etree.fromstring(data)
    ct_ns = _NS["ct"]
    for default in list(tree.findall(f"{{{ct_ns}}}Default")):
        if default.get("Extension") == "bin":
            tree.remove(default)
            break
    return etree.tostring(tree, xml_declaration=True, encoding="UTF-8", standalone=True)


def _post_process_pptx(output_path: Path, slides: list[Slide], theme_name: str) -> None:
    """Rewrite the saved .pptx so it opens without a repair prompt."""
    import shutil
    import zipfile

    tmp_path = output_path.with_suffix(output_path.suffix + ".tmp")

    with zipfile.ZipFile(output_path, "r") as zin:
        with zipfile.ZipFile(
            tmp_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zout:
            for item in zin.infolist():
                name = item.filename

                # Drop Windows-only printerSettings parts entirely.
                if "printerSettings" in name:
                    continue

                data = zin.read(name)

                if name == "docProps/app.xml":
                    data = _fix_app_xml(data, slides, theme_name)
                elif name == "ppt/_rels/presentation.xml.rels":
                    data = _strip_printer_settings_rels(data)
                elif name == "[Content_Types].xml":
                    data = _strip_bin_content_type(data)

                zout.writestr(item, data)

    shutil.move(str(tmp_path), str(output_path))
    _log(
        "post-processed %s: app.xml resynced (%d slides), printerSettings stripped",
        output_path.name,
        len(slides),
    )


# ── Public API ────────────────────────────────────────────────────────


def _build_pptx(
    slides: list[Slide], theme: ThemeSpec, topic: str, output_path: Path
) -> None:
    """Build the .pptx: render → save → safe post-process → verify."""
    _render_deck(slides, theme, topic, output_path)

    try:
        _post_process_pptx(output_path, slides, theme.display_name)
    except Exception as e:
        _log("post-process warning (non-fatal): %s", e)

    # Defense in depth: reopen with python-pptx and sanity-check.
    try:
        from pptx import Presentation as _P

        check = _P(str(output_path))
        if len(check.slides) != len(slides):
            _log(
                "WARNING: slide count mismatch after save (%d on disk vs %d expected)",
                len(check.slides),
                len(slides),
            )
    except Exception as e:
        _log("WARNING: generated file failed reopen check: %s", e)


async def generate_presentation(
    topic: str,
    outline: Optional[str] = None,
    template: Optional[str] = None,
) -> dict:
    report_id = str(uuid.uuid4())
    reports_dir = _get_reports_dir()
    slug, theme = _resolve_theme(template)

    markdown_content = await _generate_slide_markdown(topic, outline)
    slides = _parse_slides(markdown_content)
    if not slides:
        raise RuntimeError("No slides parsed from LLM output")
    if len(slides) > MAX_SLIDES:
        _log("capping deck at %d slides (LLM produced %d)", MAX_SLIDES, len(slides))
        slides = slides[:MAX_SLIDES]
    _log("parsed %d slides from markdown (theme=%s)", len(slides), slug)

    output_path = reports_dir / f"{report_id}.pptx"
    await asyncio.to_thread(_build_pptx, slides, theme, topic, output_path)

    if not output_path.exists():
        raise RuntimeError(f"PPTX file was not created: {output_path}")

    file_size = output_path.stat().st_size
    _log(
        "presentation saved: %s (%d bytes, %d slides)",
        output_path.name,
        file_size,
        len(slides),
    )

    # ── Optional LibreOffice thumbnail (works fine without it) ──
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
        "template": slug,
    }
