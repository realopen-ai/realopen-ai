"""
Schematic thumbnail generator for PPTX templates.

Renders a simplified visual preview of a PPTX template using Pillow,
based on the layout's background color, decorative shapes (rectangles,
triangles, ellipses), and their positions/fills/rotations.

The thumbnail is rendered at the slide's aspect ratio (16:9) and shows
Layout 0 (Title Slide) as the representative view, since it has the
most visual character (dark background, accent triangles, etc.).

The template name is overlaid as text at the bottom.
"""

from __future__ import annotations

import base64
import io
import logging
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[thumbnail_gen] {formatted}", flush=True)


# ── Color helpers ────────────────────────────────────────────────────


def _hex_to_rgb(hex_str: str) -> Tuple[int, int, int]:
    """Convert a hex color string (#RRGGBB or RRGGBB) to an (R, G, B) tuple."""
    h = hex_str.lstrip("#")
    if len(h) == 6:
        return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))
    return (128, 128, 128)  # gray fallback


def _emu_to_px(emu: int, scale: float) -> int:
    """Convert EMU (English Metric Units) to pixels at the given scale."""
    if emu is None:
        return 0
    return int(emu * scale)


# ── Shape extraction from PPTX layout ────────────────────────────────


def _extract_layout_visuals(prs, layout_index: int = 0) -> dict:
    """Extract visual elements from a slide layout for thumbnail rendering.

    Returns a dict with:
        bg_color: hex string or None
        slide_w, slide_h: EMU dimensions
        shapes: list of {prst, left, top, width, height, fill_color, rotation}
    """
    from pptx.oxml.ns import qn

    if layout_index >= len(prs.slide_layouts):
        layout_index = 0

    layout = prs.slide_layouts[layout_index]
    result = {
        "bg_color": None,
        "slide_w": prs.slide_width,
        "slide_h": prs.slide_height,
        "shapes": [],
    }

    # Extract background color
    cSld = layout.element.find(qn("p:cSld"))
    if cSld is not None:
        bg = cSld.find(qn("p:bg"))
        if bg is not None:
            bgPr = bg.find(qn("p:bgPr"))
            if bgPr is not None:
                solidFill = bgPr.find(qn("a:solidFill"))
                if solidFill is not None:
                    srgb = solidFill.find(qn("a:srgbClr"))
                    if srgb is not None:
                        result["bg_color"] = srgb.get("val")

    # Extract decorative shapes (non-placeholder)
    for shape in layout.shapes:
        if shape.is_placeholder:
            continue

        shape_info = {
            "prst": "rect",
            "left": shape.left or 0,
            "top": shape.top or 0,
            "width": shape.width or 0,
            "height": shape.height or 0,
            "fill_color": None,
            "rotation": 0,
        }

        try:
            spPr = shape._element.find(qn("p:spPr"))
            if spPr is not None:
                # Get preset geometry
                geom = spPr.find(qn("a:prstGeom"))
                if geom is not None:
                    shape_info["prst"] = geom.get("prst", "rect")

                # Get rotation
                xfrm = spPr.find(qn("a:xfrm"))
                if xfrm is not None:
                    rot_val = xfrm.get("rot")
                    if rot_val:
                        shape_info["rotation"] = int(rot_val) / 60000  # to degrees

                # Get fill color
                solidFill = spPr.find(qn("a:solidFill"))
                if solidFill is not None:
                    srgb = solidFill.find(qn("a:srgbClr"))
                    if srgb is not None:
                        shape_info["fill_color"] = srgb.get("val")
        except Exception as e:
            _log("error reading shape: %s", e)

        result["shapes"].append(shape_info)

    return result


# ── Shape rendering ──────────────────────────────────────────────────


def _draw_rectangle(draw: ImageDraw.ImageDraw, shape: dict, scale: float):
    """Draw a rectangle shape on the thumbnail."""
    left = _emu_to_px(shape["left"], scale)
    top = _emu_to_px(shape["top"], scale)
    w = _emu_to_px(shape["width"], scale)
    h = _emu_to_px(shape["height"], scale)

    fill = _hex_to_rgb(shape["fill_color"]) if shape["fill_color"] else None
    if fill:
        draw.rectangle([left, top, left + w, top + h], fill=fill)


def _draw_right_triangle(
    draw: ImageDraw.ImageDraw, shape: dict, scale: float, img_w: int, img_h: int
):
    """Draw a right triangle shape on the thumbnail.

    python-pptx's rtTriangle preset is a right triangle with the right
    angle at the bottom-left corner. The rotation is applied around the
    center of the shape's bounding box.

    We handle the common rotations used in our templates:
        0°   → right angle at bottom-left
        180° → right angle at top-right (pointing down-left)
        270° → right angle at top-left (pointing right)
    """
    left = _emu_to_px(shape["left"], scale)
    top = _emu_to_px(shape["top"], scale)
    w = max(1, _emu_to_px(shape["width"], scale))
    h = max(1, _emu_to_px(shape["height"], scale))
    rot = shape.get("rotation", 0)

    fill = _hex_to_rgb(shape["fill_color"]) if shape["fill_color"] else None
    if not fill:
        return

    # Base triangle (right angle at bottom-left):
    #   (left, top+h) — (left+w, top+h) — (left, top)
    # This is the default rtTriangle orientation in OOXML.
    p1 = (left, top + h)  # bottom-left (right angle)
    p2 = (left + w, top + h)  # bottom-right
    p3 = (left, top)  # top-left

    if abs(rot - 180) < 5:
        # 180° rotation: right angle at top-right
        p1 = (left + w, top)  # top-right (right angle)
        p2 = (left, top)  # top-left
        p3 = (left + w, top + h)  # bottom-right
    elif abs(rot - 270) < 5:
        # 270° rotation: right angle at top-left, hypotenuse goes from
        # bottom-left to top-right
        p1 = (left, top)  # top-left (right angle)
        p2 = (left + w, top)  # top-right
        p3 = (left, top + h)  # bottom-left
    elif abs(rot - 90) < 5:
        # 90° rotation: right angle at bottom-right
        p1 = (left + w, top + h)  # bottom-right (right angle)
        p2 = (left, top + h)  # bottom-left
        p3 = (left + w, top)  # top-right

    draw.polygon([p1, p2, p3], fill=fill)


def _draw_ellipse(draw: ImageDraw.ImageDraw, shape: dict, scale: float):
    """Draw an ellipse shape on the thumbnail."""
    left = _emu_to_px(shape["left"], scale)
    top = _emu_to_px(shape["top"], scale)
    w = _emu_to_px(shape["width"], scale)
    h = _emu_to_px(shape["height"], scale)

    fill = _hex_to_rgb(shape["fill_color"]) if shape["fill_color"] else None
    if fill:
        draw.ellipse([left, top, left + w, top + h], fill=fill)


def _draw_shape(
    draw: ImageDraw.ImageDraw, shape: dict, scale: float, img_w: int, img_h: int
):
    """Dispatch shape rendering based on the preset geometry type."""
    prst = shape.get("prst", "rect")
    if prst == "rect":
        _draw_rectangle(draw, shape, scale)
    elif prst == "rtTriangle":
        _draw_right_triangle(draw, shape, scale, img_w, img_h)
    elif prst == "ellipse":
        _draw_ellipse(draw, shape, scale)
    else:
        # Unknown shape type — render as rectangle fallback
        _draw_rectangle(draw, shape, scale)


# ── Text overlay ─────────────────────────────────────────────────────


def _get_font(size: int = 14) -> ImageFont.FreeTypeFont:
    """Get a font for the thumbnail text overlay. Falls back to default."""
    font_paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/System/Library/Fonts/Helvetica.ttc",
    ]
    for path in font_paths:
        try:
            return ImageFont.truetype(path, size)
        except (IOError, OSError):
            continue
    return ImageFont.load_default()


def _draw_text_overlay(
    img: Image.Image,
    text: str,
    bg_color: Optional[str],
):
    """Draw the template name as a semi-transparent text overlay at the bottom."""
    draw = ImageDraw.Draw(img, "RGBA")
    img_w, img_h = img.size

    # Determine text color based on background brightness
    if bg_color:
        r, g, b = _hex_to_rgb(bg_color)
        brightness = (r * 299 + g * 587 + b * 114) / 1000
        text_color = (255, 255, 255, 230) if brightness < 128 else (0, 0, 0, 200)
    else:
        text_color = (0, 0, 0, 200)

    # Truncate text if too long (max ~25 chars)
    max_chars = 25
    display_text = text[:max_chars] + "…" if len(text) > max_chars else text
    if not display_text.strip():
        display_text = "Lorem ipsum"

    font = _get_font(16)

    # Measure text
    try:
        bbox = draw.textbbox((0, 0), display_text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    except Exception:
        text_w, text_h = len(display_text) * 8, 16

    # Position: bottom-center with padding
    padding = 8
    x = (img_w - text_w) // 2
    y = img_h - text_h - padding - 4

    # Draw a semi-transparent background bar behind the text for readability
    bar_padding = 4
    bar_x0 = x - bar_padding
    bar_y0 = y - bar_padding
    bar_x1 = x + text_w + bar_padding
    bar_y1 = y + text_h + bar_padding

    # Use inverse color for the bar
    bar_color = (
        (0, 0, 0, 120)
        if brightness >= 128
        else (255, 255, 255, 120) if bg_color else (255, 255, 255, 120)
    )
    draw.rectangle([bar_x0, bar_y0, bar_x1, bar_y1], fill=bar_color)

    # Draw the text
    draw.text((x, y), display_text, fill=text_color, font=font)

    return img


# ── Main entry point ─────────────────────────────────────────────────


def generate_schematic_thumbnail(
    pptx_path: Path,
    display_name: str,
    width: int = 400,
    height: int = 225,
) -> Optional[str]:
    """Generate a schematic thumbnail for a PPTX template.

    Renders Layout 0 (Title Slide) as the representative view, including:
    - Background color
    - All decorative shapes (rectangles, triangles, ellipses) with their
      correct positions, sizes, fill colors, and rotations
    - Template name overlaid as text at the bottom

    Args:
        pptx_path: Path to the .pptx file
        display_name: Template display name for the text overlay
        width: Thumbnail width in pixels (default 400)
        height: Thumbnail height in pixels (default 225, 16:9)

    Returns:
        Base64-encoded JPEG string, or None on failure.
    """
    try:
        from pptx import Presentation
    except ImportError:
        _log("python-pptx not installed")
        return None

    try:
        prs = Presentation(str(pptx_path))
    except Exception as e:
        _log("failed to open PPTX: %s", e)
        return None

    # Extract visual elements from Layout 0 (Title Slide)
    visuals = _extract_layout_visuals(prs, layout_index=0)

    slide_w_emu = visuals["slide_w"]
    slide_h_emu = visuals["slide_h"]
    if slide_w_emu == 0 or slide_h_emu == 0:
        _log("invalid slide dimensions")
        return None

    # Scale factor: EMU → pixels
    scale_x = width / slide_w_emu
    scale_y = height / slide_h_emu
    scale = min(scale_x, scale_y)

    # Recalculate actual dimensions to maintain aspect ratio
    actual_w = int(slide_w_emu * scale)
    actual_h = int(slide_h_emu * scale)

    # Create image
    bg_color = (
        _hex_to_rgb(visuals["bg_color"]) if visuals["bg_color"] else (255, 255, 255)
    )
    img = Image.new("RGB", (actual_w, actual_h), bg_color)
    draw = ImageDraw.Draw(img, "RGBA")

    # Draw shapes in order (they're stored back-to-front in the XML)
    for shape in visuals["shapes"]:
        _draw_shape(draw, shape, scale, actual_w, actual_h)

    # Draw text overlay
    _draw_text_overlay(img, display_name, visuals["bg_color"])

    # Convert to base64 JPEG
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    _log(
        "thumbnail generated: %s (%dx%d, %d bytes base64)",
        display_name,
        actual_w,
        actual_h,
        len(b64),
    )
    return b64


# ── Auto-generate thumbnails for all templates ───────────────────────


async def auto_generate_thumbnail_for_template(
    template_path: Path,
    display_name: str,
) -> Optional[str]:
    """Generate a thumbnail for a template file.

    This is the async wrapper that can be called from the workspace API
    when a template is created or updated without a user-uploaded thumbnail.
    """
    import asyncio

    return await asyncio.to_thread(
        generate_schematic_thumbnail, template_path, display_name
    )
