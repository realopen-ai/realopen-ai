"""
RAG (Retrieval-Augmented Generation) service for RealOpen-AI.

Digestion pipeline (synchronous, with progress callbacks for SSE):
  1. Save raw upload to disk under data/documents/{doc_id}/{filename}.
  2. Extract text page-by-page (PDF/DOCX/XLSX/CSV/MD/TXT).
  3. Extract embedded images (PDF/DOCX).
  4. Adaptive-chunk the text: chunk size depends on total document length.
  5. For each image: call vision LLM → text description; treat description
     as an additional chunk with chunk_type="image_description".
  6. Embed every chunk with nomic-embed-text (768-dim).
  7. Persist Document + DocumentChunk rows.

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
line_start, line_end — the agent tool returns these to the LLM and the
frontend renders them as collapsible source cards.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import httpx
from sqlalchemy import and_, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.models import Document, DocumentChunk
from app.services.embeddings import get_embedding, get_embeddings

logger = logging.getLogger(__name__)


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
    """A retrieved chunk ready to be shown as a source citation."""

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
        pages.append(
            ExtractedPage(
                page_number=i, text=f"# Sheet: {sheet_name}\n" + "\n".join(lines)
            )
        )
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
    from docx.document import Document as _DocxDocument

    doc = _DocxDocument(io.BytesIO(buf))

    # Text — all paragraphs in document order, one page (DOCX has no
    # native page concept).
    lines: List[str] = []
    for para in doc.paragraphs:
        lines.append(para.text)
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
                    print("[rag] docx image extract failed: %s", inner)
    except Exception as e:
        print("[rag] docx image rels walk failed: %s", e)

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
    """
    import pdfplumber
    from pypdf import PdfReader

    pages: List[ExtractedPage] = []
    images: List[ExtractedImage] = []

    # Text — pdfplumber
    try:
        with pdfplumber.open(io.BytesIO(buf)) as pdf:
            for i, page in enumerate(pdf.pages, start=1):
                try:
                    page_text = page.extract_text() or ""
                except Exception as inner:
                    print("[rag] pdfplumber page %d extract_text failed: %s", i, inner)
                    page_text = ""
                pages.append(ExtractedPage(page_number=i, text=page_text))
    except Exception as e:
        print("[rag] pdfplumber open failed: %s", e)
        # Fall back to pypdf for text-only
        try:
            reader = PdfReader(io.BytesIO(buf))
            for i, page in enumerate(reader.pages, start=1):
                try:
                    page_text = page.extract_text() or ""
                except Exception as inner:
                    print("[rag] pypdf page %d extract_text failed: %s", i, inner)
                    page_text = ""
                pages.append(ExtractedPage(page_number=i, text=page_text))
        except Exception as inner2:
            print("[rag] pypdf fallback failed: %s", inner2)
            raise

    # Images — pypdf. Walk every page's /XObject resources looking for
    # image XObjects, extract their streams.
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
                                print("[rag] pdf image skipped: colorspace=%s", cs_str)
                                continue
                            try:
                                img = PILImage.frombytes(mode, (width, height), raw)
                            except Exception as inner:
                                print("[rag] pdf image frombytes failed: %s", inner)
                                continue
                            fmt = "PNG"
                        else:
                            print("[rag] pdf image skipped: filter=%s", filters)
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
                        print("[rag] pdf image extract one failed: %s", inner)
                        continue
            except Exception as inner:
                print("[rag] pdf page resources walk failed: %s", inner)
                continue
    except Exception as e:
        print("[rag] pypdf image walk failed: %s", e)

    return ExtractionResult(pages=pages, images=images, mime_type="application/pdf")


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
}


def extract_content(buf: bytes, filename: str) -> ExtractionResult:
    """Dispatch to the right extractor based on filename extension."""
    ext = get_file_extension(filename)
    extractor = _EXTRACTORS.get(ext)
    if not extractor:
        # Unknown extension — try as plain text so at least something is
        # indexed. The user can see in the Brain page that the doc was
        # accepted; if extraction yields nothing usable, digestion will
        # create a single placeholder chunk.
        print("[rag] no extractor for ext=%s, falling back to text", ext)
        return _extract_txt(buf)
    return extractor(buf)


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
    """
    if not image_bytes:
        return ""

    normalized = _normalize_image(image_bytes, settings.RAG_VISION_IMAGE_MAX_DIM)
    b64 = base64.b64encode(normalized).decode("utf-8")

    try:
        model = settings.resolve_model(settings.RAG_VISION_MODEL_ROLE)
    except Exception as e:
        print("[rag] vision model not resolved: %s", e)
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
                print("[rag] vision model returned empty description")
                return ""
            return description
    except Exception as e:
        print("[rag] vision LLM call failed: %s", e)
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
    """
    doc_dir = get_document_dir(doc_id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    # Sanitize filename — strip path separators, keep the basename.
    safe_name = os.path.basename(filename) or "upload"
    out_path = doc_dir / safe_name
    out_path.write_bytes(buf)
    # Relative path
    return f"{settings.RAG_DOCUMENTS_DIR}/{doc_id}/{safe_name}"


def save_image_file(
    image_bytes: bytes,
    doc_id: uuid.UUID,
    image_index: int,
    format_hint: str,
) -> str:
    """Save an extracted image to disk and return the relative path."""
    doc_dir = get_document_dir(doc_id)
    doc_dir.mkdir(parents=True, exist_ok=True)
    ext = "png" if format_hint.upper() in ("PNG",) else format_hint.lower()
    safe_name = f"image_{image_index:03d}.{ext}"
    out_path = doc_dir / safe_name
    out_path.write_bytes(image_bytes)
    return f"{settings.RAG_DOCUMENTS_DIR}/{doc_id}/{safe_name}"


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
) -> Document:
    """Synchronously digest an uploaded file into searchable chunks.

    Stages (each emits a DigestProgress callback so the frontend can show
    real-time feedback):
      started → extracting_text → extracting_images → chunking →
      describing_images → embedding → persisting → done

    On any unrecoverable error, marks the document row as "failed" with
    the error message and re-raises.
    """
    doc_id = uuid.uuid4()
    safe_scope = scope if scope in ("private", "public") else "private"
    mime = guess_mime_type(filename)

    # Create the document row up-front so we can mark it as "digesting"
    # and have a row to update on failure.
    doc = Document(
        id=doc_id,
        filename=os.path.basename(filename),
        original_filename=os.path.basename(filename),
        mime_type=mime,
        file_path="",  # filled in after save
        file_size_bytes=len(file_bytes),
        content_hash=_sha256(file_bytes),
        scope=safe_scope,
        conversation_id=conversation_id if safe_scope == "private" else None,
        message_id=message_id,
        digestion_status="digesting",
    )
    db.add(doc)
    await db.commit()
    await db.refresh(doc)

    progress(DigestProgress(stage="started", percent=0, details=filename))

    try:
        # ── 1. Save raw file to disk ─────────────────────────────────────
        rel_path = await save_uploaded_file(file_bytes, filename, doc_id)
        doc.file_path = rel_path
        await db.commit()

        # ── 2. Extract text + images ────────────────────────────────────
        progress(
            DigestProgress(
                stage="extracting_text",
                percent=10,
                details=f"Extracting text from {filename}",
            )
        )
        extraction = extract_content(file_bytes, filename)
        if not extraction.pages or all(not p.text.strip() for p in extraction.pages):
            # No text at all — create a single placeholder chunk so the doc
            # is at least retrievable by filename.
            print("[rag] no text extracted from %s, using placeholder", filename)
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
                details=f"Extracted {len(extraction.pages)} page(s), {len(extraction.images)} image(s)",
            )
        )

        # ── 3. Chunk text ───────────────────────────────────────────────
        progress(
            DigestProgress(
                stage="chunking",
                percent=30,
                details=f"Chunking {sum(len(p.text) for p in extraction.pages)} chars",
            )
        )
        text_chunks = chunk_pages(extraction.pages)
        print(
            "[rag] %s: %d text chunks from %d pages",
            filename,
            len(text_chunks),
            len(extraction.pages),
        )

        # ── 4. Describe images with vision LLM ──────────────────────────
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
                        details=f"Image {i+1}/{len(extraction.images)}",
                    )
                )
                description = await describe_image_with_vision(img.image_bytes)
                if not description:
                    print(
                        "[rag] image %d of %s yielded no description, skipping",
                        i,
                        filename,
                    )
                    continue
                # Persist the image bytes to disk so the user can download
                # it later from the Brain page.
                image_rel = save_image_file(
                    img.image_bytes,
                    doc_id,
                    i,
                    img.format_hint,
                )
                image_chunks.append(
                    Chunk(
                        text=(
                            f"[Image on page {img.page_number} of {filename}]\n"
                            f"{description}"
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

        # ── 5. Embed all chunks ─────────────────────────────────────────
        progress(
            DigestProgress(
                stage="embedding",
                percent=75,
                details=f"Embedding {len(all_chunks)} chunk(s)",
            )
        )
        embeddings = await get_embeddings([c.text for c in all_chunks])
        for chunk, emb in zip(all_chunks, embeddings):
            chunk._embedding = emb  # type: ignore[attr-defined]

        # ── 6. Persist chunks ───────────────────────────────────────────
        progress(
            DigestProgress(
                stage="persisting",
                percent=90,
                details=f"Saving {len(all_chunks)} chunk(s) to database",
            )
        )
        chunk_rows: List[DocumentChunk] = []
        for c in all_chunks:
            row = DocumentChunk(
                document_id=doc_id,
                chunk_index=c.chunk_index,
                text=c.text,
                page_number=c.page_number,
                line_start=c.line_start,
                line_end=c.line_end,
                chunk_type=c.chunk_type,
                image_path=c.image_path,
                embedding=getattr(c, "_embedding", None),  # type: ignore[attr-defined]
            )
            chunk_rows.append(row)
        db.add_all(chunk_rows)

        # ── 7. Update document row with final stats ─────────────────────
        doc.total_pages = len(extraction.pages) if extraction.pages else None
        doc.total_chunks = len(all_chunks)
        doc.total_images = len(image_chunks)
        doc.digestion_status = "ready"
        doc.digestion_error = None
        await db.commit()
        await db.refresh(doc)

        progress(
            DigestProgress(
                stage="done",
                percent=100,
                details=f"Digested {filename}: {len(text_chunks)} text chunks, {len(image_chunks)} image chunks",
                document_id=str(doc_id),
                total_chunks=len(all_chunks),
                total_images=len(image_chunks),
            )
        )
        print(
            "[rag] digested %s (id=%s): %d text chunks, %d image chunks",
            filename,
            doc_id,
            len(text_chunks),
            len(image_chunks),
        )
        return doc

    except Exception as e:
        print("[rag] digestion failed for %s: %s", filename, e)
        # Mark the doc as failed
        try:
            doc.digestion_status = "failed"
            doc.digestion_error = str(e)[:2000]
            await db.commit()
        except Exception as inner:
            print("[rag] failed to mark doc as failed: %s", inner)

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

    Scope filter:
      - public                                → any conversation
      - private AND conversation_id = ?       → only that conversation

    Returns at most top_k_total chunks, with at most top_k_per_doc chunks
    per document. Sorted by hybrid score (descending).
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

    # Embed the query
    q_emb = await get_embedding(query)
    if not q_emb:
        print("[rag] query embedding failed, returning no results")
        return []

    # ── Step 1: pgvector cosine search to get top candidates ────────────
    # We over-fetch so the per-doc selection still has enough to work with
    # after the cutoff filter and per-doc cap are applied.
    fetch_k = max(k_total * 3, 30)
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

    # Apply scope filter:
    #   - public docs always searchable
    #   - private docs searchable only when their conversation_id matches
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
        rows = result.all()
    except Exception as e:
        print("[rag] vector search query failed: %s", e)
        return []

    if not rows:
        print("[rag] no candidates found for query: %s", query[:80])
        return []

    # ── Step 2: BM25 score via raw SQL (search_vector is DB-managed) ────
    # Same pattern as the memory system: search_vector is a GENERATED column
    # not in the ORM, so we issue a separate raw SQL query to get ts_rank_cd
    # for the matching chunk IDs.
    chunk_ids = [r.id for r in rows]
    bm25_scores: Dict[str, float] = {}
    try:
        # Build a tsquery from query tokens — OR them so any match contributes.
        tokens = re.findall(r"\w+", query.lower())
        tokens = [t for t in tokens if len(t) >= 2]
        if tokens:
            tsquery = " | ".join(f"'{t}'" for t in tokens)
            # Build an inline UUID list for the WHERE IN clause.
            # We use a parameter for the tsquery and a tuple literal for
            # the IDs (PostgreSQL accepts this directly).
            id_list = ",".join(f"'{cid}'" for cid in chunk_ids)
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
        print("[rag] BM25 query failed, using vector-only: %s", e)
        bm25_scores = {}

    # ── Step 3: hybrid score + cutoff + per-doc selection ───────────────
    w_vec = settings.RAG_RETRIEVAL_VECTOR_WEIGHT
    w_bm25 = settings.RAG_RETRIEVAL_BM25_WEIGHT

    # Normalize BM25 to 0..1 across the candidate set
    max_bm25 = max(bm25_scores.values()) if bm25_scores else 0.0

    candidates: List[Tuple[float, float, float, Any]] = []
    for r in rows:
        vec_sim = 1.0 - (r.distance or 1.0)
        if vec_sim < cutoff:
            continue
        bm25_raw = bm25_scores.get(str(r.id), 0.0)
        bm25_norm = bm25_raw / max_bm25 if max_bm25 > 0 else 0.0
        score = w_vec * vec_sim + w_bm25 * bm25_norm
        candidates.append((score, vec_sim, bm25_norm, r))

    candidates.sort(key=lambda x: x[0], reverse=True)

    per_doc_count: Dict[str, int] = {}
    final: List[RetrievedSource] = []
    for score, vec_sim, bm25_norm, r in candidates:
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
            )
        )
        if len(final) >= k_total:
            break

    print(
        "[rag] search '%s' → %d candidates, %d returned (cutoff=%s, k_per_doc=%d, k_total=%d)",
        query[:60],
        len(rows),
        len(final),
        cutoff,
        k_per_doc,
        k_total,
    )
    return final


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
    # Delete on-disk files
    doc_dir = get_document_dir(doc_id)
    try:
        if doc_dir.exists():
            shutil.rmtree(doc_dir)
            print("[rag] deleted on-disk files for doc %s at %s", doc_id, doc_dir)
    except Exception as e:
        print("[rag] failed to delete doc dir %s: %s", doc_dir, e)
    # Delete DB rows (cascade handles chunks)
    await db.delete(doc)
    await db.commit()
    print("[rag] deleted document %s (%s)", doc_id, doc.filename)
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
    print("[rag] renamed doc %s → %s", doc_id, safe)
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
    print(
        "[rag] toggled doc %s scope → %s (conv=%s)",
        doc_id,
        doc.scope,
        doc.conversation_id,
    )
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
    """Serialize a RetrievedSource for the API / SSE events."""
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
    }
