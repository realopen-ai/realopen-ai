"""Tests for the PPTX generation service (app/services/pptx_gen.py) — the
theme-engine rewrite. The existing test_pptx_*_viewer.py files cover the
slide/notes VIEWERS; this file covers the GENERATOR itself.

Scope:
1. Text sanitization — _sanitize_text / _first_sentence (XML-hostile LLM
   output handling).
2. Colour helpers — _hex/_to_int/_luminance/_blend/_is_dark.
3. Themes — builtin catalog, ThemeSpec derived colours, _resolve_theme
   (builtin / unknown / on-disk custom template) and _theme_from_template
   (real .pptx theme extraction + corrupt-file fallback).
4. Slide parsing — _parse_slides (titles, sections, bullets, numbered
   lists, notes, notes-derived bullets) and _is_closing_slide.
5. Layout/metrics helpers — _bullet_metrics, _count_words,
   _count_paragraphs, _blank_layout, _draw_footer, _set_notes, _add_text.
6. Post-processing XML units — _fix_app_xml, _strip_printer_settings_rels,
   _strip_bin_content_type (pure lxml over hand-built XML).
7. Rendering e2e — _render_deck/_build_pptx produce REAL .pptx files in
   tmp dirs for every builtin theme (16:9, notes, printerSettings
   stripped, app.xml resynced); post-process failure is non-fatal.
8. LLM flow — _generate_slide_markdown with mocked model resolution +
   providers.chat_once (fence stripping, empty content, missing model).
9. Public API — generate_presentation with the LLM step mocked: result
   contract, slide capping, filename sanitization, error branches,
   optional LibreOffice thumbnail (mocked).
10. DB template helpers — _get_available_templates_from_db /
    _get_templates_with_descriptions against a fake session factory.
11. _get_reports_dir fallback logic via a Path double (first writable
    candidate → repo-data fallback → /app retry; no real dirs created),
    plus the bug-fixed _strip_printer_settings_rels behaviour.

Mocks:
- NO Ollama/LLM: providers.chat_once and _generate_slide_markdown are
  patched (never a real network call).
- NO database: app.db.session.async_session_factory is replaced with a
  fake context manager.
- LibreOffice availability/thumbnail calls are patched.
- Real files are generated with python-pptx into tmp_path only.
"""

import sys
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from unittest.mock import AsyncMock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.services import pptx_gen as pg  # noqa: E402

SAMPLE_MD = """# Deck Title
> Opening notes here.

---

## Section One
> Section notes.

- Bullet **one**
- Bullet two
* Star bullet

---

Content Slide Title
1. Numbered first
2. Numbered second
Plain text becomes a bullet

---

# Thank You
> Bye for now.
"""

# ── Fake DB layer ───────────────────────────────────────────────────


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class FakeDB:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, *_args, **_kwargs):
        return FakeResult(self._rows)


class FakeSessionCtx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


def _db_patch(rows=None, error=None):
    if error:

        def factory():
            raise error

    else:
        factory = lambda: FakeSessionCtx(FakeDB(rows))  # noqa: E731
    return patch("app.db.session.async_session_factory", factory)


@pytest.fixture
def reports_dir(tmp_path, monkeypatch):
    """Isolate report output into the test's tmp dir."""
    reports = tmp_path / "reports"
    reports.mkdir()
    monkeypatch.setattr(pg, "_get_reports_dir", lambda: reports)
    return reports


@pytest.fixture
def no_libreoffice():
    with patch("app.services.integrations.libreoffice.is_available", return_value=False):
        yield


# ══════════════════════════════════════════════════════════════════════
# 1. Sanitization
# ══════════════════════════════════════════════════════════════════════


class TestSanitizeText:
    def test_empty_and_none(self):
        assert pg._sanitize_text("") == ""
        assert pg._sanitize_text(None) == ""

    def test_normal_text_untouched(self):
        assert pg._sanitize_text("Hello, world!") == "Hello, world!"

    def test_newlines_normalized(self):
        assert pg._sanitize_text("a\r\nb\rc") == "a\nb\nc"

    def test_tabs_replaced_by_space(self):
        assert pg._sanitize_text("a\tb") == "a b"

    def test_weird_whitespace_replaced(self):
        # Zero-width joiner + BOM + line separator → single spaces.
        assert pg._sanitize_text("a\u200bb\ufeffc\u2028d") == "a b c d"

    def test_control_chars_stripped(self):
        # Vertical tab, form feed, DEL, C1 control are removed entirely.
        assert pg._sanitize_text("a\x0bb\x0cc\x7fd\x9fe") == "abcde"

    def test_long_space_runs_collapsed(self):
        assert pg._sanitize_text("a      b") == "a  b"

    def test_result_stripped(self):
        assert pg._sanitize_text("  padded  ") == "padded"


class TestFirstSentence:
    def test_empty(self):
        assert pg._first_sentence("") == ""
        assert pg._first_sentence(None) == ""

    def test_sentence_split_on_period(self):
        assert pg._first_sentence("Hello there. How are you?") == "Hello there."

    def test_sentence_split_on_exclamation_and_question(self):
        assert pg._first_sentence("Wow! Really?") == "Wow!"
        assert pg._first_sentence("Is it? Yes.") == "Is it?"

    def test_short_text_returned_whole(self):
        assert pg._first_sentence("One single sentence") == "One single sentence"

    def test_long_text_truncated_with_ellipsis(self):
        text = " ".join(["word"] * 60)
        out = pg._first_sentence(text, limit=100)
        assert out.endswith("…")
        assert len(out) <= 102

    def test_separator_beyond_limit_ignored(self):
        text = "x" * 150 + ". tail"
        out = pg._first_sentence(text, limit=50)
        assert out.endswith("…")
        assert len(out) <= 52


# ══════════════════════════════════════════════════════════════════════
# 2. Colour helpers
# ══════════════════════════════════════════════════════════════════════


class TestColorHelpers:
    def test_hex_parses_with_and_without_hash(self):
        assert pg._hex("#0F2740") == pg._hex("0F2740")

    def test_to_int_roundtrip(self):
        color = pg._hex("3E7CB1")
        assert pg._to_int(color) == 0x3E7CB1

    def test_luminance_extremes(self):
        assert pg._luminance(pg._hex("FFFFFF")) == 1.0
        assert pg._luminance(pg._hex("000000")) == 0.0
        assert 0.0 < pg._luminance(pg._hex("808080")) < 1.0

    def test_blend_endpoints(self):
        c1, c2 = pg._hex("FF0000"), pg._hex("0000FF")
        assert pg._blend(c1, c2, 0.0) == c2
        assert pg._blend(c1, c2, 1.0) == c1

    def test_blend_clamps_ratio(self):
        c1, c2 = pg._hex("FF0000"), pg._hex("0000FF")
        assert pg._blend(c1, c2, 5.0) == c1
        assert pg._blend(c1, c2, -3.0) == c2

    def test_blend_midpoint(self):
        assert pg._blend(pg._hex("000000"), pg._hex("FFFFFF"), 0.5) == pg._hex("808080")

    def test_is_dark(self):
        assert pg._is_dark(pg._hex("0F2740")) is True
        assert pg._is_dark(pg._hex("FFFFFF")) is False


# ══════════════════════════════════════════════════════════════════════
# 3. Themes
# ══════════════════════════════════════════════════════════════════════


class TestThemes:
    def test_builtin_themes_match_template_list(self):
        assert set(pg.BUILTIN_THEMES) == set(pg.AVAILABLE_TEMPLATES)
        assert pg.DEFAULT_TEMPLATE == "corporate"
        assert pg.MAX_SLIDES == 25

    def test_builtin_theme_flags(self):
        assert pg.BUILTIN_THEMES["corporate"].dark is True
        assert pg.BUILTIN_THEMES["modern"].dark is False
        assert pg.BUILTIN_THEMES["elegant"].decor == "frame"
        assert pg.BUILTIN_THEMES["modern"].decor == "circles"

    def test_builtin_catalog_has_descriptions(self):
        catalog = pg._builtin_theme_catalog()
        assert ("corporate", "Corporate — Navy blue professional theme") in catalog
        assert len(catalog) == len(pg.AVAILABLE_TEMPLATES)

    def test_theme_spec_derived_colors(self):
        theme = pg.BUILTIN_THEMES["corporate"]
        assert theme.soft_accent() != theme.accent
        assert theme.soft_accent2() != theme.accent2
        assert theme.faint_accent() != theme.accent
        assert theme.on_accent_soft() != theme.on_accent

    def test_resolve_theme_builtin(self):
        slug, theme = pg._resolve_theme("corporate")
        assert slug == "corporate"
        assert theme is pg.BUILTIN_THEMES["corporate"]

    def test_resolve_theme_normalizes_case_and_spaces(self):
        slug, theme = pg._resolve_theme("  MODERN  ")
        assert slug == "modern"
        assert theme is pg.BUILTIN_THEMES["modern"]

    def test_resolve_theme_none_uses_default(self):
        slug, theme = pg._resolve_theme(None)
        assert slug == pg.DEFAULT_TEMPLATE
        assert theme is pg.BUILTIN_THEMES[pg.DEFAULT_TEMPLATE]

    def test_resolve_theme_unknown_falls_back(self):
        slug, theme = pg._resolve_theme("does-not-exist")
        assert slug == pg.DEFAULT_TEMPLATE
        assert theme is pg.BUILTIN_THEMES[pg.DEFAULT_TEMPLATE]

    def test_resolve_theme_custom_file_on_disk(self, tmp_path, monkeypatch):
        from pptx import Presentation

        template = tmp_path / "brandkit.pptx"
        Presentation().save(str(template))
        monkeypatch.setattr(pg, "_get_templates_dir", lambda: tmp_path)

        slug, theme = pg._resolve_theme("brandkit")
        assert slug == "brandkit"
        assert theme.name == "brandkit"
        assert theme.display_name == "Brandkit"
        assert theme.title_font == "Calibri"
        assert theme.body_font == "Calibri"
        assert theme.dark is False

    def test_resolve_theme_custom_in_custom_subdir(self, tmp_path, monkeypatch):
        from pptx import Presentation

        custom_dir = tmp_path / "custom"
        custom_dir.mkdir()
        Presentation().save(str(custom_dir / "shiny.pptx"))
        monkeypatch.setattr(pg, "_get_templates_dir", lambda: tmp_path)

        slug, theme = pg._resolve_theme("shiny")
        assert slug == "shiny"
        assert theme.name == "shiny"

    def test_theme_from_template_corrupt_file_falls_back(self, tmp_path):
        broken = tmp_path / "broken.pptx"
        broken.write_bytes(b"this is not a zip file")
        theme = pg._theme_from_template(broken)
        assert theme is pg.BUILTIN_THEMES[pg.DEFAULT_TEMPLATE]

    def test_theme_from_dark_template_with_frame(self, tmp_path):
        """Explicit dark master background + full-width outline on the title
        layout → dark theme with the 'frame' decorative style."""
        from lxml import etree
        from pptx import Presentation
        from pptx.dml.color import RGBColor
        from pptx.oxml.ns import qn

        prs = Presentation()
        master = prs.slide_masters[0]
        master.background.fill.solid()
        master.background.fill.fore_color.rgb = RGBColor(0x11, 0x11, 0x33)

        # A full-width, no-fill outlined rect on layout 0 → "frame" decor.
        sp_tree = prs.slide_layouts[0].shapes._spTree
        sp = etree.SubElement(sp_tree, qn("p:sp"))
        nv = etree.SubElement(sp, qn("p:nvSpPr"))
        cnv = etree.SubElement(nv, qn("p:cNvPr"))
        cnv.set("id", "99")
        cnv.set("name", "FrameOutline")
        etree.SubElement(nv, qn("p:cNvSpPr"))
        sp_pr = etree.SubElement(sp, qn("p:spPr"))
        xfrm = etree.SubElement(sp_pr, qn("a:xfrm"))
        off = etree.SubElement(xfrm, qn("a:off"))
        off.set("x", "0")
        off.set("y", "0")
        ext = etree.SubElement(xfrm, qn("a:ext"))
        ext.set("cx", str(int(prs.slide_width)))
        ext.set("cy", "457200")
        prst = etree.SubElement(sp_pr, qn("a:prstGeom"))
        prst.set("prst", "rect")
        etree.SubElement(prst, qn("a:avLst"))
        etree.SubElement(sp_pr, qn("a:noFill"))
        ln = etree.SubElement(sp_pr, qn("a:ln"))
        fill = etree.SubElement(ln, qn("a:solidFill"))
        clr = etree.SubElement(fill, qn("a:srgbClr"))
        clr.set("val", "D4AF37")

        template = tmp_path / "darkframe.pptx"
        prs.save(str(template))

        theme = pg._theme_from_template(template)
        assert theme.dark is True
        assert theme.decor == "frame"
        assert theme.bg == pg._hex("111133")
        assert theme.title == pg._hex("FFFFFF")
        assert theme.display_name == "Darkframe"

    def test_theme_extraction_without_theme_rel_falls_back(self):
        """A master without a theme relationship → builtin default theme."""
        from unittest.mock import MagicMock

        fake_master = MagicMock()
        fake_master.part.rels = {}  # no theme relationship
        fake_prs = MagicMock()
        fake_prs.slide_masters = [fake_master]
        with patch("pptx.Presentation", return_value=fake_prs):
            theme = pg._theme_from_template(Path("never-opened.pptx"))
        assert theme is pg.BUILTIN_THEMES[pg.DEFAULT_TEMPLATE]

    def test_theme_extraction_tolerates_missing_colors(self, tmp_path):
        """A theme missing dk2 / carrying an invalid lt2 falls back gracefully."""
        import re
        import zipfile

        from pptx import Presentation

        template = tmp_path / "partial.pptx"
        Presentation().save(str(template))

        with zipfile.ZipFile(template) as src:
            entries = {name: src.read(name) for name in src.namelist()}
        theme = entries["ppt/theme/theme1.xml"].decode("utf-8")
        # dk2 removed entirely; lt2 carries an unparsable colour value.
        theme = re.sub(r"<a:dk2>.*?</a:dk2>", "", theme, flags=re.DOTALL)
        theme = re.sub(
            r'(<a:lt2>)<a:srgbClr val="[0-9A-Fa-f]{6}"/>(</a:lt2>)',
            r"\1<a:srgbClr val='XYZ'/>\2",
            theme,
        )
        entries["ppt/theme/theme1.xml"] = theme.encode("utf-8")
        with zipfile.ZipFile(template, "w") as out:
            for name, data in entries.items():
                out.writestr(name, data)

        spec = pg._theme_from_template(template)
        # dk2 → dk1 fallback, lt2 → lt1 fallback; extraction still succeeds
        # (a light deck on the lt1 background with default decor).
        assert spec.bg == pg._hex("FFFFFF")
        assert spec.dark is False
        assert spec.decor == "circles"
        assert spec.accent == pg._hex("4F81BD")
        assert spec.accent2 == pg._hex("C0504D")

    def test_theme_extraction_skips_malformed_decor_shapes(self, tmp_path):
        """Non-frame decorative shapes are skipped; broken geometry is tolerated."""
        from lxml import etree
        from pptx import Presentation
        from pptx.oxml.ns import qn

        prs = Presentation()
        sp_tree = prs.slide_layouts[0].shapes._spTree
        next_id = [90]

        def _new_sp():
            sp = etree.SubElement(sp_tree, qn("p:sp"))
            nv = etree.SubElement(sp, qn("p:nvSpPr"))
            cnv = etree.SubElement(nv, qn("p:cNvPr"))
            cnv.set("id", str(next_id[0]))
            cnv.set("name", f"Decor {next_id[0]}")
            next_id[0] += 1
            etree.SubElement(nv, qn("p:cNvSpPr"))
            return sp

        # 1. Shape with no spPr at all → skipped.
        _new_sp()

        # 2. spPr without prstGeom → skipped.
        sp = _new_sp()
        sp_pr = etree.SubElement(sp, qn("p:spPr"))
        etree.SubElement(sp_pr, qn("a:noFill"))

        # 3. Rect with a solid fill (no noFill) → not an outline → skipped.
        sp = _new_sp()
        sp_pr = etree.SubElement(sp, qn("p:spPr"))
        prst = etree.SubElement(sp_pr, qn("a:prstGeom"))
        prst.set("prst", "rect")
        fill = etree.SubElement(sp_pr, qn("a:solidFill"))
        etree.SubElement(fill, qn("a:srgbClr")).set("val", "000000")

        # 4. Rect with noFill but no outline → skipped.
        sp = _new_sp()
        sp_pr = etree.SubElement(sp, qn("p:spPr"))
        etree.SubElement(sp_pr, qn("a:prstGeom")).set("prst", "rect")
        etree.SubElement(sp_pr, qn("a:noFill"))

        # 5. Full outline rect whose extent is unparsable → tolerated.
        sp = _new_sp()
        sp_pr = etree.SubElement(sp, qn("p:spPr"))
        etree.SubElement(sp_pr, qn("a:prstGeom")).set("prst", "rect")
        etree.SubElement(sp_pr, qn("a:noFill"))
        etree.SubElement(sp_pr, qn("a:ln"))
        xfrm = etree.SubElement(sp_pr, qn("a:xfrm"))
        ext = etree.SubElement(xfrm, qn("a:ext"))
        ext.set("cx", "not-a-number")
        ext.set("cy", "100")

        template = tmp_path / "decor.pptx"
        prs.save(str(template))

        spec = pg._theme_from_template(template)
        # No valid frame found → light default decor, extraction succeeds.
        assert spec.decor in ("bars", "circles")
        assert spec.dark is False


# ══════════════════════════════════════════════════════════════════════
# 4. Slide parsing
# ══════════════════════════════════════════════════════════════════════


class TestParseSlides:
    def test_parses_full_markdown(self):
        slides = pg._parse_slides(SAMPLE_MD)
        assert len(slides) == 4

        title_slide = slides[0]
        assert title_slide.title == "Deck Title"
        assert title_slide.is_section is False
        assert title_slide.notes == "Opening notes here."
        # No bullets → first sentence of the notes becomes the bullet.
        assert title_slide.bullets == ["Opening notes here."]

        section = slides[1]
        assert section.is_section is True
        assert section.title == "Section One"
        assert section.notes == "Section notes."
        assert section.bullets == ["Bullet one", "Bullet two", "Star bullet"]

        content = slides[2]
        assert content.title == "Content Slide Title"
        assert content.bullets == [
            "Numbered first",
            "Numbered second",
            "Plain text becomes a bullet",
        ]

        closing = slides[3]
        assert closing.title == "Thank You"
        assert closing.bullets == ["Bye for now."]

    def test_empty_input(self):
        assert pg._parse_slides("") == []
        assert pg._parse_slides("   \n  \n") == []

    def test_separators_only(self):
        assert pg._parse_slides("---\n---\n---") == []

    def test_separator_with_surrounding_whitespace(self):
        slides = pg._parse_slides("# A\n --- \n# B")
        assert [s.title for s in slides] == ["A", "B"]

    def test_bold_and_emphasis_stripped_from_bullets(self):
        slides = pg._parse_slides("# T\n- **bold** and *italic*")
        assert slides[0].bullets == ["bold and italic"]

    def test_bare_quote_note_line(self):
        slides = pg._parse_slides("# T\n>note without space")
        assert slides[0].notes == "note without space"

    def test_multiline_notes_joined(self):
        slides = pg._parse_slides("# T\n> line one\n> line two")
        assert slides[0].notes == "line one\nline two"

    def test_h2_marks_section(self):
        slides = pg._parse_slides("## Just a section")
        assert slides[0].is_section is True
        assert slides[0].title == "Just a section"

    def test_plain_text_before_title_becomes_title(self):
        slides = pg._parse_slides("First line is title\nSecond becomes bullet")
        assert slides[0].title == "First line is title"
        assert slides[0].bullets == ["Second becomes bullet"]

    def test_empty_segments_dropped(self):
        slides = pg._parse_slides("# Real\n\n---\n\n   \n")
        assert len(slides) == 1
        assert slides[0].title == "Real"


class TestIsClosingSlide:
    def _slide(self, title):
        slide = pg.Slide()
        slide.title = title
        return slide

    def test_last_thank_you_slide(self):
        assert pg._is_closing_slide(self._slide("Thank You"), 4, 5) is True

    @pytest.mark.parametrize("title", ["Questions?", "Conclusion", "Key Takeaways", "Q&A"])
    def test_other_closing_patterns(self, title):
        assert pg._is_closing_slide(self._slide(title), 2, 3) is True

    def test_not_last_slide(self):
        assert pg._is_closing_slide(self._slide("Thank You"), 0, 5) is False

    def test_short_deck_never_closing(self):
        assert pg._is_closing_slide(self._slide("Thank You"), 1, 2) is False

    def test_regular_last_slide(self):
        assert pg._is_closing_slide(self._slide("Budget Details"), 3, 4) is False

    def test_empty_title(self):
        assert pg._is_closing_slide(self._slide(""), 2, 3) is False


# ══════════════════════════════════════════════════════════════════════
# 5. Metrics + layout helpers
# ══════════════════════════════════════════════════════════════════════


class TestBulletMetrics:
    def test_few_bullets(self):
        assert pg._bullet_metrics(["a"]) == (20.0, 16.0)
        assert pg._bullet_metrics(["a", "b", "c"]) == (20.0, 16.0)

    def test_medium_counts(self):
        assert pg._bullet_metrics(["a"] * 4) == (18.0, 13.0)
        assert pg._bullet_metrics(["a"] * 5) == (18.0, 13.0)
        assert pg._bullet_metrics(["a"] * 7) == (16.0, 10.0)

    def test_many_bullets(self):
        assert pg._bullet_metrics(["a"] * 8) == (14.5, 8.0)

    def test_long_bullets_shrink_font(self):
        long_bullet = "x" * 106
        assert pg._bullet_metrics([long_bullet]) == (18.0, 16.0)
        medium_bullet = "x" * 79
        assert pg._bullet_metrics([medium_bullet]) == (19.0, 16.0)

    def test_empty_list(self):
        assert pg._bullet_metrics([]) == (20.0, 16.0)


class TestCounts:
    def _slides(self):
        first = pg.Slide()
        first.title = "Two words"
        first.bullets = ["one two three", "four"]
        first.notes = "five six"
        second = pg.Slide()
        second.title = "Solo"
        second.bullets = []
        second.notes = ""
        return [first, second]

    def test_count_words(self):
        # 2 + (3 + 1) + 2 + 1 + 0 = 9
        assert pg._count_words(self._slides()) == 9

    def test_count_paragraphs(self):
        # titles (2) + bullets (2)
        assert pg._count_paragraphs(self._slides()) == 4


class TestLayoutHelpers:
    def test_blank_layout_found(self):
        from pptx import Presentation

        layout = pg._blank_layout(Presentation())
        assert layout.name.lower() == "blank"

    def test_blank_layout_falls_back_to_placeholder_free_layout(self):
        from pptx import Presentation

        prs = Presentation()
        for layout in prs.slide_layouts:
            layout.name = "NotBlank"
        chosen = pg._blank_layout(prs)
        # No "blank" name → the first layout without content placeholders
        # (the stock Blank layout, index 6) wins.
        assert chosen is prs.slide_layouts[6]

    def test_blank_layout_last_resort(self):
        """Every layout carries content placeholders → index 6 is returned."""
        from types import SimpleNamespace
        from unittest.mock import MagicMock

        layout = MagicMock()
        layout.name = "Title Slide"
        layout.placeholders = [SimpleNamespace(placeholder_format=SimpleNamespace(
            type="TITLE (1)"
        ))]
        layouts = MagicMock()
        layouts.__iter__.return_value = [layout]
        layouts.__getitem__.return_value = layout
        prs = MagicMock()
        prs.slide_layouts = layouts

        assert pg._blank_layout(prs) is layout

    def test_draw_footer_and_notes_on_real_slide(self):
        from pptx import Presentation

        prs = Presentation()
        prs.slide_width = pg._SLIDE_W
        prs.slide_height = pg._SLIDE_H
        slide = prs.slides.add_slide(prs.slide_layouts[6])

        pg._draw_footer(
            slide, pg.BUILTIN_THEMES["modern"], "a very long topic " * 5, 2, 3
        )
        pg._set_notes(slide, "Speaker notes text")

        texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame]
        assert any("…" in t and len(t) <= 45 for t in texts)  # truncated label
        assert "2 / 3" in texts
        assert slide.notes_slide.notes_text_frame.text == "Speaker notes text"

    def test_draw_footer_short_topic_not_truncated(self):
        from pptx import Presentation

        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        pg._draw_footer(slide, pg.BUILTIN_THEMES["corporate"], "short", 1, 2)
        texts = [sh.text_frame.text for sh in slide.shapes if sh.has_text_frame]
        assert "SHORT" in texts

    def test_set_notes_empty_is_noop(self):
        from pptx import Presentation

        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        pg._set_notes(slide, "")
        assert not slide.has_notes_slide

    def test_add_text_formatting_options(self):
        from pptx import Presentation

        prs = Presentation()
        slide = prs.slides.add_slide(prs.slide_layouts[6])
        box = pg._add_text(
            slide,
            pg._MARGIN,
            pg.Inches(1),
            pg.Inches(5),
            pg.Inches(1),
            "Styled",
            size=24,
            color=pg._hex("FF0000"),
            font="Georgia",
            bold=True,
            italic=True,
            align="center",
            anchor="middle",
            line_spacing=1.2,
        )
        run = box.text_frame.paragraphs[0].runs[0]
        assert run.text == "Styled"
        assert run.font.size == pg.Pt(24)
        assert run.font.bold is True
        assert run.font.italic is True
        assert run.font.name == "Georgia"


# ══════════════════════════════════════════════════════════════════════
# 6. Post-processing XML units
# ══════════════════════════════════════════════════════════════════════

EP = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
VT = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def _printer_rels_xml(rel_type):
    """A slide .rels part carrying one printerSettings rel (Type=rel_type)
    + one normal rel."""
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{REL}">
  <Relationship Id="rId1" Type="{rel_type}"
    Target="printerSettings/printerSettings1.bin"/>
  <Relationship Id="rId2"
    Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"
    Target="slideMasters/slideMaster1.xml"/>
</Relationships>'''.encode()


def _app_xml(slides_count=1):
    return f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Properties xmlns="{EP}" xmlns:vt="{VT}">
  <Slides>{slides_count}</Slides><Notes>0</Notes><Words>1</Words>
  <Paragraphs>1</Paragraphs><HiddenSlides>0</HiddenSlides><MMClips>0</MMClips>
  <HeadingPairs><vt:vector size="4" baseType="variant">
    <vt:variant><vt:lpstr>Theme</vt:lpstr></vt:variant>
    <vt:variant><vt:i4>{slides_count}</vt:i4></vt:variant>
    <vt:variant><vt:lpstr>Slide Titles</vt:lpstr></vt:variant>
    <vt:variant><vt:i4>{slides_count}</vt:i4></vt:variant>
  </vt:vector></HeadingPairs>
  <TitlesOfParts><vt:vector size="2" baseType="lpstr">
    <vt:lpstr>Office Theme</vt:lpstr><vt:lpstr>Old</vt:lpstr>
  </vt:vector></TitlesOfParts>
</Properties>""".encode()


def _slides(n, with_notes=False):
    out = []
    for i in range(n):
        slide = pg.Slide()
        slide.title = f"Slide {i + 1}"
        slide.bullets = ["one two", "three"]
        if with_notes:
            slide.notes = "note text"
        out.append(slide)
    return out


class TestFixAppXml:
    def test_resyncs_counts_and_titles(self):
        from lxml import etree

        slides = _slides(3, with_notes=True)
        out = pg._fix_app_xml(_app_xml(slides_count=1), slides, "Corporate")
        tree = etree.fromstring(out)

        assert tree.find(f"{{{EP}}}Slides").text == "3"
        assert tree.find(f"{{{EP}}}Notes").text == "3"
        # Words: 3 titles (2 words each) + 6 bullets (2-3 words) + 3 notes.
        words = int(tree.find(f"{{{EP}}}Words").text)
        assert words == pg._count_words(slides)
        assert tree.find(f"{{{EP}}}Paragraphs").text == str(pg._count_paragraphs(slides))

        vector = tree.find(f"{{{EP}}}TitlesOfParts/{{{VT}}}vector")
        entries = [lp.text for lp in vector.findall(f"{{{VT}}}lpstr")]
        assert entries[0] == "Corporate"
        assert entries[1:] == ["Slide 1", "Slide 2", "Slide 3"]
        assert vector.get("size") == "4"

        hp = tree.find(f"{{{EP}}}HeadingPairs/{{{VT}}}vector")
        variants = hp.findall(f"{{{VT}}}variant")
        assert variants[3].find(f"{{{VT}}}i4").text == "3"

    def test_handles_empty_deck(self):
        from lxml import etree

        out = pg._fix_app_xml(_app_xml(), [], "Modern")
        tree = etree.fromstring(out)
        assert tree.find(f"{{{EP}}}Slides").text == "0"
        vector = tree.find(f"{{{EP}}}TitlesOfParts/{{{VT}}}vector")
        assert [lp.text for lp in vector.findall(f"{{{VT}}}lpstr")] == ["Modern"]


class TestStripXmlParts:
    def test_printer_settings_rels_bug_compatibly_kept(self):
        from lxml import etree

        out = pg._strip_printer_settings_rels(
            _printer_rels_xml(pg._PRINTER_SETTINGS_REL_TYPE[0])
        )
        ids = [r.get("Id") for r in etree.fromstring(out).findall(f"{{{REL}}}Relationship")]
        # Non-printer relationships are always preserved.
        assert "rId2" in ids
        # NOTE: _PRINTER_SETTINGS_REL_TYPE is a 1-tuple in the current
        # source (trailing comma), so the Type comparison never matches
        # and the rel is bug-compatibly kept. The printerSettings PART
        # is still dropped by name in _post_process_pptx, which is what
        # actually protects the generated file. This assertion documents
        # both behaviors and stays correct if the tuple is ever fixed.
        if isinstance(pg._PRINTER_SETTINGS_REL_TYPE, str):
            assert "rId1" not in ids
        else:
            assert "rId1" in ids

    def test_printer_settings_rels_removed_with_string_rel_type(self):
        """Bug-fixed behaviour: with _PRINTER_SETTINGS_REL_TYPE as the
        plain string it was clearly meant to be (upstream it is a 1-tuple
        — stray trailing comma — so the Type comparison never matches),
        the printerSettings relationship IS dropped. Pins the intended
        removal logic (the tree.remove branch) for whoever fixes the
        constant."""
        from lxml import etree

        fixed_type = pg._PRINTER_SETTINGS_REL_TYPE[0]
        assert isinstance(fixed_type, str)  # sanity: unpack the 1-tuple
        xml = _printer_rels_xml(fixed_type)  # built BEFORE the patch
        with patch.object(pg, "_PRINTER_SETTINGS_REL_TYPE", fixed_type):
            out = pg._strip_printer_settings_rels(xml)

        rels = etree.fromstring(out).findall(f"{{{REL}}}Relationship")
        assert [r.get("Id") for r in rels] == ["rId2"]
        assert all("printerSettings" not in (r.get("Target") or "") for r in rels)

    def test_bin_content_type_removed(self):
        from lxml import etree

        data = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{CT}">
  <Default Extension="bin" ContentType="application/vnd.printerSettings"/>
  <Default Extension="xml" ContentType="application/xml"/>
</Types>""".encode()
        out = pg._strip_bin_content_type(data)
        defaults = etree.fromstring(out).findall(f"{{{CT}}}Default")
        assert [d.get("Extension") for d in defaults] == ["xml"]

    def test_bin_content_type_absent_is_noop(self):
        data = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="{CT}"><Default Extension="xml" ContentType="application/xml"/></Types>""".encode()
        out = pg._strip_bin_content_type(data)
        assert b'Extension="xml"' in out
        assert b'Extension="bin"' not in out


# ══════════════════════════════════════════════════════════════════════
# 7. Rendering e2e (real .pptx files in tmp dirs)
# ══════════════════════════════════════════════════════════════════════


class TestRenderDeck:
    @pytest.mark.parametrize("theme_slug", pg.AVAILABLE_TEMPLATES)
    def test_each_builtin_theme_renders_valid_deck(self, tmp_path, theme_slug):
        from pptx import Presentation

        slides = pg._parse_slides(SAMPLE_MD)
        out = tmp_path / f"{theme_slug}.pptx"
        pg._build_pptx(slides, pg.BUILTIN_THEMES[theme_slug], "Topic", out)

        assert out.exists()
        reopened = Presentation(str(out))
        assert len(reopened.slides) == len(slides)
        assert reopened.slide_width == pg._SLIDE_W
        assert reopened.slide_height == pg._SLIDE_H
        # Notes survive the round trip.
        assert reopened.slides[0].notes_slide.notes_text_frame.text == (
            "Opening notes here."
        )
        # Post-processing stripped the Windows printerSettings part.
        names = zipfile.ZipFile(out).namelist()
        assert not any("printerSettings" in n for n in names)
        # app.xml was resynced with the real slide count.
        app_xml = zipfile.ZipFile(out).read("docProps/app.xml").decode()
        assert f"<Slides>{len(slides)}</Slides>" in app_xml

    def test_build_pptx_survives_postprocess_failure(self, tmp_path):
        slides = pg._parse_slides("# Title\n- a")
        out = tmp_path / "deck.pptx"
        with patch.object(pg, "_post_process_pptx", side_effect=OSError("zip busy")):
            pg._build_pptx(slides, pg.BUILTIN_THEMES["modern"], "Topic", out)
        assert out.exists()

        from pptx import Presentation

        assert len(Presentation(str(out)).slides) == len(slides)

    def test_render_deck_with_section_and_closing_slides(self, tmp_path):
        from pptx import Presentation

        slides = pg._parse_slides(SAMPLE_MD)
        out = tmp_path / "structure.pptx"
        pg._render_deck(slides, pg.BUILTIN_THEMES["elegant"], "Structure", out)

        reopened = Presentation(str(out))
        assert len(reopened.slides) == 4
        # Every slide got a background fill and at least one shape.
        for slide in reopened.slides:
            assert len(slide.shapes) >= 1

    def test_content_slide_without_bullets_renders(self, tmp_path):
        """A title-only content slide (no bullets, no notes) still renders."""
        from pptx import Presentation

        slides = pg._parse_slides("# Deck\n\n---\n\n# Only a title")
        assert slides[1].bullets == []
        out = tmp_path / "titleonly.pptx"
        pg._build_pptx(slides, pg.BUILTIN_THEMES["modern"], "Topic", out)
        assert len(Presentation(str(out)).slides) == 2

    def test_build_pptx_warns_on_slide_count_mismatch(self, tmp_path):
        slides = pg._parse_slides("# A\n- x\n\n---\n\n# B\n- y")
        out = tmp_path / "mismatch.pptx"
        real_render = pg._render_deck

        def render_only_first(slides_, theme, topic, output_path):
            real_render(slides_[:1], theme, topic, output_path)

        with patch.object(pg, "_render_deck", side_effect=render_only_first):
            pg._build_pptx(slides, pg.BUILTIN_THEMES["modern"], "Topic", out)
        # The file exists but holds fewer slides than expected (logged).
        assert out.exists()

    def test_build_pptx_warns_when_reopen_fails(self, tmp_path):
        slides = pg._parse_slides("# A")
        out = tmp_path / "bad.pptx"
        with patch.object(
            pg, "_render_deck", lambda *args: out.write_bytes(b"garbage")
        ):
            pg._build_pptx(slides, pg.BUILTIN_THEMES["modern"], "Topic", out)
        # Non-fatal: the (garbage) file still exists after the warning.
        assert out.exists()


# ══════════════════════════════════════════════════════════════════════
# 8. LLM flow (mocked providers)
# ══════════════════════════════════════════════════════════════════════


class TestGenerateSlideMarkdown:
    @pytest.mark.asyncio
    async def test_happy_path(self):
        chat = AsyncMock(
            return_value={"message": {"role": "assistant", "content": "# T\n- a"}}
        )
        with (
            patch(
                "app.services.pptx_gen.model_prefs.resolve_task_model",
                AsyncMock(return_value="test-model"),
            ),
            patch("app.services.pptx_gen.providers.chat_once", chat),
        ):
            content = await pg._generate_slide_markdown("My Topic", None)

        assert content == "# T\n- a"
        chat.assert_awaited_once()
        args, kwargs = chat.await_args
        assert args[0] == "test-model"
        assert args[1][0]["role"] == "system"
        assert "Topic: My Topic" in args[1][1]["content"]
        assert "Generate the slide deck now." in args[1][1]["content"]
        assert kwargs["think"] is False

    @pytest.mark.asyncio
    async def test_outline_included_in_user_content(self):
        chat = AsyncMock(return_value={"message": {"content": "# T"}})
        with (
            patch(
                "app.services.pptx_gen.model_prefs.resolve_task_model",
                AsyncMock(return_value="m"),
            ),
            patch("app.services.pptx_gen.providers.chat_once", chat),
        ):
            await pg._generate_slide_markdown("T", "Intro; Close")
        user_content = chat.await_args.args[1][1]["content"]
        assert "Suggested outline" in user_content
        assert "Intro; Close" in user_content

    @pytest.mark.asyncio
    async def test_explicit_model_skips_task_resolution(self):
        resolve = AsyncMock(return_value="should-not-be-used")
        with (
            patch("app.services.pptx_gen.model_prefs.resolve_task_model", resolve),
            patch(
                "app.services.pptx_gen.providers.chat_once",
                AsyncMock(return_value={"message": {"content": "# T"}}),
            ),
        ):
            content = await pg._generate_slide_markdown("T", None, model="explicit")
        assert content == "# T"
        resolve.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_fenced_markdown_unwrapped(self):
        fenced = "```markdown\n# Title\n- bullet\n```"
        with (
            patch(
                "app.services.pptx_gen.model_prefs.resolve_task_model",
                AsyncMock(return_value="m"),
            ),
            patch(
                "app.services.pptx_gen.providers.chat_once",
                AsyncMock(return_value={"message": {"content": fenced}}),
            ),
        ):
            content = await pg._generate_slide_markdown("T", None)
        assert content == "# Title\n- bullet"

    @pytest.mark.asyncio
    async def test_empty_content_raises(self):
        with (
            patch(
                "app.services.pptx_gen.model_prefs.resolve_task_model",
                AsyncMock(return_value="m"),
            ),
            patch(
                "app.services.pptx_gen.providers.chat_once",
                AsyncMock(return_value={"message": {"content": "   "}}),
            ),
        ):
            with pytest.raises(RuntimeError, match="empty presentation content"):
                await pg._generate_slide_markdown("T", None)

    @pytest.mark.asyncio
    async def test_no_model_configured_raises(self):
        with patch(
            "app.services.pptx_gen.model_prefs.resolve_task_model",
            AsyncMock(return_value=""),
        ):
            with pytest.raises(RuntimeError, match="No LLM model configured"):
                await pg._generate_slide_markdown("T", None)


# ══════════════════════════════════════════════════════════════════════
# 9. Public API — generate_presentation
# ══════════════════════════════════════════════════════════════════════


class TestGeneratePresentation:
    @pytest.mark.asyncio
    async def test_happy_path_contract(self, reports_dir, no_libreoffice):
        markdown_mock = AsyncMock(return_value=SAMPLE_MD)
        with patch.object(pg, "_generate_slide_markdown", markdown_mock):
            result = await pg.generate_presentation(
                "My Great Topic!", outline="A; B", template="modern"
            )

        markdown_mock.assert_awaited_once_with("My Great Topic!", "A; B", model=None)
        assert result["type"] == "presentation"
        assert result["format"] == "pptx"
        assert result["template"] == "modern"
        assert result["slide_count"] == 4
        assert result["filename"] == "My_Great_Topic.pptx"
        assert result["file_path"] == f"reports/{result['report_id']}.pptx"
        assert result["download_url"] == f"/api/reports/{result['report_id']}/download"
        assert result["thumbnail_url"] == (
            f"/api/reports/{result['report_id']}/thumbnail"
        )
        assert result["created_at"] > 0

        saved = reports_dir / f"{result['report_id']}.pptx"
        assert saved.exists() and saved.stat().st_size > 0

    @pytest.mark.asyncio
    async def test_default_template_when_none_requested(
        self, reports_dir, no_libreoffice
    ):
        with patch.object(
            pg, "_generate_slide_markdown", AsyncMock(return_value="# T\n- a")
        ):
            result = await pg.generate_presentation("Topic")
        assert result["template"] == pg.DEFAULT_TEMPLATE
        assert result["slide_count"] == 1

    @pytest.mark.asyncio
    async def test_unknown_template_falls_back(self, reports_dir, no_libreoffice):
        with patch.object(
            pg, "_generate_slide_markdown", AsyncMock(return_value="# T\n- a")
        ):
            result = await pg.generate_presentation("Topic", template="no-such-theme")
        assert result["template"] == pg.DEFAULT_TEMPLATE

    @pytest.mark.asyncio
    async def test_max_slides_cap(self, reports_dir, no_libreoffice):
        with patch.object(
            pg, "_generate_slide_markdown", AsyncMock(return_value=SAMPLE_MD)
        ):
            result = await pg.generate_presentation("Topic", max_slides=2)
        assert result["slide_count"] == 2

    @pytest.mark.asyncio
    async def test_falsy_max_slides_uses_default_cap(self, reports_dir, no_libreoffice):
        # 0 is falsy → the MAX_SLIDES default cap applies (SAMPLE_MD: 4).
        with patch.object(
            pg, "_generate_slide_markdown", AsyncMock(return_value=SAMPLE_MD)
        ):
            result = await pg.generate_presentation("Topic", max_slides=0)
        assert result["slide_count"] == 4

    @pytest.mark.asyncio
    async def test_no_slides_parsed_raises(self, reports_dir, no_libreoffice):
        with patch.object(
            pg, "_generate_slide_markdown", AsyncMock(return_value="")
        ):
            with pytest.raises(RuntimeError, match="No slides parsed"):
                await pg.generate_presentation("Topic")

    @pytest.mark.asyncio
    async def test_llm_failure_propagates(self, reports_dir, no_libreoffice):
        with patch.object(
            pg,
            "_generate_slide_markdown",
            AsyncMock(side_effect=RuntimeError("LLM down")),
        ):
            with pytest.raises(RuntimeError, match="LLM down"):
                await pg.generate_presentation("Topic")

    @pytest.mark.asyncio
    async def test_model_forwarded_to_markdown_step(self, reports_dir, no_libreoffice):
        markdown_mock = AsyncMock(return_value="# T\n- a")
        with patch.object(pg, "_generate_slide_markdown", markdown_mock):
            await pg.generate_presentation("Topic", model="override-model")
        assert markdown_mock.await_args.kwargs["model"] == "override-model"

    @pytest.mark.asyncio
    async def test_thumbnail_generated_when_libreoffice_available(self, reports_dir):
        thumbnail = AsyncMock(return_value=True)
        with (
            patch.object(
                pg, "_generate_slide_markdown", AsyncMock(return_value="# T\n- a")
            ),
            patch(
                "app.services.integrations.libreoffice.is_available",
                return_value=True,
            ),
            patch(
                "app.services.integrations.libreoffice.generate_and_cache_thumbnail",
                thumbnail,
            ),
        ):
            result = await pg.generate_presentation("Topic")

        thumbnail.assert_awaited_once()
        args, kwargs = thumbnail.await_args
        assert args[0].name == f"{result['report_id']}.pptx"
        assert args[1].name == f"{result['report_id']}_thumb.jpg"
        assert kwargs["max_width"] == 400

    @pytest.mark.asyncio
    async def test_thumbnail_failure_is_non_fatal(self, reports_dir):
        with (
            patch.object(
                pg, "_generate_slide_markdown", AsyncMock(return_value="# T\n- a")
            ),
            patch(
                "app.services.integrations.libreoffice.is_available",
                return_value=True,
            ),
            patch(
                "app.services.integrations.libreoffice.generate_and_cache_thumbnail",
                AsyncMock(side_effect=RuntimeError("soffice crashed")),
            ),
        ):
            result = await pg.generate_presentation("Topic")
        assert result["slide_count"] == 1
        assert (reports_dir / f"{result['report_id']}.pptx").exists()

    @pytest.mark.asyncio
    async def test_thumbnail_cached_false_retries_on_demand(self, reports_dir):
        """A thumbnail that returns False logs a retry-later and moves on."""
        with (
            patch.object(
                pg, "_generate_slide_markdown", AsyncMock(return_value="# T\n- a")
            ),
            patch(
                "app.services.integrations.libreoffice.is_available",
                return_value=True,
            ),
            patch(
                "app.services.integrations.libreoffice.generate_and_cache_thumbnail",
                AsyncMock(return_value=False),
            ),
        ):
            result = await pg.generate_presentation("Topic")
        assert result["slide_count"] == 1

    @pytest.mark.asyncio
    async def test_file_not_created_raises(self, reports_dir, no_libreoffice):
        with (
            patch.object(
                pg, "_generate_slide_markdown", AsyncMock(return_value="# T\n- a")
            ),
            patch.object(pg, "_build_pptx", lambda *args, **kwargs: None),
        ):
            with pytest.raises(RuntimeError, match="PPTX file was not created"):
                await pg.generate_presentation("Topic")


# ══════════════════════════════════════════════════════════════════════
# 10. DB template helpers
# ══════════════════════════════════════════════════════════════════════


class TestDbTemplateHelpers:
    @pytest.mark.asyncio
    async def test_slugs_from_db(self):
        with _db_patch(rows=[("corp",), ("brand-x",)]):
            assert await pg._get_available_templates_from_db() == ["corp", "brand-x"]

    @pytest.mark.asyncio
    async def test_empty_db_falls_back_to_builtins(self):
        with _db_patch(rows=[]):
            assert await pg._get_available_templates_from_db() == pg.AVAILABLE_TEMPLATES

    @pytest.mark.asyncio
    async def test_db_error_falls_back_to_builtins(self):
        with _db_patch(error=RuntimeError("no database")):
            assert await pg._get_available_templates_from_db() == pg.AVAILABLE_TEMPLATES

    @pytest.mark.asyncio
    async def test_descriptions_from_db(self):
        rows = [("corp", "Navy blue"), ("modern", None)]
        with _db_patch(rows=rows):
            result = await pg._get_templates_with_descriptions()
        assert result == [("corp", "Navy blue"), ("modern", "")]

    @pytest.mark.asyncio
    async def test_empty_descriptions_fall_back_to_catalog(self):
        with _db_patch(rows=[]):
            result = await pg._get_templates_with_descriptions()
        assert ("corporate", "Corporate — Navy blue professional theme") in result

    @pytest.mark.asyncio
    async def test_descriptions_db_error_falls_back(self):
        with _db_patch(error=RuntimeError("down")):
            result = await pg._get_templates_with_descriptions()
        assert [slug for slug, _ in result] == pg.AVAILABLE_TEMPLATES


# ── misc ────────────────────────────────────────────────────────────


class TestLogHelper:
    def test_log_formats_args(self, capsys):
        pg._log("hello %s", "world")
        assert "[pptx_gen] hello world" in capsys.readouterr().out

    def test_log_survives_bad_format(self, capsys):
        pg._log("value=%d", "NaN")
        assert "NaN" in capsys.readouterr().out


def _fake_reports_path(fail_on):
    """Build a Path double for _get_reports_dir() tests.

    ``fail_on`` maps a path value to the set of 1-based mkdir attempt
    numbers that raise OSError. Call counting is shared per path VALUE
    (not per object) so the function's fallback — a NEW
    Path("/app/data/reports") instance — observes attempt #2 for that
    path, modelling a transient first-attempt failure. No real
    directory is ever created.
    """
    calls: dict = {}

    class _PathDouble:
        def __init__(self, value):
            self.value = str(value)

        def resolve(self):
            return self

        @property
        def parent(self):
            return _PathDouble(str(PurePosixPath(self.value).parent))

        def __truediv__(self, other):
            return _PathDouble(str(PurePosixPath(self.value) / str(other)))

        def mkdir(self, parents=True, exist_ok=True):
            attempt = calls.get(self.value, 0) + 1
            calls[self.value] = attempt
            if attempt in fail_on.get(self.value, set()):
                raise OSError(f"simulated mkdir failure for {self.value}")
            return None

        def __eq__(self, other):
            return isinstance(other, _PathDouble) and self.value == other.value

        def __ne__(self, other):
            return not self.__eq__(other)

        def __hash__(self):
            return hash(self.value)

        def __repr__(self):
            return f"FakePath({self.value!r})"

    return _PathDouble, calls


class TestGetReportsDir:
    """The real _get_reports_dir (no monkeypatching of the function) via a
    Path double: calling it for real would either hit the unwritable /app
    candidate and create <backend>/data/reports inside the checkout, or
    (as root) create /app/data/reports — environment-dependent side
    effects the tests must not have."""

    APP = "/app/data/reports"
    REPO = str(BACKEND_ROOT / "data" / "reports")

    def test_first_writable_candidate_wins(self, monkeypatch):
        cls, calls = _fake_reports_path({})
        monkeypatch.setattr(pg, "Path", cls)

        result = pg._get_reports_dir()

        assert result == cls(self.APP)
        # Early return: exactly one mkdir, on the first candidate only.
        assert calls == {self.APP: 1}

    def test_unwritable_app_dir_falls_back_to_repo_data(self, monkeypatch):
        # /app/data/reports mkdir raises PermissionError-class OSError
        # (the situation on this machine) → the repo-relative candidate.
        cls, calls = _fake_reports_path({self.APP: {1}})
        monkeypatch.setattr(pg, "Path", cls)

        result = pg._get_reports_dir()

        assert result == cls(self.REPO)
        assert calls == {self.APP: 1, self.REPO: 1}

    def test_transient_failures_retry_the_app_fallback(self, monkeypatch):
        # Both candidates fail their first mkdir; the final fallback is a
        # NEW Path("/app/data/reports") whose mkdir (attempt #2 for that
        # path value) succeeds → the /app path is returned.
        cls, calls = _fake_reports_path({self.APP: {1}, self.REPO: {1}})
        monkeypatch.setattr(pg, "Path", cls)

        result = pg._get_reports_dir()

        assert result == cls(self.APP)
        assert calls == {self.APP: 2, self.REPO: 1}

    def test_persistent_failures_raise_out_of_the_helper(self, monkeypatch):
        # Every mkdir (including the fallback retry) fails → the OSError
        # propagates rather than returning a bogus path.
        cls, calls = _fake_reports_path({self.APP: {1, 2}, self.REPO: {1}})
        monkeypatch.setattr(pg, "Path", cls)

        with pytest.raises(OSError, match="simulated mkdir failure"):
            pg._get_reports_dir()
        assert calls == {self.APP: 2, self.REPO: 1}


def test_uuid_result_ids_are_unique(reports_dir, no_libreoffice):
    """Two generations produce distinct report ids (sanity for the flow)."""
    import asyncio

    async def run():
        with patch.object(
            pg, "_generate_slide_markdown", AsyncMock(return_value="# T\n- a")
        ):
            one = await pg.generate_presentation("Topic")
            two = await pg.generate_presentation("Topic")
        return one, two

    one, two = asyncio.run(run())
    assert one["report_id"] != two["report_id"]
    assert uuid.UUID(one["report_id"])
    assert uuid.UUID(two["report_id"])
