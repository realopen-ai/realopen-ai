"""
Reports API — download endpoint for generated report files.

Serves files from the data/reports/ directory with the correct
Content-Type and Content-Disposition headers so the browser triggers
a download.
"""

import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

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
