"""
LibreOffice helper — headless document conversion and thumbnail rendering.

This module wraps the `soffice` wrapper script (at /usr/lib/libreoffice/program/soffice)
to provide PPTX → PNG thumbnail rendering and PPTX → PDF conversion.

LibreOffice is installed to its DEFAULT system location (/usr/lib/libreoffice)
via apt-get install, and that path is mounted as a Docker volume. This means:
  - All hardcoded paths in fundamentalrc are correct by default
  - The wrapper script handles URE_BOOTSTRAP, LD_LIBRARY_PATH, etc. itself
  - No env var hacks, no path patching, no overlay extraction needed

We just call the wrapper script. That's it.
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

# Path to the soffice wrapper script (on the mounted volume)
_SOFFICE_PATH = "/usr/lib/libreoffice/program/soffice"

# Path to soffice.bin (used for diagnostics)
_SOFFICE_BIN = "/usr/lib/libreoffice/program/soffice.bin"


def is_available() -> bool:
    """Check if LibreOffice (soffice) is installed."""
    return Path(_SOFFICE_BIN).exists() or shutil.which("soffice") is not None


def _find_soffice() -> Optional[str]:
    """Find the soffice wrapper script."""
    if Path(_SOFFICE_PATH).exists():
        return _SOFFICE_PATH
    return shutil.which("soffice")


async def _run_soffice(
    args: list[str],
    timeout: int = _SOFFICE_TIMEOUT,
) -> tuple[int, str, str]:
    """Run a soffice command. Returns (returncode, stdout, stderr).

    We call the wrapper script (not soffice.bin directly) because the wrapper
    handles all the environment setup: URE_BOOTSTRAP, LD_LIBRARY_PATH,
    fundamentalrc resolution, etc. Since LibreOffice is installed to its
    default location (/usr/lib/libreoffice), all paths are correct by default.
    """
    soffice = _find_soffice()
    if not soffice:
        return -1, "", "LibreOffice (soffice) not found"

    profile_dir = tempfile.mkdtemp(prefix="lo_profile_")

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

        _log("running: %s ... %s", " ".join(cmd[:3]), " ".join(args))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "HOME": profile_dir},
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
        shutil.rmtree(profile_dir, ignore_errors=True)


async def _diagnose_soffice_failure() -> None:
    """Run diagnostic checks when soffice fails."""
    _log("=== running diagnostics ===")

    # Check 1: soffice --version
    rc, out, err = await _run_soffice(["--version"], timeout=15)
    if rc == 0:
        _log("soffice --version OK: %s", out.strip()[:100])
    else:
        _log("soffice --version FAILED (rc=%d): %s", rc, err.strip()[:500])

    # Check 2: fonts
    for d in [Path("/usr/share/fonts"), Path("/usr/local/share/fonts")]:
        if d.exists():
            fonts = list(d.rglob("*.ttf")) + list(d.rglob("*.otf"))
            _log("%s — %d font files", d, len(fonts))

    # Check 3: fontconfig
    if Path("/etc/fonts/fonts.conf").exists():
        _log("/etc/fonts/fonts.conf exists (fontconfig OK)")

    # Check 4: shared libraries
    if Path(_SOFFICE_BIN).exists():
        import subprocess

        try:
            result = subprocess.run(
                ["ldd", _SOFFICE_BIN],
                capture_output=True,
                text=True,
                timeout=10,
            )
            missing = [i for i in result.stdout.split("\n") if "not found" in i]
            if missing:
                _log("MISSING LIBRARIES:")
                for m in missing:
                    _log("  %s", m.strip())
            else:
                _log("all shared libraries resolved")
        except Exception as e:
            _log("could not run ldd: %s", e)

    _log("=== diagnostics complete ===")


async def render_pptx_thumbnail(
    pptx_path: Path,
    max_width: int = 400,
) -> Optional[bytes]:
    """Render the first slide of a PPTX as a JPEG thumbnail.

    Uses LibreOffice to convert PPTX to PNG, then Pillow to resize to JPEG.
    Returns JPEG bytes, or None if unavailable/failed.
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
            # Convert PPTX to PNG (soffice renders the first slide as PNG)
            rc, stdout, stderr = await _run_soffice(
                ["--convert-to", "png", "--outdir", str(out_dir), str(pptx_path)]
            )

            if rc != 0:
                _log("soffice conversion failed (rc=%d)", rc)
                _log("stderr: %s", stderr.strip()[:1000])
                await _diagnose_soffice_failure()
                return None

            # Find the output PNG
            stem = pptx_path.stem
            png_path = out_dir / f"{stem}.png"
            if not png_path.exists():
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
        import io

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
        rc, _, stderr = await _run_soffice(
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


async def clean_pptx_with_libreoffice(pptx_path: Path) -> bool:
    """Clean/repair a PPTX file by opening it in LibreOffice and re-saving.

    python-pptx generates valid OOXML, but when using slide duplication
    (deep-copying template slides), the result can have issues that
    PowerPoint flags as "needs repair": duplicate shape IDsd, dangling
    relationship references, malformed XML elements, etc.

    LibreOffice's OOXML writer validates everything and produces a clean,
    PowerPoint-compatible file. This function:
      1. Opens the PPTX in LibreOffice headless
      2. Saves it as PPTX to a temp directory
      3. Replaces the original file with the cleaned version

    Returns True if the file was cleaned successfully, False otherwise.
    The original file is preserved if cleaning fails.
    """
    if not is_available():
        _log("LibreOffice not available — skipping PPTX cleaning")
        return False

    if not pptx_path.exists():
        _log("PPTX file not found: %s", pptx_path)
        return False

    async with _so_lock:
        out_dir = Path(tempfile.mkdtemp(prefix="lo_clean_"))
        try:
            # Convert PPTX → PPTX (LibreOffice opens, validates, re-saves)
            rc, stdout, stderr = await _run_soffice(
                ["--convert-to", "pptx", "--outdir", str(out_dir), str(pptx_path)]
            )

            if rc != 0:
                _log("PPTX cleaning failed (rc=%d): %s", rc, stderr.strip()[:300])
                return False

            # Find the cleaned output
            cleaned_path = out_dir / f"{pptx_path.stem}.pptx"
            if not cleaned_path.exists():
                _log("cleaned PPTX not found in %s", out_dir)
                return False

            # Replace the original with the cleaned version
            original_size = pptx_path.stat().st_size
            cleaned_size = cleaned_path.stat().st_size
            shutil.move(str(cleaned_path), str(pptx_path))

            _log(
                "PPTX cleaned: %s (%d → %d bytes, %s diff)",
                pptx_path.name,
                original_size,
                cleaned_size,
                (
                    f"+{cleaned_size - original_size}"
                    if cleaned_size > original_size
                    else f"{cleaned_size - original_size}"
                ),
            )
            return True

        finally:
            shutil.rmtree(out_dir, ignore_errors=True)
