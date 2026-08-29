"""
Reports API — download endpoint for generated report files.

Serves files from the data/reports/ directory with the correct
Content-Type and Content-Disposition headers so the browser triggers
a download. Also provides thumbnail endpoints for presentations and
per-page image endpoints for the in-app document viewer
(PPTX / PDF / DOCX).
"""

import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter()

# Per-report locks so two simultaneous viewer opens never run the
# (expensive, serialized) slide generation twice for the same deck.
_slide_gen_locks: dict[str, asyncio.Lock] = {}
_SLIDE_LOCKS_MAX = 128


def _get_data_dir() -> Path:
    """Get the data directory (same logic as app.main._get_data_dir)."""
    candidates = [
        Path("/app/data"),
        Path(__file__).resolve().parent.parent.parent.parent / "data",
    ]
    for d in candidates:
        if d.exists():
            return d
    # Fallback
    fallback = Path("/app/data")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


# MIME types for supported report formats
_MIME_TYPES = {
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "pdf": "application/pdf",
}


@router.get("/reports/{report_id}/download")
async def download_report(report_id: str):
    """Download a generated report file by its ID.

    The report_id is a UUID (without extension). The endpoint looks for
    a file named {report_id}.pptx / .docx / .pdf in the data/reports/
    directory and streams it with the correct Content-Type.

    Probe order matters: PPTX and DOCX first, PDF last. The slide
    viewer caches a converted PDF *next to* a previewed PPTX report
    ({id}.pdf beside {id}.pptx), so probing .pdf first would make the
    download button serve the conversion artifact instead of the
    original presentation. A bare .pptx/.docx file is always the
    original deliverable.
    """
    # Validate report_id — must be a safe filename (UUID-like)
    # Strip any path separators to prevent directory traversal
    safe_id = os.path.basename(report_id)

    reports_dir = _get_data_dir() / "reports"

    # Originals first (.pdf can be a conversion artifact of a previewed
    # PPTX report) — see the docstring above.
    for ext in ("pptx", "docx", "pdf"):
        mime_type = _MIME_TYPES[ext]
        file_path = reports_dir / f"{safe_id}.{ext}"
        if file_path.exists() and file_path.is_file():
            # Build a user-friendly download filename
            download_name = f"report.{ext}"

            logger.info("[reports] serving %s (%s)", file_path.name, mime_type)

            return FileResponse(
                path=str(file_path),
                media_type=mime_type,
                filename=download_name,
                headers={
                    "Content-Disposition": f'attachment; filename="{download_name}"',
                    "Cache-Control": "private, max-age=3600",
                },
            )

    # No file found
    raise HTTPException(
        status_code=404,
        detail=f"Report not found: {safe_id}",
    )


@router.get("/reports/{report_id}/thumbnail")
async def get_report_thumbnail(report_id: str):
    """Get a thumbnail image for a generated presentation (PPTX).

    Flow:
      1. If a cached thumbnail exists on disk -> serve it immediately.
      2. If not, but the PPTX exists and LibreOffice is available ->
         generate on-demand, cache, and serve.
      3. If LibreOffice is not available or the PPTX doesn't exist -> 404.

    The frontend uses this URL in an <img> tag with onError fallback to
    a file-type icon, so a 404 gracefully degrades to the icon.

    Returns JPEG image bytes with long-lived cache headers.
    """
    safe_id = os.path.basename(report_id)
    reports_dir = _get_data_dir() / "reports"

    thumb_path = reports_dir / f"{safe_id}_thumb.jpg"
    pptx_path = reports_dir / f"{safe_id}.pptx"

    # Step 1: Serve cached thumbnail if it exists
    if thumb_path.exists() and thumb_path.is_file():
        return Response(
            content=thumb_path.read_bytes(),
            media_type="image/jpeg",
            headers={
                "Cache-Control": "public, max-age=86400",  # 24h
            },
        )

    # Step 2: Generate on-demand if PPTX exists + LibreOffice available
    if pptx_path.exists() and pptx_path.is_file():
        try:
            from app.services.integrations import libreoffice

            if not libreoffice.is_available():
                # LibreOffice not installed — can't generate a thumbnail
                raise HTTPException(
                    status_code=404,
                    detail="Thumbnail unavailable — LibreOffice is not installed.",
                )

            # Generate + cache the thumbnail
            cached = await libreoffice.generate_and_cache_thumbnail(
                pptx_path, thumb_path, max_width=400
            )

            if cached and cached.exists():
                return Response(
                    content=cached.read_bytes(),
                    media_type="image/jpeg",
                    headers={
                        "Cache-Control": "public, max-age=86400",
                    },
                )
            else:
                # Generation failed (e.g. corrupted PPTX, soffice error)
                raise HTTPException(
                    status_code=404,
                    detail="Thumbnail generation failed.",
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("Thumbnail generation error: %s", e)
            raise HTTPException(
                status_code=404,
                detail=f"Thumbnail generation error: {e}",
            )

    # Step 3: No PPTX file found
    raise HTTPException(
        status_code=404,
        detail=f"PPTX file not found for thumbnail: {safe_id}",
    )


def _get_slide_gen_lock(report_id: str) -> asyncio.Lock:
    """Get (or create) the generation lock for a report."""
    lock = _slide_gen_locks.get(report_id)
    if lock is None:
        # Bound the dict so long-running servers don't accumulate locks
        # for every report ever viewed.
        if len(_slide_gen_locks) >= _SLIDE_LOCKS_MAX:
            _slide_gen_locks.clear()
        lock = asyncio.Lock()
        _slide_gen_locks[report_id] = lock
    return lock


@router.get("/reports/{report_id}/slides")
async def get_report_slides(
    report_id: str,
    format: str = Query(default="pptx", pattern="^(pptx|pdf|docx)$"),
):
    """Page manifest for the in-app document viewer (PPTX / PDF / DOCX).

    Renders every page of the document as JPEG images (full-size +
    thumbnail) on first request, caches them under
    data/reports/{report_id}_slides/, and returns a JSON manifest:

        {
          "report_id": "...",
          "count": 10,
          "width": 1600,
          "height": 900,
          "slides": [
            {"index": 1, "url": "/api/reports/{id}/slides/1?v=...",
             "thumb_url": "/api/reports/{id}/slides/1?v=...&variant=thumb",
             "notes": "Speaker notes text for slide 1 (\"\" when none)"},
            ...
          ]
        }

    The `?format=` query parameter selects the deliverable to render
    ("pptx" default, "pdf", or "docx") — a report_id maps to exactly
    one file, and the PDF preview of a PPTX report is cached as
    {id}.pdf, so the format must be explicit to pick the ORIGINAL file.

    Availability per format:
      - pptx / docx — LibreOffice required (document → PDF conversion)
      - pdf         — no LibreOffice needed (rasterized directly with
                      PyMuPDF / poppler)

    The `?v=` cache-buster is the source file's mtime, so a regenerated
    document gets fresh URLs and stale browser cache entries are
    bypassed automatically.

    Errors:
      - 404 — file not found for the requested format
      - 404 — LibreOffice not installed (pptx/docx)
      - 404 — no PDF rasterizer available (pdf)
      - 404 — conversion/rendering failed
    """
    safe_id = os.path.basename(report_id)
    reports_dir = _get_data_dir() / "reports"

    source_path = reports_dir / f"{safe_id}.{format}"
    if not (source_path.exists() and source_path.is_file()):
        raise HTTPException(
            status_code=404,
            detail=f"{format.upper()} file not found: {safe_id}",
        )

    from app.services.integrations import libreoffice

    cache_dir = libreoffice.viewer_cache_dir(source_path)

    async with _get_slide_gen_lock(safe_id):
        if libreoffice.is_slide_cache_valid(source_path, cache_dir):
            manifest = libreoffice.read_slides_manifest(cache_dir)
        else:
            if format in ("pptx", "docx") and not libreoffice.is_available():
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "Document preview unavailable — LibreOffice is not "
                        f"installed. You can still download the .{format} file."
                    ),
                )
            if format == "pdf" and not libreoffice.can_rasterize_pdf():
                raise HTTPException(
                    status_code=404,
                    detail=(
                        "PDF preview unavailable — no PDF renderer "
                        "(PyMuPDF/poppler) is available on the server. "
                        "You can still download the .pdf file."
                    ),
                )
            manifest = await libreoffice.convert_document_to_page_images(
                source_path, cache_dir
            )

    if not manifest:
        raise HTTPException(
            status_code=404,
            detail="Page rendering failed. Please try again or download the file.",
        )

    try:
        version = int(source_path.stat().st_mtime)
    except OSError:
        version = 0

    count = manifest["count"]
    notes = manifest.get("notes") or []
    slides = [
        {
            "index": i,
            "url": f"/api/reports/{safe_id}/slides/{i}?v={version}",
            "thumb_url": f"/api/reports/{safe_id}/slides/{i}?v={version}&variant=thumb",
            # Per-slide speaker notes ("" when absent) — defensive against
            # manifests that predate the notes field.
            "notes": str(notes[i - 1]) if i - 1 < len(notes) else "",
        }
        for i in range(1, count + 1)
    ]

    return {
        "report_id": safe_id,
        "count": count,
        "width": manifest.get("width", 0),
        "height": manifest.get("height", 0),
        "slides": slides,
    }


@router.get("/reports/{report_id}/slides/{slide_number}")
async def get_report_slide_image(
    report_id: str,
    slide_number: int,
    variant: str = Query(default="full", pattern="^(full|thumb)$"),
):
    """Serve a single rendered slide image (JPEG).

    slide_number is 1-based. Use ?variant=thumb for the small filmstrip
    variant. The manifest endpoint must have been called first (it creates
    the cache); otherwise this returns 404. Works for every viewer format
    (pptx/pdf/docx) — they all share the {report_id}_slides cache layout.
    """
    safe_id = os.path.basename(report_id)
    reports_dir = _get_data_dir() / "reports"

    cache_dir = reports_dir / f"{safe_id}_slides"

    from app.services.integrations import libreoffice

    manifest = libreoffice.read_slides_manifest(cache_dir)
    if manifest is None:
        raise HTTPException(
            status_code=404,
            detail="Slides not rendered yet — request /slides first.",
        )

    if slide_number < 1 or slide_number > manifest["count"]:
        raise HTTPException(
            status_code=404,
            detail=f"Slide {slide_number} out of range (1..{manifest['count']}).",
        )

    image_path = cache_dir / libreoffice.slide_image_name(
        slide_number, thumb=(variant == "thumb")
    )
    if not (image_path.exists() and image_path.is_file()):
        raise HTTPException(status_code=404, detail="Slide image missing on disk.")

    return FileResponse(
        path=str(image_path),
        media_type="image/jpeg",
        headers={
            # The ?v= cache-buster makes these safely cacheable long-term.
            "Cache-Control": "private, max-age=604800",  # 7 days
        },
    )


@router.get("/reports/{report_id}/pdf")
async def get_report_pdf(report_id: str):
    """Get a PDF version of a generated presentation (PPTX) for in-browser viewing.

    Converts the PPTX to PDF using LibreOffice, caches the PDF on disk, and
    serves it inline (not as a download) so the frontend can display it in
    an <iframe>.

    Flow:
      1. If a cached PDF exists on disk -> serve it immediately.
      2. If not, but the PPTX exists and LibreOffice is available ->
         convert on-demand, cache, and serve.
      3. If LibreOffice is not available or the PPTX doesn't exist -> 404.

    Returns PDF bytes with Content-Disposition: inline (for iframe viewing).
    """
    safe_id = os.path.basename(report_id)
    reports_dir = _get_data_dir() / "reports"

    pdf_path = reports_dir / f"{safe_id}.pdf"
    pptx_path = reports_dir / f"{safe_id}.pptx"

    # Step 1: Serve cached PDF if it exists
    if pdf_path.exists() and pdf_path.is_file():
        return FileResponse(
            path=str(pdf_path),
            media_type="application/pdf",
            headers={
                "Content-Disposition": "inline",
                "Cache-Control": "private, max-age=3600",
            },
        )

    # Step 2: Convert on-demand if PPTX exists + LibreOffice available
    if pptx_path.exists() and pptx_path.is_file():
        try:
            from app.services.integrations import libreoffice

            if not libreoffice.is_available():
                raise HTTPException(
                    status_code=404,
                    detail="PDF view unavailable — LibreOffice is not installed.",
                )

            # Convert PPTX → PDF (saves next to the PPTX as {report_id}.pdf)
            result_path = await libreoffice.convert_pptx_to_pdf(pptx_path)

            if result_path and result_path.exists():
                return FileResponse(
                    path=str(result_path),
                    media_type="application/pdf",
                    headers={
                        "Content-Disposition": "inline",
                        "Cache-Control": "private, max-age=3600",
                    },
                )
            else:
                raise HTTPException(
                    status_code=404,
                    detail="PDF conversion failed.",
                )
        except HTTPException:
            raise
        except Exception as e:
            logger.exception("PDF conversion error: %s", e)
            raise HTTPException(
                status_code=404,
                detail=f"PDF conversion error: {e}",
            )

    # Step 3: No PPTX file found
    raise HTTPException(
        status_code=404,
        detail=f"PPTX file not found: {safe_id}",
    )
