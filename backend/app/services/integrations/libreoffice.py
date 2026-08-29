"""
LibreOffice helper — headless document conversion and thumbnail rendering.

This module wraps the `soffice` wrapper script (at /usr/lib/libreoffice/program/soffice)
to provide PPTX -> PNG thumbnail rendering, PPTX -> PDF conversion, and
PPTX -> per-slide JPEG rendering (for the in-app slide viewer).

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


# ══════════════════════════════════════════════════════════════════════
# PPTX → per-slide JPEG rendering (for the in-app slide viewer)
# ══════════════════════════════════════════════════════════════════════
#
# Pipeline:  PPTX --soffice--> PDF --fitz/pdftoppm--> slide_NNN.jpg
#                                                 └-> slide_NNN_t.jpg (thumb)
#
# Results are cached in a directory next to the PPTX:
#     data/reports/{report_id}_slides/
#         manifest.json          — count + dims + source mtime/size
#         slide_001.jpg          — full-size render (≤ 1600px wide)
#         slide_001_t.jpg        — thumbnail (≤ 360px wide)
#         ...
#
# The cache is invalidated automatically whenever the source PPTX changes
# (mtime or size mismatch against the manifest).

# Manifest format version — bump when the layout/naming changes.
SLIDES_MANIFEST_VERSION = 1

# Full render max width (px). 16:9 @1600px is crisp on retina yet ~150-300KB.
SLIDE_MAX_WIDTH = 1600

# Thumbnail max width (px) — used by the viewer's filmstrip.
SLIDE_THUMB_WIDTH = 360

# JPEG encoders quality.
_SLIDE_JPEG_QUALITY = 85
_THUMB_JPEG_QUALITY = 80


def slides_cache_dir(pptx_path: Path) -> Path:
    """Directory where per-slide renders for a PPTX are cached.

    E.g. data/reports/abc.pptx → data/reports/abc_slides/
    """
    return pptx_path.parent / f"{pptx_path.stem}_slides"


def slide_image_name(index: int, thumb: bool = False) -> str:
    """Filename for a slide render (1-based index)."""
    return f"slide_{index:03d}{'_t' if thumb else ''}.jpg"


def read_slides_manifest(cache_dir: Path) -> Optional[dict]:
    """Read and validate manifest.json from a slide cache dir.

    Returns the parsed manifest dict, or None if missing/corrupt/too old.
    """
    manifest_path = cache_dir / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        import json

        data = json.loads(manifest_path.read_text())
        if (
            not isinstance(data, dict)
            or data.get("version") != SLIDES_MANIFEST_VERSION
            or not isinstance(data.get("count"), int)
            or data["count"] < 1
        ):
            return None
        return data
    except Exception as e:
        _log("slides manifest unreadable (%s): %s", manifest_path, e)
        return None


def is_slide_cache_valid(pptx_path: Path, cache_dir: Optional[Path] = None) -> bool:
    """Check whether the on-disk slide render cache is fresh and complete.

    Pure filesystem check — no LibreOffice involvement. Valid when:
      - manifest.json exists, parses, and is the current version
      - the recorded source mtime+size match the PPTX on disk
      - every full-size AND thumbnail image file exists
    """
    if not pptx_path.exists():
        return False
    cache_dir = cache_dir or slides_cache_dir(pptx_path)

    manifest = read_slides_manifest(cache_dir)
    if manifest is None:
        return False

    try:
        src_stat = pptx_path.stat()
        if manifest.get("source_mtime") != src_stat.st_mtime:
            return False
        if manifest.get("source_size") != src_stat.st_size:
            return False
    except OSError:
        return False

    for i in range(1, manifest["count"] + 1):
        if not (cache_dir / slide_image_name(i)).is_file():
            return False
        if not (cache_dir / slide_image_name(i, thumb=True)).is_file():
            return False
    return True


def _pil_to_jpeg_bytes(img, max_width: int, quality: int) -> tuple[bytes, int, int]:
    """Flatten to RGB, downscale to max_width, encode as JPEG.

    Returns (jpeg_bytes, width, height).
    """
    from PIL import Image

    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, (255, 255, 255))
        if img.mode == "P":
            img = img.convert("RGBA")
        bg.paste(img, mask=img.split()[-1] if img.mode in ("RGBA", "LA") else None)
        img = bg
    elif img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    if w > max_width:
        img = img.resize((max_width, int(h * max_width / w)), Image.LANCZOS)

    import io

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality)
    return buf.getvalue(), img.size[0], img.size[1]


def _render_pdf_pages_with_fitz(
    pdf_path: Path,
    cache_dir: Path,
    max_width: int,
    thumb_width: int,
) -> Optional[list[dict]]:
    """Render every PDF page to slide_NNN.jpg (+ _t thumb) using PyMuPDF.

    Returns a list of {"index", "width", "height"} dicts, or None on failure.
    Runs synchronously — call via asyncio.to_thread().
    """
    try:
        import fitz
        from PIL import Image
    except ImportError as e:
        _log("PyMuPDF/Pillow unavailable for rendering: %s", e)
        return None

    pages: list[dict] = []
    try:
        doc = fitz.open(str(pdf_path))
        with doc:
            for page in doc:
                # Scale so the page renders ≈ max_width pixels wide.
                zoom = max_width / max(page.rect.width, 1.0)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)

                full, fw, fh = _pil_to_jpeg_bytes(img, max_width, _SLIDE_JPEG_QUALITY)
                thumb, _, _ = _pil_to_jpeg_bytes(img, thumb_width, _THUMB_JPEG_QUALITY)

                i = len(pages) + 1
                (cache_dir / slide_image_name(i)).write_bytes(full)
                (cache_dir / slide_image_name(i, thumb=True)).write_bytes(thumb)
                pages.append({"index": i, "width": fw, "height": fh})
        return pages or None
    except Exception as e:
        _log("fitz page rendering failed: %s", e)
        return None


def _render_pdf_pages_with_pdftoppm(
    pdf_path: Path,
    cache_dir: Path,
    max_width: int,
    thumb_width: int,
) -> Optional[list[dict]]:
    """Fallback renderer using poppler's pdftoppm CLI.

    Produces slide_NNN.jpg (+ _t thumb) like the fitz path. Returns the same
    shape, or None if pdftoppm is missing / fails.
    """
    import subprocess

    binary = shutil.which("pdftoppm")
    if not binary:
        _log("pdftoppm not found — cannot fall back to poppler rendering")
        return None

    out_dir = Path(tempfile.mkdtemp(prefix="pdftoppm_"))
    try:
        # --scale-to N scales the LONG side to N px; 16:9 decks are wider
        # than tall so this yields ≈ max_width-wide output.
        proc = subprocess.run(
            [
                binary,
                "-jpeg",
                "-jpegopt",
                "quality=90",
                "-scale-to",
                str(max_width),
                str(pdf_path),
                str(out_dir / "page"),
            ],
            capture_output=True,
            timeout=120,
        )
        if proc.returncode != 0:
            _log(
                "pdftoppm failed (rc=%d): %s",
                proc.returncode,
                proc.stderr.decode(errors="replace")[:300],
            )
            return None

        # pdftoppm names outputs page-1.jpg, page-01.jpg, page-001.jpg …
        # (zero-padded to the page count). Sort numerically.
        files = sorted(
            out_dir.glob("page-*.jpg"),
            key=lambda p: int(p.name.rsplit("-", 1)[1].split(".")[0]),
        )
        if not files:
            _log("pdftoppm produced no pages for %s", pdf_path)
            return None

        pages: list[dict] = []
        for i, f in enumerate(files, start=1):
            try:
                from PIL import Image

                img = Image.open(str(f))
                full, fw, fh = _pil_to_jpeg_bytes(img, max_width, _SLIDE_JPEG_QUALITY)
                thumb, _, _ = _pil_to_jpeg_bytes(img, thumb_width, _THUMB_JPEG_QUALITY)
            except ImportError:
                # No Pillow — serve pdftoppm's own JPEGs as-is, skip thumbs
                # by duplicating the full image as the "thumbnail".
                full = f.read_bytes()
                fw = fh = 0
                thumb = full

            (cache_dir / slide_image_name(i)).write_bytes(full)
            (cache_dir / slide_image_name(i, thumb=True)).write_bytes(thumb)
            pages.append({"index": i, "width": fw, "height": fh})

        return pages
    except Exception as e:
        _log("pdftoppm rendering failed: %s", e)
        return None
    finally:
        shutil.rmtree(out_dir, ignore_errors=True)


def _write_slides_manifest(
    cache_dir: Path,
    pptx_path: Path,
    pages: list[dict],
) -> dict:
    """Persist manifest.json describing a fresh slide render cache."""
    import json
    import time

    src_stat = pptx_path.stat()
    first = pages[0]
    manifest = {
        "version": SLIDES_MANIFEST_VERSION,
        "count": len(pages),
        "width": first.get("width") or 0,
        "height": first.get("height") or 0,
        "source_mtime": src_stat.st_mtime,
        "source_size": src_stat.st_size,
        "generated_at": time.time(),
    }
    (cache_dir / "manifest.json").write_text(json.dumps(manifest))
    return manifest


async def convert_pptx_to_slide_images(
    pptx_path: Path,
    cache_dir: Optional[Path] = None,
    max_width: int = SLIDE_MAX_WIDTH,
    thumb_width: int = SLIDE_THUMB_WIDTH,
    force: bool = False,
) -> Optional[dict]:
    """Render every slide of a PPTX as JPEG images (full + thumbnail).

    Pipeline: PPTX → PDF (LibreOffice, cached next to the PPTX) → per-page
    JPEGs (PyMuPDF primary, pdftoppm fallback) → manifest.json.

    A fresh cache is written to {cache_dir}; any previous partial cache is
    wiped first so the directory never mixes generations.

    Returns the manifest dict on success, or None on failure (missing
    PPTX, LibreOffice unavailable, conversion error, zero pages).
    """
    if not pptx_path.exists():
        _log("PPTX file not found: %s", pptx_path)
        return None

    cache_dir = cache_dir or slides_cache_dir(pptx_path)

    # Serve from cache when still fresh (unless forced).
    if not force and is_slide_cache_valid(pptx_path, cache_dir):
        manifest = read_slides_manifest(cache_dir)
        if manifest is not None:
            return manifest

    # Fresh generation — start from an empty cache dir.
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # Step 1: PPTX → PDF. Drop a stale cached PDF first so a regenerated
    # PPTX never shows old slides.
    pdf_path = pptx_path.with_suffix(".pdf")
    try:
        if pdf_path.exists() and pdf_path.stat().st_mtime < pptx_path.stat().st_mtime:
            pdf_path.unlink()
            _log("dropped stale cached PDF (older than PPTX): %s", pdf_path.name)
    except OSError:
        pass

    if not pdf_path.exists():
        converted = await convert_pptx_to_pdf(pptx_path)
        if converted is None or not converted.exists():
            _log("PPTX→PDF failed — cannot render slides")
            return None
        pdf_path = converted

    # Step 2: PDF → per-page JPEGs (try fitz, fall back to pdftoppm).
    pages = await asyncio.to_thread(
        _render_pdf_pages_with_fitz, pdf_path, cache_dir, max_width, thumb_width
    )
    if pages is None:
        _log("fitz renderer failed — falling back to pdftoppm")
        pages = await asyncio.to_thread(
            _render_pdf_pages_with_pdftoppm,
            pdf_path,
            cache_dir,
            max_width,
            thumb_width,
        )
    if not pages:
        _log("no slide images rendered for %s", pptx_path.name)
        return None

    manifest = await asyncio.to_thread(
        _write_slides_manifest, cache_dir, pptx_path, pages
    )
    _log(
        "slide images rendered: %s → %d slides in %s",
        pptx_path.name,
        manifest["count"],
        cache_dir.name,
    )
    return manifest
