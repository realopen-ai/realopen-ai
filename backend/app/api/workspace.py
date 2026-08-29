"""
Workspace API — template management + generated files listing.

Endpoints:
  Templates:
    GET    /api/workspace/templates              — list all templates
    GET    /api/workspace/templates/{id}         — get one template
    POST   /api/workspace/templates              — create template (after validation)
    PUT    /api/workspace/templates/{id}         — update template (rename, edit, re-thumbnail)
    DELETE /api/workspace/templates/{id}         — delete template (removes file from disk)
    POST   /api/workspace/templates/validate     — validate a PPTX file (no LLM, python-pptx only)
    POST   /api/workspace/templates/{id}/thumbnail — upload thumbnail for a template

  Generated Files:
    GET    /api/workspace/generated-files        — list all generated files (from messages.deliverables)
"""

import base64
import io
import logging
import re
import uuid
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy import select, delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Template
from app.db.session import get_db
from app.services.pptx_gen import AVAILABLE_TEMPLATES

logger = logging.getLogger(__name__)
router = APIRouter()


def _get_templates_dir() -> Path:
    """Get the custom templates directory."""
    return Path(__file__).resolve().parent.parent / "templates" / "pptx" / "custom"


def _slugify(name: str) -> str:
    """Convert a display name to a slug: lowercase, non-alphanum → underscore."""
    slug = re.sub(r"[^\w\s-]", "", name.lower().strip())
    slug = re.sub(r"[\s-]+", "_", slug)
    return slug or "template"


def _get_data_dir() -> Path:
    candidates = [
        Path("/app/data"),
        Path(__file__).resolve().parent.parent.parent.parent / "data",
    ]
    for d in candidates:
        if d.exists():
            return d
    fallback = Path("/app/data")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


# ─── Request/Response models ─────────────────────────────────────────


class TemplateCreate(BaseModel):
    display_name: str
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    thumbnail: Optional[str] = None  # base64
    slug: Optional[str] = None  # auto-generated from display_name if not provided
    filename: Optional[str] = None  # the uploaded filename (for validation-only flow)


class TemplateUpdate(BaseModel):
    display_name: Optional[str] = None
    description: Optional[str] = None
    tags: Optional[List[str]] = None
    thumbnail: Optional[str] = None


class TemplateValidateRequest(BaseModel):
    filename: Optional[str] = None  # if validating an already-uploaded temp file


class GeneratedFileItem(BaseModel):
    filename: str
    file_type: str  # "pdf", "docx", "pptx", "image"
    tool: str  # "use_report_gen", "use_pptx_gen", "use_image_gen"
    download_url: str
    report_id: Optional[str] = None
    created_at: Optional[int] = None
    conversation_id: Optional[str] = None
    conversation_title: Optional[str] = None
    message_id: Optional[str] = None
    file_size: Optional[int] = None
    original_prompt: Optional[str] = None
    thumbnail_url: Optional[str] = None


# ─── Template serialization ──────────────────────────────────────────


def _template_to_dict(t: Template) -> dict:
    return {
        "id": str(t.id),
        "display_name": t.display_name,
        "slug": t.slug,
        "description": t.description,
        "tags": t.tags or [],
        "thumbnail": t.thumbnail,
        "path": t.path,
        "created_at": int(t.created_at.timestamp() * 1000) if t.created_at else 0,
        "updated_at": int(t.updated_at.timestamp() * 1000) if t.updated_at else 0,
    }


# ─── Template CRUD ───────────────────────────────────────────────────


@router.get("/workspace/templates")
async def list_templates(db: AsyncSession = Depends(get_db)):
    """List all PPTX templates."""
    result = await db.execute(select(Template).order_by(Template.created_at.asc()))
    templates = result.scalars().all()
    return {"templates": [_template_to_dict(t) for t in templates]}


@router.get("/workspace/templates/{template_id}")
async def get_template(template_id: str, db: AsyncSession = Depends(get_db)):
    """Get a single template by ID."""
    try:
        tid = uuid.UUID(template_id)
    except ValueError:
        raise HTTPException(400, "Invalid template ID")
    result = await db.execute(select(Template).where(Template.id == tid))
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Template not found")
    return _template_to_dict(t)


@router.post("/workspace/templates")
async def create_template(
    display_name: str = Form(...),
    description: str = Form(""),
    tags: str = Form(""),  # comma-separated
    thumbnail: Optional[str] = Form(None),  # base64
    file: UploadFile = File(...),  # the .pptx file
    db: AsyncSession = Depends(get_db),
):
    """Create a new template. The PPTX file is saved to the custom templates directory."""
    slug = _slugify(display_name)

    # Check for slug collision
    existing = await db.execute(select(Template).where(Template.slug == slug))
    if existing.scalar_one_or_none():
        raise HTTPException(409, f"Template with slug '{slug}' already exists")

    # Save the file
    templates_dir = _get_templates_dir()
    templates_dir.mkdir(parents=True, exist_ok=True)
    file_path = templates_dir / f"{slug}.pptx"
    content = await file.read()
    file_path.write_bytes(content)

    # Auto-generate a schematic thumbnail if none was uploaded
    final_thumbnail = thumbnail
    print(f"Thumbnail provided: {final_thumbnail}")
    if not final_thumbnail:
        try:
            from app.services.thumbnail_gen import generate_schematic_thumbnail

            print("generating thumbnail...")
            final_thumbnail = generate_schematic_thumbnail(file_path, display_name)
            print(final_thumbnail)
        except Exception as e:
            logger.warning("Auto-thumbnail generation failed: %s", e)
            print("Auto-thumb gen failed:")
            print(e)

    # Parse tags
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

    # Create DB record
    t = Template(
        id=uuid.uuid4(),
        display_name=display_name,
        slug=slug,
        description=description or None,
        tags=tag_list if tag_list else None,
        thumbnail=final_thumbnail,
        path=f"{slug}.pptx",
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(t)
    await db.flush()
    return _template_to_dict(t)


@router.put("/workspace/templates/{template_id}")
async def update_template(
    template_id: str,
    display_name: Optional[str] = Form(None),
    description: Optional[str] = Form(None),
    tags: Optional[str] = Form(None),
    thumbnail: Optional[str] = Form(None),
    db: AsyncSession = Depends(get_db),
):
    """Update a template. If display_name changes, slug + file are renamed."""
    try:
        tid = uuid.UUID(template_id)
    except ValueError:
        raise HTTPException(400, "Invalid template ID")

    result = await db.execute(select(Template).where(Template.id == tid))
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Template not found")

    is_default = t.slug in AVAILABLE_TEMPLATES
    if not is_default:
        templates_dir = _get_templates_dir()

        if display_name and display_name != t.display_name:
            new_slug = _slugify(display_name)
            # Check for slug collision (excluding self)
            collision = await db.execute(
                select(Template).where(Template.slug == new_slug, Template.id != tid)
            )
            if collision.scalar_one_or_none():
                raise HTTPException(409, f"Slug '{new_slug}' already in use")

            # Rename file on disk
            old_path = templates_dir / t.path
            new_path = templates_dir / f"{new_slug}.pptx"
            if old_path.exists():
                old_path.rename(new_path)

            t.display_name = display_name
            t.slug = new_slug
            t.path = f"{new_slug}.pptx"

    if description is not None:
        t.description = description or None
    if tags is not None:
        tag_list = [tg.strip() for tg in tags.split(",") if tg.strip()]
        t.tags = tag_list if tag_list else None
    if thumbnail is not None:
        t.thumbnail = thumbnail

    t.updated_at = datetime.utcnow()
    await db.flush()
    return _template_to_dict(t)


@router.delete("/workspace/templates/{template_id}")
async def delete_template(template_id: str, db: AsyncSession = Depends(get_db)):
    """Delete a template and remove the file from disk."""
    try:
        tid = uuid.UUID(template_id)
    except ValueError:
        raise HTTPException(400, "Invalid template ID")

    result = await db.execute(select(Template).where(Template.id == tid))
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Template not found")

    # Remove file from disk
    is_default = t.slug in AVAILABLE_TEMPLATES
    if not is_default:
        templates_dir = _get_templates_dir()
        file_path = templates_dir / t.path
        if file_path.exists():
            file_path.unlink()

    await db.execute(delete(Template).where(Template.id == tid))
    await db.flush()
    return {"ok": True, "deleted": str(tid)}


# ─── Template Validation ─────────────────────────────────────────────


@router.post("/workspace/templates/validate")
async def validate_template(file: UploadFile = File(...)):
    """Validate a PPTX file using python-pptx (no LLM calls).

    Performs lightweight checks:
    - File is a valid PPTX (can be opened by python-pptx)
    - Has at least 3 slide layouts
    - At least one layout has a title placeholder (idx=0)
    - At least one layout has a content/body placeholder (idx=1)
    - Slide dimensions are 16:9
    - Can add a slide without error

    Many professional templates don't follow the standard
    PowerPoint layout ordering, so we scan ALL layouts instead of
    assuming Layout 0 = Title and Layout 1 = Content.

    Returns {valid: true} on success, {valid: false, error: "..."} on failure.
    """
    try:
        from pptx import Presentation

        content = await file.read()
        if not content:
            return {"valid": False, "error": "Empty file upload"}

        # Load from bytes
        prs = Presentation(io.BytesIO(content))

        # Check 1: Has slide layouts
        if len(prs.slide_layouts) < 3:
            return {
                "valid": False,
                "error": f"Template has only {len(prs.slide_layouts)} slide layouts (need at least 3)",
            }

        # Check 2: Scan ALL layouts for a title placeholder (idx=0)
        # and a content/body placeholder (idx=1)
        has_title_layout = False
        has_content_layout = False
        title_layout_idx = None
        content_layout_idx = None
        blank_layout_idx = None

        for i, layout in enumerate(prs.slide_layouts):
            layout_has_title = False
            layout_has_content = False
            for ph in layout.placeholders:
                if ph.placeholder_format.idx == 0:
                    layout_has_title = True
                if ph.placeholder_format.idx == 1:
                    layout_has_content = True

            if layout_has_title and not has_title_layout:
                has_title_layout = True
                title_layout_idx = i
            if layout_has_title and layout_has_content and not has_content_layout:
                has_content_layout = True
                content_layout_idx = i
            # A "blank" layout is one with no placeholders
            if not list(layout.placeholders) and blank_layout_idx is None:
                blank_layout_idx = i

        if not has_title_layout:
            return {
                "valid": False,
                "error": "No layout has a title placeholder (idx=0). The template needs at least one layout with a title text placeholder.",
            }
        if not has_content_layout:
            return {
                "valid": False,
                "error": "No layout has both a title (idx=0) and content/body (idx=1) placeholder. The template needs at least one content layout.",
            }

        # Check 3: Slide dimensions are 16:9 (w/h ≈ 1.778)
        aspect = prs.slide_width / prs.slide_height
        if abs(aspect - 16 / 9) > 0.15:
            return {
                "valid": False,
                "error": f'Slide aspect ratio is {aspect:.3f} (expected 16:9 ≈ 1.778). Got {prs.slide_width/914400:.1f}" x {prs.slide_height/914400:.1f}"',
            }

        # Check 4: Can add a slide using the content layout without error
        try:
            slide = prs.slides.add_slide(prs.slide_layouts[content_layout_idx])
            for ph in slide.placeholders:
                if ph.placeholder_format.idx == 0:
                    ph.text = "Test Title"
                elif ph.placeholder_format.idx == 1:
                    ph.text = "Test bullet"
        except Exception as e:
            return {
                "valid": False,
                "error": f"Failed to add a test slide (layout {content_layout_idx}): {e}",
            }

        # All checks passed
        layout_info = []
        for i, layout in enumerate(prs.slide_layouts):
            phs = []
            for ph in layout.placeholders:
                phs.append(f"idx={ph.placeholder_format.idx}")
            layout_info.append(
                f"  Layout {i} '{layout.name}': {', '.join(phs) if phs else 'no placeholders'}"
            )

        return {
            "valid": True,
            "details": {
                "layouts": len(prs.slide_layouts),
                "slide_width": f"{prs.slide_width/914400:.2f}in",
                "slide_height": f"{prs.slide_height/914400:.2f}in",
                "aspect_ratio": f"{aspect:.3f}",
                "title_layout_index": title_layout_idx,
                "content_layout_index": content_layout_idx,
                "blank_layout_index": blank_layout_idx,
                "layout_map": "\n".join(layout_info),
            },
        }

    except ImportError:
        return {"valid": False, "error": "python-pptx is not installed"}
    except Exception as e:
        return {"valid": False, "error": f"Failed to open PPTX file: {e}"}


# ─── Generated Files ─────────────────────────────────────────────────


@router.get("/workspace/generated-files")
async def list_generated_files(
    file_type: Optional[str] = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
):
    """List all generated files by querying messages.deliverables JSONB.

    Each deliverable is a JSONB object inside the messages.deliverables array.
    We use jsonb_array_elements to unnest them, then join with conversations
    for the title and messages for the content (original prompt).
    """
    # Build the query — unnest deliverables using LATERAL join
    # sql = text("""
    #     SELECT
    #         d->>'filename'        AS filename,
    #         d->>'format'          AS file_type,
    #         d->>'type'            AS deliverable_type,
    #         d->>'download_url'    AS download_url,
    #         d->>'report_id'       AS report_id,
    #         (d->>'created_at')::bigint AS created_at_epoch,
    #         m.id::text            AS message_id,
    #         m.conversation_id::text AS conversation_id,
    #         c.title               AS conversation_title,
    #         m.content             AS message_content,
    #         m.created_at          AS message_created_at
    #     FROM messages m
    #     CROSS JOIN LATERAL jsonb_array_elements(m.deliverables) AS d
    #     LEFT JOIN conversations c ON c.id = m.conversation_id
    #     WHERE m.deliverables IS NOT NULL
    #       AND jsonb_array_length(m.deliverables) > 0
    # """)
    sql = text("""
        SELECT
            d->>'filename'          AS filename,
            d->>'format'            AS file_type,
            d->>'type'              AS deliverable_type,
            d->>'download_url'      AS download_url,
            d->>'report_id'         AS report_id,
            (d->>'created_at')::bigint AS created_at_epoch,
            m.id::text              AS message_id,
            m.conversation_id::text AS conversation_id,
            c.title                 AS conversation_title,
            m.content               AS message_content,
            m.created_at            AS message_created_at
        FROM messages m
        CROSS JOIN LATERAL jsonb_array_elements(m.deliverables) AS d
        LEFT JOIN conversations c ON c.id = m.conversation_id
        WHERE jsonb_typeof(m.deliverables) = 'array'
    """)

    params = {}
    if file_type and file_type != "all":
        sql = text(sql.text + " AND d->>'format' = :ft")
        params["ft"] = file_type

    sql = text(sql.text + " ORDER BY m.created_at DESC LIMIT :lim")
    params["lim"] = limit

    result = await db.execute(sql.bindparams(**params))
    rows = result.all()

    # Get file sizes from disk
    reports_dir = _get_data_dir() / "reports"
    items = []
    for row in rows:
        filename = row[0] or "unknown"
        ftype = row[1] or ""
        report_id = row[4] or ""

        # Try to get file size
        file_size = None
        if report_id:
            for ext in ("pdf", "docx", "pptx"):
                p = reports_dir / f"{report_id}.{ext}"
                if p.exists():
                    file_size = p.stat().st_size
                    break

        # Extract the original prompt from the user message preceding this assistant message
        # For now, use the message content as a proxy
        original_prompt = None
        if row[9]:  # message_content
            # The assistant message content is the response, not the prompt.
            # We'd need to query the preceding user message — skip for now.
            pass

        items.append(
            {
                "filename": filename,
                "file_type": ftype,
                "deliverable_type": row[2] or "",
                "download_url": row[3] or "",
                "report_id": report_id,
                "created_at": (
                    int(row[5])
                    if row[5]
                    else (int(row[10].timestamp()) if row[10] else 0)
                ),
                "message_id": row[6],
                "conversation_id": row[7],
                "conversation_title": row[8] or "Untitled",
                "file_size": file_size,
                "original_prompt": original_prompt,
                "thumbnail_url": (
                    f"/api/reports/{report_id}/thumbnail"
                    if report_id and ftype == "pptx"
                    else None
                ),
            }
        )

    return {"files": items, "total": len(items)}


# ─── Thumbnail upload ────────────────────────────────────────────────


@router.post("/workspace/templates/{template_id}/thumbnail")
async def upload_thumbnail(
    template_id: str,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    """Upload a thumbnail image for a template. Resizes to 200x150 and stores as base64."""
    try:
        from PIL import Image
    except ImportError:
        raise HTTPException(500, "Pillow not installed for image processing")

    try:
        tid = uuid.UUID(template_id)
    except ValueError:
        raise HTTPException(400, "Invalid template ID")

    result = await db.execute(select(Template).where(Template.id == tid))
    t = result.scalar_one_or_none()
    if not t:
        raise HTTPException(404, "Template not found")

    content = await file.read()
    img = Image.open(io.BytesIO(content))
    img = img.convert("RGB")
    img.thumbnail((200, 150))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")

    t.thumbnail = b64
    t.updated_at = datetime.utcnow()
    await db.flush()

    return {"ok": True, "thumbnail": b64[:50] + "..."}
