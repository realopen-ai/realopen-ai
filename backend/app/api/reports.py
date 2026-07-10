"""
Reports API — download endpoint for generated report files.

Serves files from the data/reports/ directory with the correct
Content-Type and Content-Disposition headers so the browser triggers
a download. Also provides thumbnail endpoints for presentations.
"""

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, Response

logger = logging.getLogger(__name__)

router = APIRouter()


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
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


@router.get("/reports/{report_id}/download")
async def download_report(report_id: str):
    """Download a generated report file by its ID.

    The report_id is a UUID (without extension). The endpoint looks for
    a file named {report_id}.pdf or {report_id}.docx in the data/reports/
    directory and streams it with the correct Content-Type.
    """
    # Validate report_id — must be a safe filename (UUID-like)
    # Strip any path separators to prevent directory traversal
    safe_id = os.path.basename(report_id)

    reports_dir = _get_data_dir() / "reports"

    # Try each supported extension
    for ext, mime_type in _MIME_TYPES.items():
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
            from app.services import libreoffice

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
