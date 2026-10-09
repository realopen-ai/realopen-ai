"""
RAG (Retrieval-Augmented Generation) service for RealOpen-AI.

Digestion pipeline (ASYNC, with progress callbacks for SSE):
  1. Save raw upload to disk under data/documents/{doc_id}/{filename}.
  2. Extract text page-by-page (PDF/DOCX/XLSX/CSV/MD/TXT) — runs in a
     thread pool via asyncio.to_thread so the event loop stays free.
  3. Extract embedded images (PDF/DOCX).
  4. Adaptive-chunk the text: chunk size depends on total document length.
  5. For each image: call vision LLM → text description; treat description
     as an additional chunk with chunk_type="image_description".
  6. Embed every chunk with nomic-embed-text (768-dim).
  7. Persist Document + DocumentChunk rows.

CRITICAL: all blocking I/O (pdfplumber, pypdf, openpyxl, python-docx,
PIL, file writes) MUST be wrapped in asyncio.to_thread so the FastAPI
event loop can keep serving other requests, etc. while a large
PDF is being digested. Without this, a 200-page PDF blocks the entire
backend for minutes — including health checks, which causes Docker to
flag the container as unhealthy.

Retrieval (per-doc adaptive hybrid search):
  - Filter chunks by scope:
        public                                → any conversation
        private AND conversation_id = ?       → only that conversation
  - Vector search via pgvector cosine distance, plus tsvector BM25.
  - Hybrid score: W_vec * vec_sim + W_bm25 * bm25_norm
  - Take top RAG_TOP_K_PER_DOC chunks per document_id, then keep top
    RAG_TOP_K_TOTAL overall — prevents a single large doc from crowding
    out hits from other relevant docs.
  - Drop chunks with vec_sim < RAG_SIMILARITY_CUTOFF.

Citations: each chunk knows its document.filename, page_number,
line_start, line_end, image_path — the agent tool returns these to the
LLM and the frontend renders them as collapsible source cards. Image
chunks (chunk_type="image_description") carry the on-disk image path
so the frontend can show a thumbnail of the original image.
"""

from __future__ import annotations

import asyncio
from docx.text.paragraph import Paragraph
import base64
import hashlib
import io
import logging
import os
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import and_, delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Document, DocumentChunk
from app.db.session import async_session_factory
from app.services.embeddings import get_embedding, get_embeddings
from app.services import model_prefs
from app.services import providers
from app.prompts import get_prompt

logger = logging.getLogger(__name__)


def _log(msg: str, *args: Any) -> None:
    """Always-visible print() logger.

    Uses print() with flush=True so output appears immediately in the
    container logs — no DEBUG flag needed. This is critical for
    debugging digestion hangs in production where logger.debug output
    is suppressed by uvicorn's default config.
    """
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[rag] {formatted}", flush=True)


# ---------------------------------------------------------------------------
# Text sanitization — PostgreSQL rejects 0x00 in TEXT/VARCHAR columns
# ---------------------------------------------------------------------------


# Match any character that PostgreSQL TEXT cannot store. PostgreSQL's
# TEXT type rejects:
#   - 0x00 (NUL byte) — ALWAYS rejected, causes
#     `invalid byte sequence for encoding "UTF8": 0x00`
#   - Lone surrogates (U+D800..U+DFFF) — can't be encoded to UTF-8 by
#     asyncpg, cause `UnicodeEncodeError` on the wire
# PDF extractors (pdfplumber, pypdf) occasionally emit NUL bytes for
# broken/garbled text streams; python-docx and openpyxl can too if the
# source file has weird encoding. We strip these aggressively at every
# text-entry point so a single bad page can't poison the whole INSERT.
_SURROGATE_RE = re.compile("[\ud800-\udfff]")  # lone surrogate range


def _sanitize_text_for_pg(s: Any) -> str:
    """Strip characters that PostgreSQL TEXT/VARCHAR cannot store.

    This is the defensive fix for the production crash:
        `asyncpg.exceptions.CharacterNotInRepertoireError: invalid byte
        sequence for encoding "UTF8": 0x00`

    PDF/DOCX/XLSX extractors can occasionally produce NUL bytes (0x00)
    from garbled text streams, and asyncpg rejects the entire INSERT
    batch when any single parameter contains one — taking down the whole
    digestion and leaving the SQLAlchemy session in a poisoned
    "rolled-back" state where even the failure handler can't update the
    doc row.

    We strip:
      - "\\x00" NUL bytes (the actual production crash)
      - Lone UTF-16 surrogates (U+D800..U+DFFF) — can't be UTF-8 encoded
      - Other C0/C1 control chars are KEPT (newlines, tabs are valid)

    Also coerces None → "" so callers can use it on Optional[str].
    """
    if s is None:
        return ""
    if not isinstance(s, str):
        try:
            s = str(s)
        except Exception:
            return ""
    # Remove NUL bytes (the actual crash) — done first, separately, so
    # it's obvious in the code what we're defending against.
    if "\x00" in s:
        s = s.replace("\x00", "")
    # Remove lone surrogates that can't be UTF-8 encoded.
    if _SURROGATE_RE.search(s):
        s = _SURROGATE_RE.sub("\ufffd", s)  # replacement char
    return s


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class ExtractedPage:
    """One page of extracted text from a document.

    For non-paginated formats (TXT/MD/CSV), the entire text is one "page"
    with page_number=1 so the chunker and citation logic don't have to
    special-case them.
    """

    page_number: int
    text: str


@dataclass
class ExtractedImage:
    """An image extracted from a document, with the page it appeared on."""

    page_number: int
    image_bytes: bytes
    # Original format hint ("PNG", "JPEG", etc.) — used to pick a filename
    # extension when persisting to disk.
    format_hint: str = "PNG"


@dataclass
class ExtractionResult:
    """Output of the text + image extraction stage."""

    pages: List[ExtractedPage] = field(default_factory=list)
    images: List[ExtractedImage] = field(default_factory=list)
    mime_type: str = "text/plain"


@dataclass
class Chunk:
    """In-memory chunk before persistence.

    `text` is what gets embedded and stored. `page_number` / `line_start` /
    `line_end` are used for the source citation. `chunk_type` distinguishes
    a normal text slice from a vision-LLM description of an embedded image.
    """

    text: str
    chunk_index: int
    page_number: Optional[int] = None
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    chunk_type: str = "text"
    image_path: Optional[str] = None  # relative path under data dir


@dataclass
class RetrievedSource:
    """A retrieved chunk ready to be shown as a source citation.

    `image_path` is set ONLY for image_description chunks — it's the
    on-disk path (relative to the data dir) of the original extracted
    image. The frontend can fetch it via /api/documents/chunks/{id}/image
    to render a thumbnail in the source card.
    """

    document_id: str
    document_filename: str
    chunk_id: str
    text: str
    page_number: Optional[int]
    line_start: Optional[int]
    line_end: Optional[int]
    chunk_type: str
    score: float
    vector_sim: float
    bm25_score: float
    image_path: Optional[str] = None


# ---------------------------------------------------------------------------
# Progress reporting
# ---------------------------------------------------------------------------


@dataclass
class DigestProgress:
    """Progress event emitted during document digestion.

    The `stage` values match what the frontend displays. `percent` is 0-100
    and `details` is a short human-readable string (filename, page count,
    error, etc.).
    """

    stage: str  # "started" | "extracting_text" | "extracting_images"
    #           | "chunking" | "describing_images" | "embedding"
    #           | "persisting" | "done" | "error"
    percent: int
    details: str = ""
    # Filled in only on "done" — useful for the frontend to refresh its list
    document_id: Optional[str] = None
    total_chunks: int = 0
    total_images: int = 0


ProgressCallback = Callable[[DigestProgress], None]


def _noop_progress(_: DigestProgress) -> None:
    """Default no-op progress callback."""
    pass


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def get_data_dir() -> Path:
    """Resolve the data directory.

    Mirrors main.py._get_data_dir(): /app/data in Docker,
    backend/../data in dev.
    """
    candidates = [
        Path("/app/data"),
        Path(__file__).parent.parent.parent.parent / "data",
    ]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    fallback = Path("/app/data")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def get_documents_root() -> Path:
    """Root directory for all RAG document files."""
    root = get_data_dir() / settings.RAG_DOCUMENTS_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def get_document_dir(doc_id: uuid.UUID | str) -> Path:
    """Per-document subdirectory under the documents root."""
    return get_documents_root() / str(doc_id)


# ---------------------------------------------------------------------------
# MIME-type detection
# ---------------------------------------------------------------------------


def guess_mime_type(filename: str) -> str:
    """Best-effort MIME type from filename extension."""
    import mimetypes

    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def get_file_extension(filename: str) -> str:
    """Lowercase extension without the dot, e.g. 'pdf'."""
    return Path(filename).suffix.lower().lstrip(".")


# ---------------------------------------------------------------------------
# Text + image extraction — per-format extractors
# ---------------------------------------------------------------------------


def _extract_txt(buf: bytes) -> ExtractionResult:
    """Plain text — entire content becomes one page."""
    try:
        text = buf.decode("utf-8")
    except UnicodeDecodeError:
        text = buf.decode("latin-1", errors="replace")
    # Sanitize: strip NUL bytes and lone surrogates that would crash the
    # subsequent INSERT (PostgreSQL TEXT rejects 0x00).
    text = _sanitize_text_for_pg(text)
    return ExtractionResult(
        pages=[ExtractedPage(page_number=1, text=text)],
        mime_type="text/plain",
    )


def _extract_markdown(buf: bytes) -> ExtractionResult:
    """Markdown — treated as plain text (no rendering)."""
    return _extract_txt(buf)


def _extract_csv(buf: bytes) -> ExtractionResult:
    """CSV — kept as text. The chunker will split it sensibly."""
    try:
        text = buf.decode("utf-8")
    except UnicodeDecodeError:
        text = buf.decode("latin-1", errors="replace")
    text = _sanitize_text_for_pg(text)
    return ExtractionResult(
        pages=[ExtractedPage(page_number=1, text=text)],
        mime_type="text/csv",
    )


def _extract_xlsx(buf: bytes) -> ExtractionResult:
    """XLSX — flatten each sheet to CSV text, one page per sheet."""
    import openpyxl

    wb = openpyxl.load_workbook(io.BytesIO(buf), data_only=True)
    pages: List[ExtractedPage] = []
    for i, sheet_name in enumerate(wb.sheetnames, start=1):
        ws = wb[sheet_name]
        lines: List[str] = []
        for row in ws.iter_rows(values_only=True):
            row_str = ",".join("" if v is None else str(v) for v in row)
            lines.append(row_str)
        page_text = _sanitize_text_for_pg(f"# Sheet: {sheet_name}\n" + "\n".join(lines))
        pages.append(ExtractedPage(page_number=i, text=page_text))
    return ExtractionResult(
        pages=pages,
        mime_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


def _extract_docx(buf: bytes) -> ExtractionResult:
    """DOCX — text + embedded images.

    python-docx exposes paragraph runs and inline shapes. We concatenate
    paragraphs (preserving line breaks for chunker line numbering) and pull
    embedded images via the document's relationship part.
    """
    # NOTE: must import `Document` from the top-level `docx` package —
    # `from docx.document import Document` imports the *base class* which
    # requires a `part` arg and breaks on instantiation.
    from docx import Document as _DocxDocument

    doc = _DocxDocument(io.BytesIO(buf))

    # Text — all paragraphs in document order, one page (DOCX has no
    # native page concept). Sanitize each paragraph because python-docx
    # can emit NUL bytes for malformed XML.
    lines: List[str] = []

    for block in doc.iter_inner_content():
        if isinstance(block, Paragraph):
            value = _sanitize_text_for_pg(block.text)
            style = block.style.name if block.style else ""
            match = re.match(r"Heading (\d+)", style)
            lines.append(
                ("#" * min(6, int(match.group(1))) + " " if match else "") + value
            )
        else:
            for index, row in enumerate(block.rows):
                values = [
                    _sanitize_text_for_pg(cell.text)
                    .replace("|", "\\|")
                    .replace("\n", " ")
                    for cell in row.cells
                ]
                lines.append("| " + " | ".join(values) + " |")
                if index == 0:
                    lines.append("| " + " | ".join("---" for _ in values) + " |")
    full_text = "\n".join(lines)

    # Images — pull every image part referenced by the document.
    images: List[ExtractedImage] = []
    try:
        for rel in doc.part.rels.values():
            if "image" in rel.reltype:
                try:
                    blob = rel.target_part.blob
                    # Pick extension from the part's content type
                    content_type = rel.target_part.content_type or "image/png"
                    ext_map = {
                        "image/png": "PNG",
                        "image/jpeg": "JPEG",
                        "image/jpg": "JPEG",
                        "image/gif": "GIF",
                        "image/bmp": "BMP",
                        "image/tiff": "TIFF",
                        "image/webp": "WEBP",
                    }
                    fmt = ext_map.get(content_type, "PNG")
                    images.append(
                        ExtractedImage(page_number=1, image_bytes=blob, format_hint=fmt)
                    )
                except Exception as inner:
                    _log("docx image extract failed: %s", inner)
    except Exception as e:
        _log("docx image rels walk failed: %s", e)

    return ExtractionResult(
        pages=[ExtractedPage(page_number=1, text=full_text)],
        images=images,
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


def _extract_pdf(buf: bytes) -> ExtractionResult:
    """PDF — text page-by-page + embedded images via pdfplumber + pypdf.

    pdfplumber gives us page-level text (so citations can point at a page
    number). pypdf's image extraction is more reliable for pulling the
    actual image bytes; we use it as a fallback when pdfplumber's
    `.images` doesn't expose the raw stream cleanly.

    This function is SYNCHRONOUS and CPU-bound — it MUST be called via
    asyncio.to_thread() from the async digestion pipeline so it doesn't
    block the event loop.
    """
    import pdfplumber
    from pypdf import PdfReader

    pages: List[ExtractedPage] = []
    images: List[ExtractedImage] = []

    _log("pdf extract: starting (buf=%d bytes)", len(buf))

    # Text — pdfplumber. Sanitize EVERY page's text to strip NUL bytes
    # (0x00) — pdfplumber emits these for garbled PDF text streams and
    # PostgreSQL TEXT columns reject them, which crashed the entire
    # chunk INSERT in production.
    try:
        with pdfplumber.open(io.BytesIO(buf)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    page_text = page.extract_text() or ""
                except Exception as inner:
                    _log("pdfplumber page %d extract_text failed: %s", i, inner)
                    page_text = ""
                page_text = _sanitize_text_for_pg(page_text)
                pages.append(ExtractedPage(page_number=i, text=page_text))
            _log("pdf extract: pdfplumber got %d pages", len(pages))
    except Exception as e:
        _log("pdfplumber open failed: %s", e)
        # Fall back to pypdf for text-only
        try:
            reader = PdfReader(io.BytesIO(buf))
            for i, page in enumerate(reader.pages, start=1):
                try:
                    page_text = page.extract_text() or ""
                except Exception as inner:
                    _log("pypdf page %d extract_text failed: %s", i, inner)
                    page_text = ""
                page_text = _sanitize_text_for_pg(page_text)
                pages.append(ExtractedPage(page_number=i, text=page_text))
            _log("pdf extract: pypdf fallback got %d pages", len(pages))
        except Exception as inner2:
            _log("pypdf fallback failed: %s", inner2)
            raise

    # Images — pypdf. Walk every page's /XObject resources looking for
    # image XObjects, extract their streams.
    # Skip image extraction if the PDF is huge (>200 pages) to avoid
    # spending minutes on a document that's mostly text anyway. The
    # user can still get text-based citations; image citations are best
    # effort.
    if len(pages) > 200:
        _log(
            "pdf extract: skipping image walk for large PDF (%d pages > 200)",
            len(pages),
        )
    else:
        try:
            reader = PdfReader(io.BytesIO(buf))
            for page_idx, page in enumerate(reader.pages, start=1):
                try:
                    resources = page.get("/Resources")
                    if not resources:
                        continue
                    xobjects = resources.get("/XObject")
                    if not xobjects:
                        continue
                    xobjects = xobjects.get_object()
                    for name, ref in xobjects.items():
                        try:
                            obj = ref.get_object()
                            if obj.get("/Subtype") != "/Image":
                                continue
                            width = int(obj.get("/Width", 0))
                            height = int(obj.get("/Height", 0))
                            color_space = obj.get("/ColorSpace", "/DeviceRGB")
                            # bits = int(obj.get("/BitsPerComponent", 8))
                            filters = obj.get("/Filter", "")
                            if isinstance(filters, str):
                                filters = [filters]
                            else:
                                filters = [f for f in filters]

                            raw = obj.get_data()  # decoded bytes

                            # Convert raw → PIL image based on filter
                            from PIL import Image as PILImage

                            if "/DCTDecode" in filters:
                                # JPEG — raw bytes are already a JPEG stream
                                img = PILImage.open(io.BytesIO(raw))
                                fmt = "JPEG"
                            elif "/FlateDecode" in filters:
                                # Raw pixel data — need to reconstruct the image
                                cs_str = str(color_space)
                                if cs_str in ("/DeviceRGB", "/RGB"):
                                    mode = "RGB"
                                elif cs_str in ("/DeviceGray", "/G"):
                                    mode = "L"
                                elif cs_str in ("/DeviceCMYK", "/CMYK"):
                                    mode = "CMYK"
                                else:
                                    # Skip exotic color spaces — they're rare
                                    # in PDFs and PIL conversion gets hairy.
                                    _log(
                                        "pdf image skipped: colorspace=%s",
                                        cs_str,
                                    )
                                    continue
                                try:
                                    img = PILImage.frombytes(mode, (width, height), raw)
                                except Exception as inner:
                                    _log("pdf image frombytes failed: %s", inner)
                                    continue
                                fmt = "PNG"
                            else:
                                _log("pdf image skipped: filter=%s", filters)
                                continue

                            # Save as PNG/JPEG bytes for downstream processing
                            out_buf = io.BytesIO()
                            img.save(out_buf, format=fmt)
                            images.append(
                                ExtractedImage(
                                    page_number=page_idx,
                                    image_bytes=out_buf.getvalue(),
                                    format_hint=fmt,
                                )
                            )
                        except Exception as inner:
                            _log("pdf image extract one failed: %s", inner)
                            continue
                except Exception as inner:
                    _log("pdf page resources walk failed: %s", inner)
                    continue
            _log("pdf extract: image walk got %d images", len(images))
        except Exception as e:
            _log("pypdf image walk failed: %s", e)

    return ExtractionResult(pages=pages, images=images, mime_type="application/pdf")


def _extract_pptx(buf: bytes) -> ExtractionResult:
    """PPTX — slide + notes text via python-pptx, one page per slide.

    Walks every shape on every slide (text frames + tables) plus the
    speaker-notes pane, so presentations become searchable with per-slide
    page numbers for citations.
    """
    from pptx import Presentation

    prs = Presentation(io.BytesIO(buf))
    pages: List[ExtractedPage] = []
    for i, slide in enumerate(prs.slides, start=1):
        lines: List[str] = []
        try:
            for shape in slide.shapes:
                if getattr(shape, "has_text_frame", False) and shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        text = "".join(run.text for run in para.runs)
                        if text.strip():
                            lines.append(text)
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        cells = [cell.text.strip() for cell in row.cells]
                        lines.append(" | ".join(cells))
        except Exception as inner:
            _log("pptx shape walk failed on slide %d: %s", i, inner)
        try:
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text
                if notes.strip():
                    lines.append("[Notes] " + notes)
        except Exception as inner:
            _log("pptx notes read failed on slide %d: %s", i, inner)
        page_text = _sanitize_text_for_pg("\n".join(lines))
        pages.append(ExtractedPage(page_number=i, text=page_text))
    return ExtractionResult(
        pages=pages,
        mime_type="application/vnd.openxmlformats-officedocument.presentationml.presentation",
    )


# Dispatch table — extension → extractor
_EXTRACTORS = {
    "txt": _extract_txt,
    "md": _extract_markdown,
    "markdown": _extract_markdown,
    "csv": _extract_csv,
    "tsv": _extract_csv,
    "xlsx": _extract_xlsx,
    "xls": _extract_xlsx,
    "docx": _extract_docx,
    "pdf": _extract_pdf,
    "pptx": _extract_pptx,
}

# Legacy binary formats that must be converted to a modern format by
# LibreOffice before the Python extractors can read them.
_LEGACY_CONVERSIONS = {
    "doc": "docx",
    "xls": "xlsx",
    "ppt": "pptx",
}


def extract_content(buf: bytes, filename: str) -> ExtractionResult:
    """Dispatch to the right extractor based on filename extension.

    SYNCHRONOUS — caller MUST wrap in asyncio.to_thread() to avoid
    blocking the event loop. See extract_content_async().
    """
    ext = get_file_extension(filename)
    extractor = _EXTRACTORS.get(ext)
    if not extractor:
        # Unknown extension — try as plain text so at least something is
        # indexed. The user can see in the Brain page that the doc was
        # accepted; if extraction yields nothing usable, digestion will
        # create a single placeholder chunk.
        _log("no extractor for ext=%s, falling back to text", ext)
        return _extract_txt(buf)
    return extractor(buf)


async def extract_content_async(buf: bytes, filename: str) -> ExtractionResult:
    """Async entry point the digestion pipeline uses.

    Runs the CPU-bound synchronous extractors (pdfplumber, pypdf,
    openpyxl, python-docx, python-pptx) in a worker thread via
    asyncio.to_thread() so the FastAPI event loop keeps serving other
    requests. Legacy binary formats (.doc/.xls/.ppt — no Python reader)
    are first converted to their modern equivalent by LibreOffice when
    it is available; without LibreOffice they fall back to the plain-text
    path like any other unknown extension.
    """
    ext = get_file_extension(filename)
    target = _LEGACY_CONVERSIONS.get(ext)
    if target is not None:
        converted_buf, converted_name = await _convert_legacy_bytes(
            buf, filename, target
        )
        if converted_buf is not None:
            _log("legacy .%s converted to .%s for extraction", ext, target)
            buf, filename = converted_buf, converted_name
        else:
            _log(
                "legacy .%s could not be converted (LibreOffice missing or "
                "conversion failed) — falling back to text extraction",
                ext,
            )
    return await asyncio.to_thread(extract_content, buf, filename)


async def _convert_legacy_bytes(
    buf: bytes, filename: str, target_format: str
) -> tuple[Optional[bytes], str]:
    """Convert legacy office bytes (.doc/.xls/.ppt) to the modern format.

    Writes the bytes to a temp file, lets soffice convert it, and reads
    the result back. Returns (None, "") when LibreOffice is unavailable
    or the conversion fails. The temp dir is always cleaned up.
    """
    import shutil
    import tempfile

    from app.services.integrations import libreoffice

    if not libreoffice.is_available():
        return None, ""

    tmp_dir = Path(tempfile.mkdtemp(prefix="rag_legacy_"))
    try:
        src = tmp_dir / ("source" + os.path.splitext(filename)[1])
        src.write_bytes(buf)
        converted = await libreoffice.convert_legacy_document(
            src, target_format, tmp_dir
        )
        if converted is None or converted == src:
            return None, ""
        out_bytes = await asyncio.to_thread(converted.read_bytes)
        out_name = (os.path.splitext(filename)[0] or "document") + "." + target_format
        return out_bytes, out_name
    except Exception as e:
        _log("legacy conversion of %s failed: %s", filename, e)
        return None, ""
    finally:
        await asyncio.to_thread(shutil.rmtree, tmp_dir, True)


# ---------------------------------------------------------------------------
# Adaptive chunking
# ---------------------------------------------------------------------------


def _pick_chunk_size(total_chars: int) -> int:
    """Pick chunk size based on total document length.

    Short docs → small chunks (500 chars): better Q&A precision.
    Medium docs → medium chunks (1000 chars): balanced.
    Long docs → large chunks (2000 chars): less context fragmentation.
    """
    if total_chars < settings.RAG_SMALL_DOC_THRESHOLD:
        return settings.RAG_CHUNK_SIZE_SMALL
    if total_chars < settings.RAG_LARGE_DOC_THRESHOLD:
        return settings.RAG_CHUNK_SIZE_MEDIUM
    return settings.RAG_CHUNK_SIZE_LARGE


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+|\n{2,}")


def _split_into_sentences(text: str) -> List[str]:
    """Sentence-boundary-aware splitter.

    Splits on:
      - sentence-ending punctuation followed by whitespace
      - two or more consecutive newlines (paragraph breaks)

    Single newlines are preserved inside sentences so list items and
    short lines stay together.
    """
    if not text:
        return []
    parts = _SENTENCE_SPLIT_RE.split(text)
    return [p.strip() for p in parts if p.strip()]


def chunk_pages(
    pages: List[ExtractedPage],
    chunk_size: Optional[int] = None,
    overlap_ratio: Optional[float] = None,
) -> List[Chunk]:
    """Adaptive sentence-boundary chunker.

    Walks sentences and accumulates them into chunks of ~chunk_size chars.
    When a chunk fills, the last few sentences (covering ~overlap_ratio *
    chunk_size chars) carry over to seed the next chunk so context isn't
    lost at the boundary.

    Each chunk records its source page_number and the line_start/line_end
    of the original page text, for citation.

    Returns a flat list of Chunks with sequential chunk_index starting at 0.
    """
    if not pages:
        return []

    # Pick chunk size from total document length if not provided.
    total_chars = sum(len(p.text) for p in pages)
    if chunk_size is None:
        chunk_size = _pick_chunk_size(total_chars)
    if overlap_ratio is None:
        overlap_ratio = settings.RAG_CHUNK_OVERLAP_RATIO
    overlap_chars = int(chunk_size * overlap_ratio)

    chunks: List[Chunk] = []
    chunk_idx = 0

    for page in pages:
        sentences = _split_into_sentences(page.text)
        if not sentences:
            continue

        current_sentences: List[str] = []
        current_len = 0
        # Track the line offset of each sentence within the page so we can
        # compute line_start/line_end for the citation.
        sentence_line_ranges: List[Tuple[int, int]] = []
        line_cursor = 1  # 1-indexed lines within the page

        for sent in sentences:
            sent_line_count = sent.count("\n") + 1
            sent_len = len(sent)

            # If a single sentence is larger than chunk_size, hard-split it
            if sent_len > chunk_size:
                # Flush current chunk first
                if current_sentences:
                    text = " ".join(current_sentences)
                    ls, le = sentence_line_ranges[0][0], sentence_line_ranges[-1][1]
                    chunks.append(
                        Chunk(
                            text=text,
                            chunk_index=chunk_idx,
                            page_number=page.page_number,
                            line_start=ls,
                            line_end=le,
                        )
                    )
                    chunk_idx += 1
                    current_sentences = []
                    current_len = 0
                    sentence_line_ranges = []

                # Hard-split the long sentence
                for start in range(0, sent_len, chunk_size - overlap_chars):
                    piece = sent[start : start + chunk_size]
                    if not piece.strip():
                        continue
                    piece_lines = piece.count("\n") + 1
                    chunks.append(
                        Chunk(
                            text=piece,
                            chunk_index=chunk_idx,
                            page_number=page.page_number,
                            line_start=line_cursor,
                            line_end=line_cursor + piece_lines - 1,
                        )
                    )
                    chunk_idx += 1
                    line_cursor += piece_lines
                continue

            # Normal accumulation
            if current_len + sent_len + 1 > chunk_size and current_sentences:
                # Flush
                text = " ".join(current_sentences)
                ls, le = sentence_line_ranges[0][0], sentence_line_ranges[-1][1]
                chunks.append(
                    Chunk(
                        text=text,
                        chunk_index=chunk_idx,
                        page_number=page.page_number,
                        line_start=ls,
                        line_end=le,
                    )
                )
                chunk_idx += 1

                # Carry overlap sentences forward
                overlap_sentences: List[str] = []
                overlap_ranges: List[Tuple[int, int]] = []
                overlap_len = 0
                for s, r in zip(
                    reversed(current_sentences), reversed(sentence_line_ranges)
                ):
                    if overlap_len + len(s) > overlap_chars:
                        break
                    overlap_sentences.insert(0, s)
                    overlap_ranges.insert(0, r)
                    overlap_len += len(s) + 1
                current_sentences = overlap_sentences
                sentence_line_ranges = overlap_ranges
                current_len = sum(len(s) for s in current_sentences) + max(
                    0, len(current_sentences) - 1
                )

            current_sentences.append(sent)
            sentence_line_ranges.append(
                (line_cursor, line_cursor + sent_line_count - 1)
            )
            current_len += sent_len + (1 if current_len > 0 else 0)
            line_cursor += sent_line_count

        # Flush remaining
        if current_sentences:
            text = " ".join(current_sentences)
            ls, le = sentence_line_ranges[0][0], sentence_line_ranges[-1][1]
            chunks.append(
                Chunk(
                    text=text,
                    chunk_index=chunk_idx,
                    page_number=page.page_number,
                    line_start=ls,
                    line_end=le,
                )
            )
            chunk_idx += 1

    return chunks


# ---------------------------------------------------------------------------
# Vision LLM image description
# ---------------------------------------------------------------------------


def _normalize_image(image_bytes: bytes, max_dim: int) -> bytes:
    """Downscale an image so its longest side is ≤ max_dim.

    Returns PNG bytes. The vision LLM doesn't need ultra-high-res images
    and large inputs slow it down dramatically.

    SYNCHRONOUS — caller wraps in asyncio.to_thread via describe_image_with_vision.
    """
    from PIL import Image as PILImage

    img = PILImage.open(io.BytesIO(image_bytes))
    img = img.convert("RGB")
    w, h = img.size
    longest = max(w, h)
    if longest > max_dim:
        scale = max_dim / longest
        new_size = (max(1, int(w * scale)), max(1, int(h * scale)))
        img = img.resize(new_size, PILImage.LANCZOS)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


async def describe_image_with_vision(
    image_bytes: bytes,
    prompt: Optional[str] = None,
) -> str:
    """Send an image to the vision LLM and return its description.

    Returns an empty string on any failure (network, model not pulled,
    malformed response). The caller treats an empty description as
    "skip this image" — no chunk is created.

    The PIL image normalization runs in a thread pool (CPU-bound); the
    HTTP call to Ollama is async.
    """
    if not image_bytes:
        return ""

    # PIL normalization is CPU-bound — offload to thread pool.
    normalized = await asyncio.to_thread(
        _normalize_image, image_bytes, settings.RAG_VISION_IMAGE_MAX_DIM
    )
    b64 = base64.b64encode(normalized).decode("utf-8")

    try:
        model = settings.resolve_model(settings.RAG_VISION_MODEL_ROLE)
    except Exception as e:
        _log("vision model not resolved: %s", e)
        return ""

    default_prompt = (
        "Describe this image in detail. Focus on what is visible: objects, "
        "people, text, diagrams, charts, tables, scenes. If the image "
        "contains text, transcribe it verbatim. Be concise but complete — "
        "this description will be indexed for retrieval."
    )
    final_prompt = prompt or default_prompt

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{settings.OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": final_prompt,
                            "images": [b64],
                        }
                    ],
                    "stream": False,
                },
            )
            response.raise_for_status()
            data = response.json()
            description = data.get("message", {}).get("content", "").strip()
            if not description:
                _log("vision model returned empty description")
                return ""
            # Sanitize: vision models can occasionally emit NUL bytes or
            # lone surrogates that PostgreSQL TEXT rejects.
            description = _sanitize_text_for_pg(description)
            _log("vision model returned %d-char description", len(description))
            return description
    except Exception as e:
        _log("vision LLM call failed: %s", e)
        return ""


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _sha256(buf: bytes) -> str:
    return hashlib.sha256(buf).hexdigest()


async def save_uploaded_file(
    buf: bytes,
    filename: str,
    doc_id: uuid.UUID,
) -> str:
    """Save raw upload to disk and return the relative path.

    Files live at {data_dir}/documents/{doc_id}/{filename}. We return the
    path RELATIVE to the data dir so it's portable across host/container.

    The disk write runs in a thread pool (asyncio.to_thread) — even though
    Path.write_bytes is fast, on a slow disk or large file it could block
    the event loop long enough to delay requests.
    """

    def _write() -> str:
        doc_dir = get_document_dir(doc_id)
        doc_dir.mkdir(parents=True, exist_ok=True)
        # Sanitize filename — strip path separators, keep the basename.
        safe_name = os.path.basename(filename) or "upload"
        out_path = doc_dir / safe_name
        out_path.write_bytes(buf)
        return f"{settings.RAG_DOCUMENTS_DIR}/{doc_id}/{safe_name}"

    return await asyncio.to_thread(_write)


def save_image_file(
    image_bytes: bytes,
    doc_id: uuid.UUID,
    image_index: int,
    format_hint: str,
) -> str:
    """Save an extracted image to disk and return the relative path.

    SYNCHRONOUS — caller wraps in asyncio.to_thread when called from the
    async digestion pipeline.
    """
    doc_dir = get_document_dir(doc_id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    ext = "png" if format_hint.upper() in ("PNG",) else format_hint.lower()
    safe_name = f"image_{image_index:03d}.{ext}"
    out_path = doc_dir / safe_name
    out_path.write_bytes(image_bytes)
    return f"{settings.RAG_DOCUMENTS_DIR}/{doc_id}/{safe_name}"


async def save_image_file_async(
    image_bytes: bytes,
    doc_id: uuid.UUID,
    image_index: int,
    format_hint: str,
) -> str:
    """Async wrapper around save_image_file — runs in thread pool."""
    return await asyncio.to_thread(
        save_image_file, image_bytes, doc_id, image_index, format_hint
    )


def resolve_document_path(relative_path: str) -> Path:
    """Convert a stored relative path back to an absolute filesystem path."""
    return get_data_dir() / relative_path


# ---------------------------------------------------------------------------
# Public API: digest
# ---------------------------------------------------------------------------


async def digest_document(
    db: AsyncSession,
    *,
    file_bytes: bytes,
    filename: str,
    scope: str,  # "private" | "public"
    conversation_id: Optional[uuid.UUID] = None,
    message_id: Optional[uuid.UUID] = None,
    progress: ProgressCallback = _noop_progress,
    reuse_doc_id: Optional[uuid.UUID] = None,
) -> Document:
    """Digest an uploaded file into searchable chunks.

    ASYNC & NON-BLOCKING — all blocking I/O (PDF/DOCX/XLSX extraction,
    chunking, PIL image normalization, file writes) runs in a thread
    pool via asyncio.to_thread() so the FastAPI event loop can keep
    serving other requests while a large PDF is being processed.

    REINDEX MODE — when `reuse_doc_id` is provided, the document row with
    that ID is re-digested IN PLACE: its chunks are deleted first, the
    row is flipped back to "digesting", and the raw file on disk is
    reused (no re-save, same ID, same scope/conversation/collections).
    This is what POST /documents/{id}/reindex/stream uses.

    DB SESSION DISCIPLINE — the caller's `db` session is used ONLY for
    the initial doc-row insert. After that commit, the session is
    released back to the pool and we hold NO DB connection during the
    long CPU-bound extraction/chunking/embedding phases. A fresh
    session is opened only when we actually need to write chunks back.
    This is critical: the previous implementation held a single
    connection for the entire 30+ second digestion, which under load
    exhausted the pool (size 5+10) and made all subsequent requests,
    etc. queue up waiting for a connection.

    Stages (each emits a DigestProgress callback so the frontend can
    show real-time feedback):
      started → extracting_text → extracting_images → chunking →
      describing_images → embedding → persisting → done

    Chunk persistence is BATCHED (10 chunks per INSERT) so that even
    if one batch fails (defensive — sanitization should prevent this),
    the other batches still land.

    On any unrecoverable error, marks the document row as "failed"
    using a FRESH session (the original session is poisoned after the
    failed commit) and re-raises.
    """
    t0 = time.time()
    doc_id = reuse_doc_id or uuid.uuid4()
    safe_scope = scope if scope in ("private", "public") else "private"
    mime = guess_mime_type(filename)

    # ── Content-hash dedup ──────────────────────────────────────────────
    # If a document with the same content_hash already exists with the
    # same scope (and same conversation_id for private docs), skip
    # re-digestion and return the existing document. This prevents
    # re-embedding a 200-page PDF that was already uploaded.
    content_hash = _sha256(file_bytes)
    if reuse_doc_id is None:
        try:
            dedup_stmt = select(Document).where(Document.content_hash == content_hash)
            if safe_scope == "private" and conversation_id:
                dedup_stmt = dedup_stmt.where(
                    Document.scope == "private",
                    Document.conversation_id == conversation_id,
                )
            else:
                dedup_stmt = dedup_stmt.where(Document.scope == safe_scope)
            dedup_result = await db.execute(dedup_stmt)
            existing_doc = dedup_result.scalar_one_or_none()
            if existing_doc and existing_doc.digestion_status == "ready":
                _log(
                    "digest: SKIP — content_hash %s already digested as doc %s",
                    content_hash[:12],
                    existing_doc.id,
                )
                progress(
                    DigestProgress(
                        stage="done",
                        percent=100,
                        details=f"Duplicate of existing document: {existing_doc.filename}",
                        document_id=str(existing_doc.id),
                        total_chunks=existing_doc.total_chunks,
                        total_images=existing_doc.total_images,
                    )
                )
                return existing_doc
        except Exception as e:
            _log("digest: dedup check failed (non-fatal): %s", e)

    _log(
        "digest_document START  filename=%s  size=%d  scope=%s  conv=%s  doc_id=%s",
        filename,
        len(file_bytes),
        safe_scope,
        conversation_id,
        doc_id,
    )

    # ── Phase 1: Create the doc row + save raw file (uses caller's db) ──
    # After this commit, we RELEASE the caller's session so its
    # connection goes back to the pool. All subsequent phases either
    # need no DB (extraction/chunking/embedding) or open a fresh
    # session (chunk persistence, error marking).
    if reuse_doc_id is not None:
        # REINDEX MODE — reuse the existing row in place: delete old
        # chunks, reset counters, flip back to "digesting". The raw file
        # on disk is reused as-is (no re-save); scope / conversation /
        # collections are preserved.
        await db.execute(
            delete(DocumentChunk).where(DocumentChunk.document_id == doc_id)
        )
        await db.execute(
            update(Document)
            .where(Document.id == doc_id)
            .values(
                filename=os.path.basename(filename),
                original_filename=os.path.basename(filename),
                mime_type=mime,
                file_size_bytes=len(file_bytes),
                content_hash=content_hash,
                total_chunks=0,
                total_images=0,
                digestion_status="digesting",
                digestion_error=None,
            )
        )
        await db.commit()
        _log("digest: reindex reset for doc %s (chunks dropped)", doc_id)
        rel_path = None  # reuse the on-disk file — resolved below
    else:
        doc = Document(
            id=doc_id,
            filename=os.path.basename(filename),
            original_filename=os.path.basename(filename),
            mime_type=mime,
            file_path="",  # filled in after save
            file_size_bytes=len(file_bytes),
            content_hash=content_hash,  # reuse the hash computed for dedup
            scope=safe_scope,
            conversation_id=conversation_id if safe_scope == "private" else None,
            message_id=message_id,
            digestion_status="digesting",
        )
        db.add(doc)
        await db.commit()
        await db.refresh(doc)
        _log("digest: created doc row id=%s (session held briefly)", doc_id)

    progress(DigestProgress(stage="started", percent=0, details=filename))

    try:
        if reuse_doc_id is not None:
            # Reuse the existing on-disk raw file — its stored rel_path.
            row = (
                await db.execute(
                    select(Document.file_path).where(Document.id == doc_id)
                )
            ).first()
            if row is None or not row[0]:
                raise RuntimeError(
                    f"Reindex failed: document {doc_id} has no stored file path"
                )
            rel_path = row[0]
            _log("digest: reindex reusing on-disk file %s", rel_path)
        else:
            # ── 1. Save raw file to disk (THREAD POOL — disk I/O) ──────
            t1 = time.time()
            rel_path = await save_uploaded_file(file_bytes, filename, doc_id)

            # Update the doc row with the file path. Use a FRESH session
            # so we don't pin the caller's connection while we go off and
            # do CPU-bound work next.
            async with async_session_factory() as db2:
                stmt = (
                    Document.__table__.update()
                    .where(Document.id == doc_id)
                    .values(file_path=rel_path)
                )
                await db2.execute(stmt)
                await db2.commit()
            _log(
                "digest: saved raw file in %.2fs (db session released)",
                time.time() - t1,
            )
        # The caller's `db` session is now free to be returned to the
        # pool by the caller's `async with` block — we don't touch it
        # again until the very end (and even then, we prefer fresh
        # sessions for the chunk persistence phase).

        # ── 2. Extract text + images (THREAD POOL — CPU-bound) ──────────
        # NO DB SESSION HELD during this phase. This is the key fix for
        # the "backend hangs on large PDF" issue — previously the
        # session was pinned here for 10-30 seconds on a big PDF,
        # starving the connection pool.
        progress(
            DigestProgress(
                stage="extracting_text",
                percent=10,
                details=f"Extracting text from {filename}",
            )
        )
        t1 = time.time()
        # document_extraction imports this module's extraction adapters.
        # Keep this import lazy to avoid a circular module initialization.
        from app.services.document_extraction import extract, save_snapshot

        extraction, snapshot = await extract(file_bytes, filename)
        try:
            await asyncio.to_thread(
                save_snapshot, get_document_dir(doc_id) / "extraction.json", snapshot
            )
        except OSError:
            logger.exception(
                "Extraction cache unavailable; artifact reads can reconstruct it"
            )
        _log(
            "digest: extraction done in %.2fs  pages=%d  images=%d",
            time.time() - t1,
            len(extraction.pages),
            len(extraction.images),
        )
        if not extraction.pages or all(not p.text.strip() for p in extraction.pages):
            # No text at all — create a single placeholder chunk so the doc
            # is at least retrievable by filename.
            _log("digest: no text extracted from %s, using placeholder", filename)
            extraction.pages = [
                ExtractedPage(
                    page_number=1,
                    text=f"[Document: {filename} — no extractable text]",
                )
            ]

        progress(
            DigestProgress(
                stage="extracting_images",
                percent=20,
                details=(
                    f"Extracted {len(extraction.pages)} page(s), {len(extraction.images)} image(s)"
                ),
            )
        )

        # ── 3. Chunk text (THREAD POOL — CPU-bound on huge docs) ────────
        progress(
            DigestProgress(
                stage="chunking",
                percent=30,
                details=f"Chunking {sum(len(p.text) for p in extraction.pages)} chars",
            )
        )
        t1 = time.time()
        text_chunks = await asyncio.to_thread(chunk_pages, extraction.pages)
        _log(
            "digest: chunking done in %.2fs  %d chunks from %d pages",
            time.time() - t1,
            len(text_chunks),
            len(extraction.pages),
        )

        # ── 4. Describe images with vision LLM (async HTTP) ─────────────
        image_chunks: List[Chunk] = []
        if extraction.images:
            progress(
                DigestProgress(
                    stage="describing_images",
                    percent=40,
                    details=f"Describing {len(extraction.images)} image(s) with vision model",
                )
            )
            next_chunk_idx = len(text_chunks)
            for i, img in enumerate(extraction.images):
                pct = 40 + int(30 * (i / max(1, len(extraction.images))))
                progress(
                    DigestProgress(
                        stage="describing_images",
                        percent=pct,
                        details=f"Image {i + 1}/{len(extraction.images)}",
                    )
                )
                t1 = time.time()
                description = await describe_image_with_vision(img.image_bytes)
                _log(
                    "digest: vision describe image %d/%d in %.2fs  desc_len=%d",
                    i + 1,
                    len(extraction.images),
                    time.time() - t1,
                    len(description),
                )
                if not description:
                    _log(
                        "digest: image %d of %s yielded no description, skipping",
                        i,
                        filename,
                    )
                    continue
                # Persist the image bytes to disk so the user can download
                # it later from the Brain page.
                image_rel = await save_image_file_async(
                    img.image_bytes,
                    doc_id,
                    i,
                    img.format_hint,
                )
                image_chunks.append(
                    Chunk(
                        text=(
                            f"[Image on page {img.page_number} of {filename}]\n{description}"
                        ),
                        chunk_index=next_chunk_idx,
                        page_number=img.page_number,
                        line_start=None,
                        line_end=None,
                        chunk_type="image_description",
                        image_path=image_rel,
                    )
                )
                next_chunk_idx += 1
        else:
            progress(
                DigestProgress(
                    stage="describing_images",
                    percent=70,
                    details="No images to describe",
                )
            )

        all_chunks = text_chunks + image_chunks
        if not all_chunks:
            # Should be unreachable (we always create ≥1 page above), but
            # be defensive.
            raise RuntimeError("No chunks produced from document")

        # ── 5. Embed all chunks (async HTTP to Ollama) ──────────────────
        progress(
            DigestProgress(
                stage="embedding",
                percent=75,
                details=f"Embedding {len(all_chunks)} chunk(s)",
            )
        )
        t1 = time.time()
        embeddings = await get_embeddings([c.text for c in all_chunks])
        _log("digest: embedded %d chunks in %.2fs", len(all_chunks), time.time() - t1)
        for chunk, emb in zip(all_chunks, embeddings):
            chunk._embedding = emb  # type: ignore[attr-defined]

        # ── 6. Persist chunks in BATCHES (fresh session, 10 per INSERT) ─
        # Batching is defensive: if one batch fails (e.g. a sanitization
        # edge case slips through), the other batches still land and the
        # doc remains searchable. Each batch uses its own session so a
        # failure in batch N doesn't poison batch N+1.
        progress(
            DigestProgress(
                stage="persisting",
                percent=90,
                details=f"Saving {len(all_chunks)} chunk(s) to database",
            )
        )

        # Defense-in-depth: sanitize every chunk's text right before
        # building the ORM row. The extractors already sanitize, but
        # this catches anything that might have slipped through (e.g.
        # a vision model that emits a NUL byte despite our sanitize
        # call on its output — defensive code is cheap, crashes are
        # expensive).
        BATCH_SIZE = 10
        total_persisted = 0
        t1 = time.time()
        for batch_start in range(0, len(all_chunks), BATCH_SIZE):
            batch = all_chunks[batch_start : batch_start + BATCH_SIZE]
            chunk_rows: List[DocumentChunk] = []
            for c in batch:
                row = DocumentChunk(
                    document_id=doc_id,
                    chunk_index=c.chunk_index,
                    text=_sanitize_text_for_pg(c.text),  # defense-in-depth
                    page_number=c.page_number,
                    line_start=c.line_start,
                    line_end=c.line_end,
                    chunk_type=c.chunk_type,
                    image_path=c.image_path,
                    embedding=getattr(c, "_embedding", None),  # type: ignore[attr-defined]
                )
                chunk_rows.append(row)
            try:
                async with async_session_factory() as db_batch:
                    db_batch.add_all(chunk_rows)
                    await db_batch.commit()
                total_persisted += len(chunk_rows)
                _log(
                    "digest: persisted batch %d-%d/%d",
                    batch_start,
                    batch_start + len(batch),
                    len(all_chunks),
                )
            except Exception as batch_err:
                # Log and continue — partial persistence is better than
                # total failure. The doc will still be searchable with
                # whatever chunks made it through.
                _log(
                    "digest: batch %d-%d FAILED (continuing): %s",
                    batch_start,
                    batch_start + len(batch),
                    batch_err,
                )
        _log(
            "digest: persisted %d/%d chunks in %.2fs",
            total_persisted,
            len(all_chunks),
            time.time() - t1,
        )

        # ── 7. Update document row with final stats (fresh session) ────
        async with async_session_factory() as db3:
            stmt = (
                Document.__table__.update()
                .where(Document.id == doc_id)
                .values(
                    total_pages=len(extraction.pages) if extraction.pages else None,
                    total_chunks=len(all_chunks),
                    total_images=len(image_chunks),
                    digestion_status="ready",
                    digestion_error=None,
                )
            )
            await db3.execute(stmt)
            await db3.commit()

        # Re-fetch the doc for the return value (so the caller gets a
        # fully-populated Document object).
        async with async_session_factory() as db4:
            result = await db4.execute(select(Document).where(Document.id == doc_id))
            doc = result.scalar_one_or_none()
            if doc is None:
                # Shouldn't happen, but be defensive
                raise RuntimeError(f"Document {doc_id} vanished after digestion")

        progress(
            DigestProgress(
                stage="done",
                percent=100,
                details=(
                    f"Digested {filename}: {len(text_chunks)} text chunks, "
                    f"{len(image_chunks)} image chunks"
                ),
                document_id=str(doc_id),
                total_chunks=len(all_chunks),
                total_images=len(image_chunks),
            )
        )
        _log(
            (
                "digest_document DONE  filename=%s  doc_id=%s  text_chunks=%d  "
                "image_chunks=%d  total_time=%.2fs"
            ),
            filename,
            doc_id,
            len(text_chunks),
            len(image_chunks),
            time.time() - t0,
        )
        return doc

    except Exception as e:
        _log(
            "digest_document FAILED  filename=%s  doc_id=%s  error=%s  after %.2fs",
            filename,
            doc_id,
            e,
            time.time() - t0,
        )
        # Mark the doc as failed using a FRESH session. The caller's
        # session is likely poisoned (rolled back due to the exception
        # during flush), so we can't reuse it. The original production
        # crash showed this exact failure mode:
        #   "This Session's transaction has been rolled back due to a
        #    previous exception during flush. To begin a new transaction
        #    with this Session, first issue Session.rollback()."
        # Using a brand-new session sidesteps the whole problem.
        try:
            async with async_session_factory() as db_err:
                stmt = (
                    Document.__table__.update()
                    .where(Document.id == doc_id)
                    .values(
                        digestion_status="failed",
                        digestion_error=_sanitize_text_for_pg(str(e))[:2000],
                    )
                )
                await db_err.execute(stmt)
                await db_err.commit()
            _log("digest: marked doc %s as failed", doc_id)
        except Exception as inner:
            _log("failed to mark doc as failed: %s", inner)
            # Last-resort: try to rollback the caller's session so it
            # doesn't leak a poisoned transaction back to the pool.
            try:
                await db.rollback()
            except Exception:
                pass

        progress(
            DigestProgress(
                stage="error",
                percent=100,
                details=f"Digestion failed: {e}",
                document_id=str(doc_id),
            )
        )
        raise


# ---------------------------------------------------------------------------
# Public API: search (per-doc adaptive hybrid retrieval)
# ---------------------------------------------------------------------------


async def search_documents(
    db: AsyncSession,
    query: str,
    *,
    conversation_id: Optional[uuid.UUID] = None,
    top_k_per_doc: Optional[int] = None,
    top_k_total: Optional[int] = None,
    similarity_cutoff: Optional[float] = None,
) -> List[RetrievedSource]:
    """Hybrid vector + BM25 search with per-doc adaptive top-k.

    IMPROVEMENTS OVER PREVIOUS IMPLEMENTATION:
    - Query expansion: expands the query with synonyms before embedding
      (cheap, no LLM call). Improves recall for short queries.
    - Larger candidate pool: fetches RAG_CANDIDATE_POOL (60) vector
      candidates instead of 30, AND separately fetches keyword-only
      matches via tsvector (UNION). Previously only 30 vector candidates
      were BM25-scored, biasing toward vector similarity and hiding
      keyword-only matches.
    - LLM reranking: optionally reranks the top-N chunks with a small LLM
      call. Costs one extra inference but meaningfully reorders results.

    Scope filter:
      - public                                → any conversation
      - private AND conversation_id = ?       → only that conversation

    Returns at most top_k_total chunks, with at most top_k_per_doc chunks
    per document. Sorted by hybrid score (or reranked score if reranking).
    """
    if not query or not query.strip():
        return []

    k_per_doc = top_k_per_doc or settings.RAG_TOP_K_PER_DOC
    k_total = top_k_total or settings.RAG_TOP_K_TOTAL
    cutoff = (
        similarity_cutoff
        if similarity_cutoff is not None
        else settings.RAG_SIMILARITY_CUTOFF
    )

    # ── Query expansion ─────────────────────────────────────────────────
    expanded_query = query
    if settings.RAG_QUERY_EXPANSION:
        expanded_query = _expand_query(query)

    # Embed the (expanded) query
    q_emb = await get_embedding(expanded_query)
    if not q_emb:
        _log("query embedding failed, returning no results")
        return []

    # ── Step 1: pgvector cosine search — larger candidate pool ──────────
    fetch_k = max(settings.RAG_CANDIDATE_POOL, k_total * 3)
    distance_expr = DocumentChunk.embedding.cosine_distance(q_emb)

    stmt = (
        select(
            DocumentChunk.id,
            DocumentChunk.document_id,
            DocumentChunk.text,
            DocumentChunk.page_number,
            DocumentChunk.line_start,
            DocumentChunk.line_end,
            DocumentChunk.chunk_type,
            DocumentChunk.image_path,
            Document.filename.label("doc_filename"),
            DocumentChunk.embedding.cosine_distance(q_emb).label("distance"),
        )
        .join(Document, Document.id == DocumentChunk.document_id)
        .where(Document.digestion_status == "ready")
        .where(DocumentChunk.embedding.isnot(None))
        .order_by(distance_expr)
        .limit(fetch_k)
    )

    # Apply scope filter
    if conversation_id:
        stmt = stmt.where(
            or_(
                Document.scope == "public",
                and_(
                    Document.scope == "private",
                    Document.conversation_id == conversation_id,
                ),
            )
        )
    else:
        stmt = stmt.where(Document.scope == "public")

    try:
        result = await db.execute(stmt)
        vector_rows = result.all()
    except Exception as e:
        _log("vector search query failed: %s", e)
        return []

    if not vector_rows:
        _log("no candidates found for query: %s", query[:80])
        return []

    # ── Step 2: BM25 keyword search — catch keyword-only matches ────────
    # Previously BM25 was only scored on the vector candidates, so a chunk
    # with great keyword overlap but mediocre vector similarity never got
    # a chance. Now we run a separate tsvector query to fetch keyword-only
    # candidates and merge them with the vector candidates.
    chunk_ids = [r.id for r in vector_rows]  # noqa
    bm25_scores: Dict[str, float] = {}
    keyword_only_rows: Dict[str, Any] = {}  # chunk_id_str -> row-like dict

    try:
        # Build a tsquery from query tokens — OR them so any match contributes.
        tokens = re.findall(r"\w+", query.lower())
        tokens = [t for t in tokens if len(t) >= 2]
        if tokens:
            tsquery = " | ".join(f"'{t}'" for t in tokens)
            # Fetch keyword matches that AREN'T already in the vector set
            # (those we already have). We also get their text + metadata
            # so we can include them as candidates.
            scope_clause = ""
            params = {"q": tsquery, "lim": fetch_k}
            if conversation_id:
                scope_clause = (
                    "AND (d.scope = 'public' OR "
                    "(d.scope = 'private' AND d.conversation_id = CAST(:cid AS uuid)))"
                )
                params["cid"] = str(conversation_id)
            else:
                scope_clause = "AND d.scope = 'public'"

            kw_sql = text(f"""
                SELECT dc.id, dc.document_id, dc.text, dc.page_number,
                       dc.line_start, dc.line_end, dc.chunk_type, dc.image_path,
                       d.filename AS doc_filename,
                       ts_rank_cd(dc.search_vector, to_tsquery('simple', :q)) AS rank
                FROM document_chunks dc
                JOIN documents d ON d.id = dc.document_id
                WHERE dc.search_vector @@ to_tsquery('simple', :q)
                  AND d.digestion_status = 'ready'
                  {scope_clause}
                ORDER BY rank DESC
                LIMIT :lim
            """).bindparams(**params)
            kw_result = await db.execute(kw_sql)
            for row in kw_result:
                cid_str = str(row[0])
                bm25_scores[cid_str] = float(row[9] or 0.0)
                # If this chunk isn't in the vector results, add it as a
                # keyword-only candidate (with distance=None so vec_sim=0).
                if cid_str not in {str(r.id) for r in vector_rows}:
                    keyword_only_rows[cid_str] = row

            # Also score the vector candidates that match keywords
            existing_ids = {str(r.id) for r in vector_rows}
            if existing_ids:
                id_list = ",".join(f"'{cid}'" for cid in existing_ids)
                rank_sql = text(f"""
                    SELECT id,
                           ts_rank_cd(search_vector, to_tsquery('simple', :q)) AS rank
                    FROM document_chunks
                    WHERE search_vector @@ to_tsquery('simple', :q)
                      AND id IN ({id_list})
                """).bindparams(q=tsquery)
                rank_result = await db.execute(rank_sql)
                for row in rank_result:
                    bm25_scores[str(row[0])] = float(row[1] or 0.0)
    except Exception as e:
        _log("BM25 query failed, using vector-only: %s", e)
        bm25_scores = {}

    # ── Step 3: merge vector + keyword-only candidates ──────────────────
    # Build a unified candidate list. Vector candidates have a distance;
    # keyword-only candidates have distance=None (vec_sim=0).
    all_candidates: List[Tuple[float, float, float, Any]] = []

    w_vec = settings.RAG_RETRIEVAL_VECTOR_WEIGHT
    w_bm25 = settings.RAG_RETRIEVAL_BM25_WEIGHT

    # Normalize BM25 to 0..1 across the candidate set
    max_bm25 = max(bm25_scores.values()) if bm25_scores else 0.0
    if max_bm25 <= 0:
        max_bm25 = 6.0  # default normalizer (matches memory system)

    # Vector candidates
    for r in vector_rows:
        vec_sim = 1.0 - (r.distance or 1.0)
        if vec_sim < cutoff and bm25_scores.get(str(r.id), 0.0) == 0:
            continue  # below cutoff AND no keyword match — skip
        bm25_raw = bm25_scores.get(str(r.id), 0.0)
        bm25_norm = min(bm25_raw / max_bm25, 1.0)
        score = w_vec * vec_sim + w_bm25 * bm25_norm
        all_candidates.append((score, vec_sim, bm25_norm, r))

    # Keyword-only candidates (not in vector results)
    for cid_str, row in keyword_only_rows.items():
        bm25_raw = bm25_scores.get(cid_str, 0.0)
        bm25_norm = min(bm25_raw / max_bm25, 1.0)
        vec_sim = 0.0  # no vector match
        score = w_bm25 * bm25_norm  # keyword-only score
        all_candidates.append((score, vec_sim, bm25_norm, row))

    all_candidates.sort(key=lambda x: x[0], reverse=True)

    # ── Step 4: per-doc selection ───────────────────────────────────────
    per_doc_count: Dict[str, int] = {}
    final: List[RetrievedSource] = []
    for score, vec_sim, bm25_norm, r in all_candidates:
        doc_id_str = str(r.document_id)
        if per_doc_count.get(doc_id_str, 0) >= k_per_doc:
            continue
        per_doc_count[doc_id_str] = per_doc_count.get(doc_id_str, 0) + 1
        final.append(
            RetrievedSource(
                document_id=doc_id_str,
                document_filename=r.doc_filename,
                chunk_id=str(r.id),
                text=r.text,
                page_number=r.page_number,
                line_start=r.line_start,
                line_end=r.line_end,
                chunk_type=r.chunk_type,
                score=round(score, 4),
                vector_sim=round(vec_sim, 4),
                bm25_score=round(bm25_norm, 4),
                image_path=r.image_path,
            )
        )
        if len(final) >= max(k_total, settings.RAG_RERANK_TOP_N):
            break

    # ── Step 5: LLM reranking (optional) ────────────────────────────────
    if settings.RAG_LLM_RERANK and len(final) > 1:
        final = await _rerank_with_llm(query, final, k_total)

    final = final[:k_total]

    _log(
        "search '%s' → %d vector + %d keyword candidates, %d returned "
        "(cutoff=%s, k_per_doc=%d, k_total=%d, rerank=%s)",
        query[:60],
        len(vector_rows),
        len(keyword_only_rows),
        len(final),
        cutoff,
        k_per_doc,
        k_total,
        settings.RAG_LLM_RERANK,
    )
    return final


# ── Query expansion ────────────────────────────────────────────────────


# Lightweight synonym map for common query terms. This is NOT a full
# wordnet-style expansion — just catches obvious synonyms that small
# models can't reformulate themselves. Kept short to avoid query bloat.
_QUERY_SYNONYMS: Dict[str, List[str]] = {
    "how": ["way", "method", "approach"],
    "what": ["definition", "description", "explain"],
    "why": ["reason", "cause", "because"],
    "problem": ["issue", "error", "bug", "failure"],
    "fix": ["solve", "resolve", "repair", "correct"],
    "create": ["make", "build", "generate", "produce"],
    "delete": ["remove", "drop", "destroy", "erase"],
    "update": ["modify", "change", "edit", "alter"],
    "find": ["locate", "search", "discover", "identify"],
    "start": ["begin", "launch", "initiate", "commence"],
    "stop": ["end", "halt", "terminate", "cease"],
    "fast": ["quick", "speedy", "rapid", "efficient"],
    "big": ["large", "huge", "major", "significant"],
    "small": ["little", "minor", "tiny", "compact"],
    "good": ["best", "optimal", "recommended", "preferred"],
    "bad": ["poor", "worst", "broken", "faulty"],
}


def _expand_query(query: str) -> str:
    """Expand a query with synonyms (cheap, no LLM call).

    Tokenizes the query, looks up each token in the synonym map, and
    appends synonyms to the query. The original tokens are kept first
    (so the embedding still matches the original intent) and synonyms
    are appended (so the embedding also catches paraphrases).

    Example: "how to fix the problem" → "how to fix the problem way method
    approach solve resolve repair correct issue error bug failure"
    """
    if not query or not query.strip():
        return query
    tokens = re.findall(r"\w+", query.lower())
    expansions: List[str] = []
    for tok in tokens:
        syns = _QUERY_SYNONYMS.get(tok)
        if syns:
            expansions.extend(syns[:2])  # cap at 2 synonyms per token
    if not expansions:
        return query
    # Keep original query first, append expansions (deduped)
    seen = set(query.lower().split())
    unique_expansions = [s for s in expansions if s not in seen][:6]
    if not unique_expansions:
        return query
    return query + " " + " ".join(unique_expansions)


# ── LLM reranking ──────────────────────────────────────────────────────


async def _rerank_with_llm(
    query: str,
    sources: List["RetrievedSource"],
    final_k: int,
) -> List["RetrievedSource"]:
    """Rerank retrieved sources with a small LLM call.

    Sends the query + top-N chunk texts to the utility model and asks it
    to rank them by relevance. Falls back to the original order on any
    failure (reranking is a nice-to-have, not critical).

    Uses the default_utility model (falls back to chat model) with a
    short timeout — if the LLM is slow or fails, we keep the hybrid-score
    ordering.
    """
    if not sources or len(sources) <= 1:
        return sources

    try:
        # Document-reasoning task slot (Settings ▸ AI ▸ Models)
        model = await model_prefs.resolve_task_model("document_reasoning")
        if not model:
            return sources

        # Build the excerpts list (truncate each chunk to keep payload small)
        excerpts_lines: List[str] = []
        for i, s in enumerate(sources[: settings.RAG_RERANK_TOP_N], start=1):
            text = (s.text or "")[:300]
            excerpts_lines.append(f"{i}. {text}")
        excerpts = "\n".join(excerpts_lines)

        user_msg = (
            f"Query: {query}\n\nExcerpts:\n{excerpts}\n\n"
            f"Return the JSON array of indices, most relevant first."
        )

        data = await providers.chat_once(
            model,
            [
                {"role": "system", "content": get_prompt("rag_rerank_system")},
                {"role": "user", "content": user_msg},
            ],
            think=False,
            format="json",
            options={"num_predict": 256},
            timeout=30.0,
        )
        raw = data.get("message", {}).get("content", "").strip()

        # Parse the JSON array of indices
        import json as _json

        # Strip markdown fences / <think> tags
        raw_clean = raw
        if raw_clean.startswith("```"):
            raw_clean = raw_clean.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        start = raw_clean.find("[")
        end = raw_clean.rfind("]")
        if start < 0 or end <= start:
            return sources
        order = _json.loads(raw_clean[start : end + 1])
        if not isinstance(order, list):
            return sources

        # Reorder sources by the LLM's ranking
        reranked: List["RetrievedSource"] = []
        used = set()
        for idx in order:
            if isinstance(idx, int) and 1 <= idx <= len(sources):
                src = sources[idx - 1]
                if id(src) not in used:
                    reranked.append(src)
                    used.add(id(src))
        # Append any sources the LLM didn't rank
        for s in sources:
            if id(s) not in used:
                reranked.append(s)

        _log("LLM rerank: reordered %d sources", len(reranked))
        return reranked[:final_k] if final_k else reranked

    except Exception as e:
        _log("LLM rerank failed (keeping hybrid order): %s", e)
        return sources


def format_sources_for_llm(sources: List[RetrievedSource]) -> str:
    """Format retrieved chunks as a string to feed back to the LLM.

    Each chunk is rendered as:
        [Source 1: filename, page X, lines Y-Z]
        <text>
        ---
    The LLM uses these to answer the user's question and can reference
    sources by their [Source N] number.
    """
    if not sources:
        return "No relevant document chunks found."
    lines: List[str] = []
    for i, s in enumerate(sources, start=1):
        location_parts: List[str] = [s.document_filename]
        location_parts.append(f"document_id {s.document_id}")
        location_parts.append(f"chunk_id {s.chunk_id}")
        if s.page_number is not None:
            location_parts.append(f"page {s.page_number}")
        if s.line_start is not None and s.line_end is not None:
            if s.line_start == s.line_end:
                location_parts.append(f"line {s.line_start}")
            else:
                location_parts.append(f"lines {s.line_start}-{s.line_end}")
        elif s.line_start is not None:
            location_parts.append(f"line {s.line_start}")
        location = ", ".join(location_parts)
        lines.append(f"[Source {i}: {location}]")
        lines.append(s.text)
        lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API: list / get / delete / rename / toggle scope
# ---------------------------------------------------------------------------


async def list_documents(
    db: AsyncSession,
    *,
    scope: Optional[str] = None,
    conversation_id: Optional[uuid.UUID] = None,
) -> List[Document]:
    """List documents, optionally filtered by scope and/or conversation."""
    stmt = select(Document).order_by(Document.created_at.desc())
    if scope:
        stmt = stmt.where(Document.scope == scope)
    if conversation_id:
        stmt = stmt.where(Document.conversation_id == conversation_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def get_document(db: AsyncSession, doc_id: uuid.UUID) -> Optional[Document]:
    """Fetch a single document by ID."""
    result = await db.execute(select(Document).where(Document.id == doc_id))
    return result.scalar_one_or_none()


async def delete_document(db: AsyncSession, doc_id: uuid.UUID) -> bool:
    """Delete a document: DB rows (cascade to chunks) + on-disk files."""
    doc = await get_document(db, doc_id)
    if not doc:
        return False
    # Delete on-disk files (rmtree is sync I/O — offload to thread pool)
    doc_dir = get_document_dir(doc_id)
    try:
        if doc_dir.exists():
            await asyncio.to_thread(shutil.rmtree, doc_dir)
            _log("deleted on-disk files for doc %s at %s", doc_id, doc_dir)
    except Exception as e:
        _log("failed to delete doc dir %s: %s", doc_dir, e)
    # Delete DB rows (cascade handles chunks)
    await db.delete(doc)
    await db.commit()
    _log("deleted document %s (%s)", doc_id, doc.filename)
    return True


async def rename_document(
    db: AsyncSession, doc_id: uuid.UUID, new_filename: str
) -> Optional[Document]:
    """Rename a document's display filename.

    Updates both `filename` (display) and `original_filename`. Does NOT
    rename the on-disk file (kept stable so existing references remain
    valid). The user-facing name is what matters for citations.
    """
    doc = await get_document(db, doc_id)
    if not doc:
        return None
    safe = os.path.basename(new_filename.strip()) or doc.filename
    doc.filename = safe
    doc.original_filename = safe
    await db.commit()
    await db.refresh(doc)
    _log("renamed doc %s → %s", doc_id, safe)
    return doc


async def toggle_document_scope(
    db: AsyncSession,
    doc_id: uuid.UUID,
    new_scope: str,
    new_conversation_id: Optional[uuid.UUID] = None,
) -> Optional[Document]:
    """Toggle a document between 'private' and 'public'.

    When switching TO private, a conversation_id must be provided (the doc
    becomes tied to that conversation). When switching TO public, any
    existing conversation_id is cleared.
    """
    if new_scope not in ("private", "public"):
        raise ValueError(f"Invalid scope: {new_scope}")
    doc = await get_document(db, doc_id)
    if not doc:
        return None
    if new_scope == "private":
        if not new_conversation_id:
            raise ValueError(
                "conversation_id is required when switching to private scope"
            )
        doc.scope = "private"
        doc.conversation_id = new_conversation_id
    else:
        doc.scope = "public"
        doc.conversation_id = None
    await db.commit()
    await db.refresh(doc)
    _log(
        "toggled doc %s scope → %s (conv=%s)",
        doc_id,
        doc.scope,
        doc.conversation_id,
    )
    return doc


async def reindex_document(
    db: AsyncSession,
    doc_id: uuid.UUID,
    progress: ProgressCallback = _noop_progress,
) -> Document:
    """Re-digest an existing document in place (same ID, same scope).

    Reads the raw file back from disk and runs the full digestion
    pipeline with `reuse_doc_id` — chunks are dropped first, the row is
    reset to "digesting", and all stats are recomputed. Used by
    POST /documents/{id}/reindex/stream from the Workspace detail modal.
    """
    doc = await get_document(db, doc_id)
    if not doc:
        raise ValueError(f"Document not found: {doc_id}")

    if not doc.file_path:
        raise ValueError(f"Document {doc_id} has no stored file on disk")

    file_path = resolve_document_path(doc.file_path)
    if not file_path.exists():
        raise ValueError(
            f"Document file missing on disk: {file_path} — re-upload instead"
        )

    file_bytes = await asyncio.to_thread(file_path.read_bytes)
    scope = doc.scope if doc.scope in ("private", "public") else "private"
    return await digest_document(
        db,
        file_bytes=file_bytes,
        filename=doc.original_filename or doc.filename,
        scope=scope,
        conversation_id=doc.conversation_id if scope == "private" else None,
        progress=progress,
        reuse_doc_id=doc_id,
    )


async def remove_document_knowledge(
    db: AsyncSession, doc_id: uuid.UUID
) -> Optional[Document]:
    """Remove a document from the AI knowledge base (keep the file).

    Deletes every chunk (embeddings + BM25 vectors go with them) and the
    extracted images, resets the counters, and sets digestion_status to
    "not_indexed" — the document row and raw file stay so it can be
    re-indexed later. Used by DELETE /documents/{id}/knowledge.
    """
    doc = await get_document(db, doc_id)
    if not doc:
        return None

    await db.execute(delete(DocumentChunk).where(DocumentChunk.document_id == doc_id))
    doc.total_chunks = 0
    doc.total_images = 0
    doc.digestion_status = "not_indexed"
    doc.digestion_error = None
    await db.commit()
    await db.refresh(doc)

    # Remove extracted images (data/documents/{id}/images/) — orphans
    # once the chunks that referenced them are gone.
    images_dir = get_document_dir(doc_id) / "images"
    try:
        if images_dir.exists():
            await asyncio.to_thread(shutil.rmtree, images_dir)
    except Exception as e:
        _log("failed to remove images dir for %s: %s", doc_id, e)

    _log("removed AI knowledge for doc %s (%s)", doc_id, doc.filename)
    return doc


async def set_document_collections(
    db: AsyncSession, doc_id: uuid.UUID, collections: List[str]
) -> Optional[Document]:
    """Assign the document's collections (user-defined knowledge groups).

    Values are trimmed, de-duplicated (case-insensitively), capped at 16
    entries of 40 chars each, and empty strings are dropped.
    """
    doc = await get_document(db, doc_id)
    if not doc:
        return None

    cleaned: List[str] = []
    seen_lower = set()
    for raw in collections or []:
        value = str(raw).strip()[:40]
        if not value or value.lower() in seen_lower:
            continue
        seen_lower.add(value.lower())
        cleaned.append(value)
        if len(cleaned) >= 16:
            break

    doc.collections = cleaned
    await db.commit()
    await db.refresh(doc)
    _log("set collections for doc %s → %s", doc_id, cleaned)
    return doc


# ---------------------------------------------------------------------------
# Document → dict (for API responses)
# ---------------------------------------------------------------------------


def document_to_dict(doc: Document, *, include_chunks: bool = False) -> dict:
    """Serialize a Document for the API."""
    d = {
        "id": str(doc.id),
        "filename": doc.filename,
        "original_filename": doc.original_filename,
        "mime_type": doc.mime_type,
        "file_size_bytes": doc.file_size_bytes,
        "content_hash": doc.content_hash,
        "scope": doc.scope,
        "conversation_id": str(doc.conversation_id) if doc.conversation_id else None,
        "message_id": str(doc.message_id) if doc.message_id else None,
        "total_pages": doc.total_pages,
        "total_chunks": doc.total_chunks,
        "total_images": doc.total_images,
        "digestion_status": doc.digestion_status,
        "digestion_error": doc.digestion_error,
        "collections": list(doc.collections) if doc.collections else [],
        "created_at": int(doc.created_at.timestamp() * 1000) if doc.created_at else 0,
        "updated_at": int(doc.updated_at.timestamp() * 1000) if doc.updated_at else 0,
    }
    if include_chunks and hasattr(doc, "chunks"):
        d["chunks"] = [
            {
                "id": str(c.id),
                "chunk_index": c.chunk_index,
                "text": c.text[:500],  # truncate for API response
                "page_number": c.page_number,
                "line_start": c.line_start,
                "line_end": c.line_end,
                "chunk_type": c.chunk_type,
                "has_image": c.image_path is not None,
            }
            for c in sorted(doc.chunks, key=lambda x: x.chunk_index)
        ]
    return d


def retrieved_source_to_dict(s: RetrievedSource) -> dict:
    """Serialize a RetrievedSource for the API / SSE events.

    `image_path` is included so the frontend can fetch the original
    image (via /api/documents/chunks/{id}/image) for chunks where
    chunk_type == "image_description". For text chunks, image_path
    is None and the frontend renders only the snippet.
    """
    return {
        "document_id": s.document_id,
        "document_filename": s.document_filename,
        "chunk_id": s.chunk_id,
        "text": s.text,
        "snippet": s.text[:300] + ("…" if len(s.text) > 300 else ""),
        "page_number": s.page_number,
        "line_start": s.line_start,
        "line_end": s.line_end,
        "chunk_type": s.chunk_type,
        "score": s.score,
        "vector_sim": s.vector_sim,
        "bm25_score": s.bm25_score,
        "image_path": s.image_path,
        "has_image": s.image_path is not None,
    }


async def get_chunk_image_path(db: AsyncSession, chunk_id: uuid.UUID) -> Optional[str]:
    """Look up the on-disk image_path for a single chunk by its ID.

    Used by the /api/documents/chunks/{id}/image endpoint to serve the
    raw image bytes for an image_description chunk. Returns None if the
    chunk doesn't exist or has no image_path.
    """
    try:
        stmt = select(DocumentChunk.image_path).where(DocumentChunk.id == chunk_id)
        result = await db.execute(stmt)
        row = result.first()
        return row[0] if row else None
    except Exception as e:
        _log("get_chunk_image_path failed for %s: %s", chunk_id, e)
        return None
