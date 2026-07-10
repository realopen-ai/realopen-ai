"""
LibreOffice helper — headless document conversion and thumbnail rendering.

This module wraps the `soffice` (LibreOffice) binary to provide:
  - PPTX -> PNG thumbnail rendering
  - PPTX -> PDF conversion

## Concurrency

LibreOffice does NOT handle concurrent invocations well (it uses a
single-user-profile lock). We serialize all calls with an asyncio.Lock
and give each call its own temporary user profile directory via
`-env:UserInstallation`.

## Availability

LibreOffice is an optional dependency (installed via the Dependencies UI
to the /opt/optional overlay volume). All functions gracefully degrade
when it's not available — callers should check `is_available()` first
or handle `None` returns.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[libreoffice] {formatted}", flush=True)


# Serialize all soffice invocations — it can't handle concurrent runs
# with a single user profile, and even with separate profiles it's safer.
_so_lock = asyncio.Lock()

# Timeout for soffice conversions (seconds). LibreOffice can hang on
# corrupted files; we don't want to block the backend forever.
_SOFFICE_TIMEOUT = 60


def is_available() -> bool:
    """Check if LibreOffice (soffice) is installed and available.

    Checks the /opt/optional overlay first, then the system PATH.
    """
    from app.services.deps_manager import is_installed, get_dependency

    dep = get_dependency("libreoffice")
    if dep and is_installed(dep):
        return True

    # Fallback: check PATH directly (e.g. dev environment without overlay)
    return shutil.which("soffice") is not None


def _find_soffice() -> Optional[str]:
    """Find the soffice binary path."""
    # Check overlay first
    overlay_bin = Path("/opt/optional/usr/bin/soffice")
    if overlay_bin.exists():
        return str(overlay_bin)
    # Check system PATH
    return shutil.which("soffice")


async def _run_soffice(
    args: list[str],
    timeout: int = _SOFFICE_TIMEOUT,
) -> tuple[int, str, str]:
    """Run a soffice command with a temporary user profile.

    Returns (returncode, stdout, stderr).
    """
    soffice = _find_soffice()
    if not soffice:
        return -1, "", "LibreOffice (soffice) not found"

    # Create a temp dir for the user profile + output
    profile_dir = tempfile.mkdtemp(prefix="lo_profile_")
    output_dir = tempfile.mkdtemp(prefix="lo_output_")

    try:
        cmd = [
            soffice,
            "--headless",
            "--norestore",
            "--nodefault",
            "--nolockcheck",
            "--nologo",
            f"-env:UserInstallation=file://{profile_dir}",
        ] + args

        _log("running: %s", " ".join(cmd[:3]) + " ... " + " ".join(args))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                **os.environ,
                "HOME": profile_dir,  # soffice may write to HOME
            },
        )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            _log("soffice timed out after %ds", timeout)
            return -1, "", f"Timeout after {timeout}s"

        rc = proc.returncode
        out = stdout.decode(errors="replace") if stdout else ""
        err = stderr.decode(errors="replace") if stderr else ""
        return rc, out, err

    finally:
        # Clean up temp dirs (best effort)
        for d in (profile_dir, output_dir):
            try:
                shutil.rmtree(d, ignore_errors=True)
            except Exception:
                pass


async def render_pptx_thumbnail(
    pptx_path: Path,
    max_width: int = 400,
) -> Optional[bytes]:
    """Render the first slide of a PPTX as a JPEG thumbnail.

    Uses LibreOffice to convert the PPTX to PNG (first slide), then
    Pillow to resize and convert to JPEG.

    Args:
        pptx_path: Path to the .pptx file
        max_width: Maximum thumbnail width in pixels (aspect ratio preserved)

    Returns:
        JPEG image bytes, or None if LibreOffice is unavailable / conversion failed.
    """
    if not is_available():
        _log("LibreOffice not available — cannot render thumbnail")
        return None

    if not pptx_path.exists():
        _log("PPTX file not found: %s", pptx_path)
        return None

    async with _so_lock:
        # Create a temp output dir for the PNG
        out_dir = Path(tempfile.mkdtemp(prefix="lo_thumb_"))
        try:
            # Convert PPTX → PNG (soffice renders the first slide as PNG)
            rc, stdout, stderr = await _run_soffice(
                ["--convert-to", "png", "--outdir", str(out_dir), str(pptx_path)]
            )

            if rc != 0:
                _log("soffice conversion failed (rc=%d): %s", rc, stderr[:200])
                return None

            # Find the output PNG — same stem as input, .png extension
            stem = pptx_path.stem
            png_path = out_dir / f"{stem}.png"
            if not png_path.exists():
                # Sometimes soffice names it differently — find any .png
                pngs = list(out_dir.glob("*.png"))
                if not pngs:
                    _log("no PNG output found in %s", out_dir)
                    return None
                png_path = pngs[0]

            # Resize with Pillow and convert to JPEG
            try:
                from PIL import Image  # noqa: F401
            except ImportError:
                _log("Pillow not installed — returning raw PNG")
                return png_path.read_bytes()

            return await asyncio.to_thread(_resize_to_jpeg, png_path, max_width)

        finally:
            shutil.rmtree(out_dir, ignore_errors=True)


def _resize_to_jpeg(png_path: Path, max_width: int) -> Optional[bytes]:
    """Resize a PNG to max_width (maintaining aspect ratio) and return JPEG bytes."""
    try:
        from PIL import Image

        img = Image.open(str(png_path))
        if img.mode in ("RGBA", "LA", "P"):
            # Flatten transparency onto white background for JPEG
            bg = Image.new("RGB", img.size, (255, 255, 255))
            if img.mode == "P":
                img = img.convert("RGBA")
            bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Resize maintaining aspect ratio
        w, h = img.size
        if w > max_width:
            new_h = int(h * max_width / w)
            img = img.resize((max_width, new_h), Image.LANCZOS)

        import io

        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        data = buf.getvalue()
        _log(
            "thumbnail rendered: %dx%d → %dx%d (%d bytes JPEG)",
            w,
            h,
            img.size[0],
            img.size[1],
            len(data),
        )
        return data

    except Exception as e:
        _log("Pillow resize failed: %s", e)
        return None


async def convert_pptx_to_pdf(pptx_path: Path) -> Optional[Path]:
    """Convert a PPTX file to PDF using LibreOffice.

    The output PDF is saved next to the input file with a .pdf extension.

    Returns:
        Path to the generated PDF, or None if conversion failed.
    """
    if not is_available():
        _log("LibreOffice not available — cannot convert to PDF")
        return None

    if not pptx_path.exists():
        _log("PPTX file not found: %s", pptx_path)
        return None

    out_dir = pptx_path.parent

    async with _so_lock:
        rc, stdout, stderr = await _run_soffice(
            ["--convert-to", "pdf", "--outdir", str(out_dir), str(pptx_path)]
        )

        if rc != 0:
            _log("soffice PDF conversion failed (rc=%d): %s", rc, stderr[:200])
            return None

        pdf_path = out_dir / f"{pptx_path.stem}.pdf"
        if not pdf_path.exists():
            _log("PDF output not found: %s", pdf_path)
            return None

        _log("PDF generated: %s (%d bytes)", pdf_path.name, pdf_path.stat().st_size)
        return pdf_path


async def generate_and_cache_thumbnail(
    pptx_path: Path,
    thumb_path: Path,
    max_width: int = 400,
) -> Optional[Path]:
    """Generate a thumbnail and cache it to disk.

    Args:
        pptx_path: Source PPTX file
        thumb_path: Where to save the JPEG thumbnail
        max_width: Maximum thumbnail width

    Returns:
        Path to the saved thumbnail, or None if generation failed.
    """
    jpeg_bytes = await render_pptx_thumbnail(pptx_path, max_width)
    if not jpeg_bytes:
        return None

    try:
        thumb_path.parent.mkdir(parents=True, exist_ok=True)
        thumb_path.write_bytes(jpeg_bytes)
        _log("thumbnail cached: %s (%d bytes)", thumb_path.name, len(jpeg_bytes))
        return thumb_path
    except Exception as e:
        _log("failed to cache thumbnail: %s", e)
        return None
