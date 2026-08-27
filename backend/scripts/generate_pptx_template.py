#!/usr/bin/env python3
"""
generate_pptx_template.py — create a new .pptx template for the PPTX
generation service.

The 3 shipped templates (corporate, modern, elegant) are "pure layout"
templates: 0 slides, 11 standard slide layouts, 1 slide master. This
script builds a fresh template of the same shape so you can drop in your
own color scheme and immediately use it with `use_pptx_gen`.

Usage:
    python3 scripts/generate_pptx_template.py \
        --name startup \
        --accent 2D7DD6 \
        --bg 0F1B2D \
        --fg FFFFFF \
        --out app/templates/pptx

The generated template:
  - 16:9 widescreen (13.33" x 7.5"), matching the shipped templates
  - 0 slides (the builder adds slides at runtime via add_slide)
  - 11 standard layouts (Title Slide, Title and Content, Section Header,
    Two Content, Comparison, Title Only, Blank, Content with Caption,
    Picture with Caption, Title and Vertical Text, Vertical Title and
    Text). The two vertical layouts are kept for stock-PowerPoint
    compatibility — the PPTX generator's _layout_has_vertical_text()
    guard skips them at build time, so they never produce rotated text.
  - Theme colors applied to the slide master background + every layout's
    title/body placeholders + the theme1.xml accent color.

Requires: python-pptx (already a project dependency).
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.oxml.ns import qn
from pptx.util import Emu, Pt
from lxml import etree

# 16:9 widescreen slide size, matching the 3 shipped templates.
WIDESCREEN_W = Emu(12192000)  # 13.333 inches
WIDESCREEN_H = Emu(6858000)  # 7.5    inches

# Standard layout set shipped by PowerPoint — same names + order as the
# 3 existing templates so this file is a drop-in addition.
LAYOUT_ORDER = [
    "Title Slide",
    "Title and Content",
    "Section Header",
    "Two Content",
    "Comparison",
    "Title Only",
    "Blank",
    "Content with Caption",
    "Picture with Caption",
    "Title and Vertical Text",
    "Vertical Title and Text",
]


def _hex_to_rgb(hex_str: str) -> RGBColor:
    """Parse a #RRGGBB or RRGGBB string into an RGBColor."""
    s = hex_str.lstrip("#").strip()
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", s):
        raise ValueError(f"invalid hex color: {hex_str!r} (expected RRGGBB)")
    return RGBColor(int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))


def _set_solid_fill(element, rgb: RGBColor) -> None:
    """Set a <a:solidFill><a:srgbClr val="RRGGBB"/></a:solidFill> child
    on `element`, replacing any existing fill."""
    # Remove existing fill children (solidFill, gradFill, etc.)
    for tag in (
        "a:solidFill",
        "a:gradFill",
        "a:noFill",
        "a:blipFill",
        "a:pattFill",
        "a:grpFill",
    ):
        for child in element.findall(qn(tag)):
            element.remove(child)
    fill = etree.SubElement(element, qn("a:solidFill"))
    clr = etree.SubElement(fill, qn("a:srgbClr"))
    clr.set("val", f"{rgb}")


def _apply_background(slide_or_layout, bg_color: RGBColor) -> None:
    """Set the <p:bg><p:bgPr><a:solidFill> on a slide/layout/master.

    This sets the background fill of the part itself — the most reliable
    way to theme a template uniformly across all 11 layouts.
    """
    cSld = slide_or_layout._element.find(qn("p:cSld"))
    if cSld is None:
        return
    # Remove any existing <p:bg>
    for bg in cSld.findall(qn("p:bg")):
        cSld.remove(bg)
    bg = etree.SubElement(cSld, qn("p:bg"))
    bgPr = etree.SubElement(bg, qn("p:bgPr"))
    _set_solid_fill(bgPr, bg_color)
    # <p:bgPr> requires an <a:effectLst/> child per the schema
    etree.SubElement(bgPr, qn("a:effectLst"))
    # Move <p:bg> to be the FIRST child of <p:cSld> (schema order)
    cSld.insert(0, bg)


def _style_placeholder_text(
    ph, color: RGBColor, size_pt: int, bold: bool = False
) -> None:
    """Set font color/size/bold on every run in every paragraph of a
    placeholder's text frame. We touch the runs directly so the color
    sticks even when the layout's defRPr would otherwise override it."""
    if not ph.has_text_frame:
        return
    tf = ph.text_frame
    for para in tf.paragraphs:
        # Set the paragraph's default run properties so new runs inherit
        for run in para.runs:
            run.font.color.rgb = color
            run.font.size = Pt(size_pt)
            run.font.bold = bold
        # Also set the defRPr on the paragraph properties (pPr/defRPr)
        # so PowerPoint picks up the color on empty placeholders too.
        pPr = para._pPr
        if pPr is None:
            pPr = etree.SubElement(para._p, qn("a:pPr"))
        # Remove existing defRPr
        for defr in pPr.findall(qn("a:defRPr")):
            pPr.remove(defr)
        defRPr = etree.SubElement(pPr, qn("a:defRPr"))
        defRPr.set("sz", str(size_pt * 100))
        if bold:
            defRPr.set("b", "1")
        solidFill = etree.SubElement(defRPr, qn("a:solidFill"))
        clr = etree.SubElement(solidFill, qn("a:srgbClr"))
        clr.set("val", f"{color}")


def _apply_theme_accent(prs, accent: RGBColor) -> None:
    """Rewrite the theme1.xml accent1 color so charts, shapes, and any
    element that inherits 'accent1' from the theme pick up the new color."""
    # The theme part lives at ppt/theme/theme1.xml — reachable via the
    # slide master's part relationships.
    if not prs.slide_masters:
        return
    master = prs.slide_masters[0]
    theme_part = None
    for rel in master.part.rels.values():
        if rel.reltype.endswith("/theme"):
            theme_part = rel.target_part
            break
    if theme_part is None:
        return
    tree = etree.fromstring(theme_part.blob)
    # The <a:clrScheme> has <a:accent1><a:srgbClr val="..."/>
    A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
    clr_scheme = tree.find(f".//{{{A_NS}}}clrScheme")
    if clr_scheme is None:
        return
    for accent_tag in ("a:accent1", "a:hyperlink"):
        el = clr_scheme.find(
            f"{{{A_NS}}}{accent_tag.split('}')[1] if '}' in accent_tag else accent_tag}"
        )
        if el is not None:
            srgb = el.find(f"{{{A_NS}}}srgbClr")
            if srgb is not None:
                srgb.set("val", f"{accent}")
    # Serialize back into the part
    new_blob = etree.tostring(
        tree, xml_declaration=True, encoding="UTF-8", standalone=True
    )
    theme_part._blob = new_blob


def generate_template(
    name: str,
    accent_hex: str,
    bg_hex: str,
    fg_hex: str,
    title_hex: str | None,
    out_dir: Path,
) -> Path:
    """Build a themed .pptx template and write it to {out_dir}/{name}.pptx."""
    accent = _hex_to_rgb(accent_hex)
    bg = _hex_to_rgb(bg_hex)
    fg = _hex_to_rgb(fg_hex)
    title_color = _hex_to_rgb(title_hex) if title_hex else fg

    # Start from the python-pptx default (11 layouts, 0 slides, 1 master)
    prs = Presentation()

    # Resize to 16:9 widescreen
    prs.slide_width = WIDESCREEN_W
    prs.slide_height = WIDESCREEN_H

    # ── Style the slide master ──
    if prs.slide_masters:
        master = prs.slide_masters[0]
        _apply_background(master, bg)
        # Style every layout
        for layout in prs.slide_layouts:
            _apply_background(layout, bg)
            for ph in layout.placeholders:
                idx = ph.placeholder_format.idx
                ptype = str(ph.placeholder_format.type)
                if idx == 0 or "TITLE" in ptype or "CENTER_TITLE" in ptype:
                    _style_placeholder_text(ph, title_color, 44, bold=True)
                elif idx == 1 or "BODY" in ptype or "SUBTITLE" in ptype:
                    _style_placeholder_text(ph, fg, 20)

    # ── Rewrite the theme accent color ──
    _apply_theme_accent(prs, accent)

    # ── Write ──
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{name}.pptx"
    prs.save(str(out_path))

    # Post-process: strip the Windows printerSettings blob that the
    # python-pptx default template ships, so the new template is as clean
    # as the ones the generator produces. (Keeps the template itself
    # repair-prompt-free if someone opens it directly.)
    _strip_printer_settings_from_template(out_path)

    return out_path


def _strip_printer_settings_from_template(path: Path) -> None:
    """Remove printerSettings*.bin + its rels + content-type Default from
    a freshly-saved template, mirroring what _post_process_pptx does for
    generated decks. Imported lazily so the script works standalone."""
    import shutil
    import zipfile

    PRINTER_REL = (
        "http://schemas.openxmlformats.org/officeDocument/2006/"
        "relationships/printerSettings"
    )
    REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
    CT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"

    tmp = path.with_suffix(path.suffix + ".tmp")
    with zipfile.ZipFile(path, "r") as zin:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zout:
            for item in zin.infolist():
                if "printerSettings" in item.filename:
                    continue
                data = zin.read(item.filename)
                if item.filename == "ppt/_rels/presentation.xml.rels":
                    tree = etree.fromstring(data)
                    for rel in list(tree.findall(f"{{{REL_NS}}}Relationship")):
                        if rel.get("Type") == PRINTER_REL:
                            tree.remove(rel)
                    data = etree.tostring(
                        tree,
                        xml_declaration=True,
                        encoding="UTF-8",
                        standalone=True,
                    )
                elif item.filename == "[Content_Types].xml":
                    tree = etree.fromstring(data)
                    for d in list(tree.findall(f"{{{CT_NS}}}Default")):
                        if d.get("Extension") == "bin":
                            tree.remove(d)
                    data = etree.tostring(
                        tree,
                        xml_declaration=True,
                        encoding="UTF-8",
                        standalone=True,
                    )
                zout.writestr(item, data)
    shutil.move(str(tmp), str(path))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Generate a themed .pptx template for use_pptx_gen.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument(
        "--name",
        required=True,
        help="Template slug (becomes the filename: {name}.pptx). "
        "Must be a filesystem-safe slug.",
    )
    p.add_argument(
        "--accent",
        default="3D8BFD",
        help="Accent color, hex RRGGBB (theme accent1 + hyperlinks).",
    )
    p.add_argument("--bg", default="FFFFFF", help="Slide background color, hex RRGGBB.")
    p.add_argument(
        "--fg", default="333333", help="Default body text color, hex RRGGBB."
    )
    p.add_argument(
        "--title-color",
        default=None,
        help="Title text color, hex RRGGBB (defaults to --fg).",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Output directory (defaults to the shipped-templates "
        "directory: backend/app/templates/pptx).",
    )
    args = p.parse_args(argv)

    # Validate slug
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]*", args.name):
        p.error("--name must be a lowercase slug (letters, digits, -, _)")

    # Validate hex colors early so we fail fast.
    for label, val in [("--accent", args.accent), ("--bg", args.bg), ("--fg", args.fg)]:
        try:
            _hex_to_rgb(val)
        except ValueError as e:
            p.error(f"{label}: {e}")
    if args.title_color:
        try:
            _hex_to_rgb(args.title_color)
        except ValueError as e:
            p.error(f"--title-color: {e}")

    # Default output dir = the shipped-templates folder.
    if args.out:
        out_dir = Path(args.out)
    else:
        here = Path(__file__).resolve().parent
        out_dir = here.parent / "app" / "templates" / "pptx"

    out_path = generate_template(
        name=args.name,
        accent_hex=args.accent,
        bg_hex=args.bg,
        fg_hex=args.fg,
        title_hex=args.title_color,
        out_dir=out_dir,
    )
    print(f"✓ template generated: {out_path}")
    print(
        f"  name={args.name!r} accent={args.accent} bg={args.bg} "
        f"fg={args.fg} title={args.title_color or args.fg}"
    )
    print(
        "  layouts: 11 (Title Slide, Title and Content, Section Header, "
        "Two Content, Comparison, Title Only, Blank, Content with "
        "Caption, Picture with Caption, + 2 vertical)"
    )
    print('  slide size: 16:9 widescreen (13.33" x 7.5")')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
