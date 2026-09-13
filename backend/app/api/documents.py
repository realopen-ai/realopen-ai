"""
Documents API routes for RealOpen-AI.

Endpoints under /api/documents:
  POST   /documents/upload                     — multipart upload (sync digestion)
  POST   /documents/upload/stream              — multipart upload with SSE progress
  GET    /documents                            — list all (optionally filter by scope/conversation)
  GET    /documents/{id}                       — get one
  GET    /documents/{id}/download              — stream the raw file
  GET    /documents/{id}/thumbnail             — card thumbnail JPEG (any format, LibreOffice/PIL)
  GET    /documents/{id}/pages                 — page-image manifest for the preview viewer
  GET    /documents/{id}/pages/{page}          — one page JPEG (variant=full|thumb)
  POST   /documents/{id}/reindex/stream        — re-digest with SSE progress
  DELETE /documents/{id}/knowledge             — remove chunks (keep the file)
  GET    /documents/chunks/{id}/image          — get the image bytes for a chunk
  PATCH  /documents/{id}                       — rename / toggle scope / set collections
  DELETE /documents/{id}                       — delete (file + DB rows)

The /upload/stream and /reindex/stream endpoints emit `document_digest_*`
SSE events so the Workspace > Documents UI (and chat upload) can show
real-time progress (extracting text → chunking → describing images →
embedding → done).
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from typing import List, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from app.db.session import async_session_factory
from app.services import rag as rag_service

logger = logging.getLogger(__name__)
router = APIRouter()


def _log(msg: str, *args) -> None:
    """Always-visible print() logger for the documents API."""
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[documents] {formatted}", flush=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_uuid(s: str) -> uuid.UUID:
    """Parse a UUID string, raising HTTPException(400) if invalid."""
    try:
        return uuid.UUID(s)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid UUID: {s}")


def _parse_optional_uuid(s: Optional[str]) -> Optional[uuid.UUID]:
    if not s:
        return None
    try:
        return uuid.UUID(s)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid UUID: {s}")


# Acceptable upload extensions. Any type the RAG pipeline can digest AND
# the preview renderer can rasterize. Legacy .doc/.xls/.ppt are converted
# to their modern equivalents by LibreOffice at digestion time.
_ALLOWED_EXTENSIONS = {
    "txt",
    "md",
    "markdown",
    "csv",
    "tsv",
    "pdf",
    "docx",
    "doc",
    "xlsx",
    "xls",
    "pptx",
    "ppt",
}


def _validate_extension(filename: str) -> None:
    ext = os.path.splitext(filename)[1].lower().lstrip(".")
    if ext not in _ALLOWED_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unsupported file type: .{ext}. "
                f"Allowed: {sorted(_ALLOWED_EXTENSIONS)}"
            ),
        )


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class DocumentUpdateRequest(BaseModel):
    """Body for PATCH /documents/{id}.

    All fields optional — only provided fields are updated. When
    `scope` is "private", `conversation_id` MUST be provided.
    `collections` replaces the full list (user-defined knowledge groups).
    """

    filename: Optional[str] = None
    scope: Optional[str] = None  # "private" | "public"
    conversation_id: Optional[str] = None
    collections: Optional[List[str]] = None


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse(event: str, data: dict) -> str:
    """Format an SSE event string."""
    return f"data: {json.dumps({'event': event, **data})}\n\n"


def _progress_to_sse(p: rag_service.DigestProgress) -> str:
    """Convert a DigestProgress to an SSE event string."""
    return _sse(
        "document_digest_progress",
        {
            "stage": p.stage,
            "percent": p.percent,
            "details": p.details,
            "document_id": p.document_id,
            "total_chunks": p.total_chunks,
            "total_images": p.total_images,
        },
    )


# ---------------------------------------------------------------------------
# Endpoints — upload (non-streaming, returns when ready)
# ---------------------------------------------------------------------------


@router.post("/documents/upload")
async def upload_document(
    file: UploadFile = File(...),
    scope: str = Form("private"),
    conversation_id: Optional[str] = Form(None),
):
    """Upload a document, digest synchronously, return when ready.

    For real-time progress, use /documents/upload/stream instead.
    """
    _validate_extension(file.filename or "upload")
    if scope not in ("private", "public"):
        raise HTTPException(
            status_code=400, detail="scope must be 'private' or 'public'"
        )

    conv_id = _parse_optional_uuid(conversation_id)
    if scope == "private" and not conv_id:
        raise HTTPException(
            status_code=400,
            detail="conversation_id is required when scope is 'private'",
        )

    file_bytes = await file.read()
    _log(
        "upload filename=%s size=%d scope=%s conv=%s",
        file.filename,
        len(file_bytes),
        scope,
        conv_id,
    )

    async with async_session_factory() as db:
        try:
            _log("starting digestion for %s", file.filename)
            doc = await rag_service.digest_document(
                db,
                file_bytes=file_bytes,
                filename=file.filename or "upload",
                scope=scope,
                conversation_id=conv_id,
            )
            _log("digestion completed for %s, id=%s", file.filename, doc.id)
            return rag_service.document_to_dict(doc, include_chunks=False)
        except Exception as e:
            _log("upload failed: %s", e)
            raise HTTPException(status_code=500, detail=f"Digestion failed: {e}")


# ---------------------------------------------------------------------------
# Endpoints — upload with SSE progress
# ---------------------------------------------------------------------------


@router.post("/documents/upload/stream")
async def upload_document_stream(
    file: UploadFile = File(...),
    scope: str = Form("private"),
    conversation_id: Optional[str] = Form(None),
):
    """Upload a document with real-time SSE digestion progress.

    Emits these events:
      document_digest_progress  — { stage, percent, details, ... }
      document_digest_done      — { document: <full doc dict> }
      document_digest_error     — { error, document_id? }

    The stream ends after either 'done' or 'error'.
    """
    _validate_extension(file.filename or "upload")
    if scope not in ("private", "public"):
        raise HTTPException(
            status_code=400, detail="scope must be 'private' or 'public'"
        )

    conv_id = _parse_optional_uuid(conversation_id)
    if scope == "private" and not conv_id:
        raise HTTPException(
            status_code=400,
            detail="conversation_id is required when scope is 'private'",
        )

    file_bytes = await file.read()
    filename = file.filename or "upload"
    _log(
        "upload/stream filename=%s size=%d scope=%s conv=%s",
        filename,
        len(file_bytes),
        scope,
        conv_id,
    )

    # Use an asyncio.Queue so progress events from the digestion task are
    # delivered to the SSE generator in REAL TIME — the generator awaits
    # queue.get() with a small timeout, and yields the event as soon as
    # the digestion task pushes it. This avoids the previous list-based
    # polling which only flushed events every 50ms AND would batch all
    # events at the end if digestion ran in a tight loop.
    import asyncio

    progress_queue: asyncio.Queue = asyncio.Queue()
    # Sentinel pushed to the queue when digestion finishes so the
    # generator knows to stop draining.
    _DONE_SENTINEL = object()

    def collect_progress(p: rag_service.DigestProgress) -> None:
        # collect_progress is a sync callback invoked from inside
        # digest_document. We use put_nowait so it works from any
        # thread (asyncio.to_thread runs sync code in a worker thread,
        # but the progress callbacks are invoked from the main event
        # loop thread because they're called between awaits).
        try:
            progress_queue.put_nowait(p)
        except Exception as e:
            _log("failed to enqueue progress event: %s", e)

    async def generate():
        digestion_error: List[Optional[Exception]] = [None]
        digestion_result: List[Optional[object]] = [None]
        last_doc_id: List[Optional[str]] = [None]

        async def run_digestion():
            try:
                async with async_session_factory() as db:
                    doc = await rag_service.digest_document(
                        db,
                        file_bytes=file_bytes,
                        filename=filename,
                        scope=scope,
                        conversation_id=conv_id,
                        progress=collect_progress,
                    )
                    digestion_result[0] = doc
            except Exception as e:
                digestion_error[0] = e
            finally:
                # Always push the sentinel so the generator's drain loop
                # terminates even on error.
                try:
                    progress_queue.put_nowait(_DONE_SENTINEL)
                except Exception:
                    pass

        task = asyncio.create_task(run_digestion())

        # Drain the queue in real-time, yielding each progress event as
        # an SSE chunk. Loop until we see the sentinel (digestion done).
        while True:
            try:
                item = await asyncio.wait_for(progress_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                # No event in 100ms — check if task crashed without
                # pushing the sentinel (defensive).
                if task.done():
                    break
                continue
            if item is _DONE_SENTINEL:
                break
            # Track the latest doc_id for error reporting
            if getattr(item, "document_id", None):
                last_doc_id[0] = item.document_id
            yield _progress_to_sse(item)

        # Await the task to surface any exception
        try:
            await task
        except Exception as e:
            _log("digestion task failed: %s", e)

        if digestion_error[0] is not None:
            err = digestion_error[0]
            yield _sse(
                "document_digest_error",
                {
                    "error": str(err),
                    "document_id": last_doc_id[0],
                },
            )
            return

        if digestion_result[0] is not None:
            doc = digestion_result[0]
            yield _sse(
                "document_digest_done",
                {
                    "document": rag_service.document_to_dict(doc, include_chunks=False),
                },
            )
            return

        yield _sse(
            "document_digest_error",
            {
                "error": "Unknown error: digestion completed without result or error",
            },
        )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Endpoints — list / get / download / patch / delete
# ---------------------------------------------------------------------------


@router.get("/documents")
async def list_documents(
    scope: Optional[str] = None,
    conversation_id: Optional[str] = None,
):
    """List all documents, optionally filtered by scope and/or conversation."""
    conv_id = _parse_optional_uuid(conversation_id)
    if scope and scope not in ("private", "public"):
        raise HTTPException(
            status_code=400, detail="scope must be 'private' or 'public'"
        )

    async with async_session_factory() as db:
        docs = await rag_service.list_documents(
            db,
            scope=scope,
            conversation_id=conv_id,
        )
        return {
            "documents": [rag_service.document_to_dict(d) for d in docs],
            "total": len(docs),
        }


@router.get("/documents/chunks/{chunk_id}/image")
async def get_chunk_image(chunk_id: str):
    """Serve the original image bytes for an image_description chunk.

    Used by the frontend to render image thumbnails in source cards
    when the agent's rag_search tool returned an image_description
    chunk (chunk_type="image_description" with a non-null image_path).

    Returns 404 if the chunk doesn't exist or has no associated image.

    NOTE: this route MUST be declared BEFORE /documents/{document_id}
    so FastAPI doesn't match "chunks" as a document_id. Route order
    matters — more specific paths first, parameterized paths last.
    """
    chunk_uuid = _parse_uuid(chunk_id)
    async with async_session_factory() as db:
        image_rel = await rag_service.get_chunk_image_path(db, chunk_uuid)
        if not image_rel:
            raise HTTPException(
                status_code=404, detail="Chunk has no image or does not exist"
            )
        file_path = rag_service.resolve_document_path(image_rel)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="Image file not found on disk")
        # Guess the MIME type from the file extension.
        ext = os.path.splitext(file_path)[1].lower()
        mime_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".bmp": "image/bmp",
            ".webp": "image/webp",
            ".tiff": "image/tiff",
        }
        media_type = mime_map.get(ext, "image/png")
        return FileResponse(
            path=str(file_path),
            media_type=media_type,
            headers={"Cache-Control": "public, max-age=86400"},  # cache 24h
        )


# ---------------------------------------------------------------------------
# Endpoints — preview (thumbnail + page images, any document type)
# ---------------------------------------------------------------------------


async def _get_document_file(document_id: str):
    """Fetch the Document row and resolve its on-disk file path.

    Returns (doc, Path) or raises the right HTTPException (404 unknown
    id, 404 missing file on disk).
    """
    doc_uuid = _parse_uuid(document_id)
    async with async_session_factory() as db:
        doc = await rag_service.get_document(db, doc_uuid)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
    file_path = rag_service.resolve_document_path(doc.file_path)
    if not doc.file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="Document file not found on disk")
    return doc, file_path


@router.get("/documents/{document_id}/thumbnail")
async def get_document_thumbnail(document_id: str):
    """Card thumbnail (first page) for any document type.

    Renders via LibreOffice (office formats incl. legacy .doc/.xls/.ppt),
    PyMuPDF (PDF), or PIL (plain text fallback) — cached at
    data/documents/{id}/{stem}_pages/thumb.jpg, invalidated by the
    source file's mtime. The frontend uses this URL in <img> tags with
    onError fallback to a file-type icon, so a 404 degrades gracefully.
    """
    from fastapi.responses import Response
    from app.services.integrations import libreoffice

    doc, file_path = await _get_document_file(document_id)

    if libreoffice.document_preview_format(file_path) is None:
        raise HTTPException(
            status_code=404,
            detail=f"No preview available for .{file_path.suffix.lstrip('.')} files",
        )

    thumb_bytes = await libreoffice.render_document_thumbnail(file_path)
    if thumb_bytes is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Thumbnail unavailable — "
                + (
                    "LibreOffice is not installed."
                    if not libreoffice.is_available()
                    else "rendering failed for this file."
                )
            ),
        )
    return Response(
        content=thumb_bytes,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},  # 24h
    )


@router.get("/documents/{document_id}/pages")
async def get_document_pages(document_id: str):
    """Page-image manifest for the document preview viewer.

    Same manifest contract as /reports/{id}/slides:
    { count, width, height, notes, ... }. Page JPEGs are served by
    /documents/{id}/pages/{n}?variant=full|thumb. The mtime recorded in
    the manifest doubles as the frontend's cache-buster (?v=).
    """
    from app.services.integrations import libreoffice

    doc, file_path = await _get_document_file(document_id)

    manifest = await libreoffice.render_document_pages(file_path)
    if manifest is None:
        raise HTTPException(
            status_code=404,
            detail=(
                "Preview unavailable — "
                + (
                    "LibreOffice is not installed."
                    if not libreoffice.is_available()
                    else "this file could not be rendered."
                )
            ),
        )
    return {
        "count": manifest["count"],
        "width": manifest.get("width") or 0,
        "height": manifest.get("height") or 0,
        "source_mtime": manifest.get("source_mtime"),
        "document_id": str(doc.id),
        "filename": doc.filename,
    }


@router.get("/documents/{document_id}/pages/{page_number}")
async def get_document_page(document_id: str, page_number: int, variant: str = "full"):
    """Serve one rendered page JPEG (variant=full|thumb, 1-based index)."""
    from fastapi.responses import FileResponse
    from app.services.integrations import libreoffice

    if variant not in ("full", "thumb"):
        raise HTTPException(status_code=400, detail="variant must be 'full' or 'thumb'")
    if page_number < 1:
        raise HTTPException(status_code=400, detail="page_number must be >= 1")

    doc, file_path = await _get_document_file(document_id)

    cache_dir = libreoffice.document_preview_cache_dir(file_path)
    manifest = await libreoffice.render_document_pages(file_path)
    if manifest is None or page_number > manifest["count"]:
        raise HTTPException(status_code=404, detail=f"Page {page_number} not available")
    img_path = cache_dir / libreoffice.slide_image_name(
        page_number, thumb=(variant == "thumb")
    )
    if not img_path.is_file():
        raise HTTPException(status_code=404, detail=f"Page {page_number} not rendered")
    return FileResponse(
        path=str(img_path),
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


# ---------------------------------------------------------------------------
# Endpoints — AI knowledge management (reindex / remove)
# ---------------------------------------------------------------------------


@router.post("/documents/{document_id}/reindex/stream")
async def reindex_document_stream(document_id: str):
    """Re-digest an existing document with real-time SSE progress.

    Reads the raw file back from disk and re-runs the full digestion
    pipeline IN PLACE (same document id, scope and conversation kept).
    Emits the same events as /documents/upload/stream:
      document_digest_progress / document_digest_done / document_digest_error
    """
    import asyncio

    doc_uuid = _parse_uuid(document_id)

    # Validate up-front (404s before the stream starts).
    async with async_session_factory() as db:
        doc = await rag_service.get_document(db, doc_uuid)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        if (
            not doc.file_path
            or not rag_service.resolve_document_path(doc.file_path).exists()
        ):
            raise HTTPException(
                status_code=404, detail="Document file not found on disk"
            )

    progress_queue: asyncio.Queue = asyncio.Queue()
    _DONE_SENTINEL = object()

    def collect_progress(p: rag_service.DigestProgress) -> None:
        try:
            progress_queue.put_nowait(p)
        except Exception as e:
            _log("failed to enqueue progress event: %s", e)

    async def generate():
        digestion_error: List[Optional[Exception]] = [None]
        digestion_result: List[Optional[object]] = [None]

        async def run_reindex():
            try:
                async with async_session_factory() as db:
                    doc = await rag_service.reindex_document(
                        db, doc_uuid, progress=collect_progress
                    )
                    digestion_result[0] = doc
            except Exception as e:
                digestion_error[0] = e
            finally:
                try:
                    progress_queue.put_nowait(_DONE_SENTINEL)
                except Exception:
                    pass

        task = asyncio.create_task(run_reindex())

        while True:
            try:
                item = await asyncio.wait_for(progress_queue.get(), timeout=0.1)
            except asyncio.TimeoutError:
                if task.done():
                    break
                continue
            if item is _DONE_SENTINEL:
                break
            yield _progress_to_sse(item)

        try:
            await task
        except Exception as e:
            _log("reindex task failed: %s", e)

        if digestion_error[0] is not None:
            yield _sse(
                "document_digest_error",
                {"error": str(digestion_error[0]), "document_id": str(doc_uuid)},
            )
            return

        if digestion_result[0] is not None:
            doc = digestion_result[0]
            yield _sse(
                "document_digest_done",
                {"document": rag_service.document_to_dict(doc, include_chunks=False)},
            )
            return

        yield _sse(
            "document_digest_error",
            {"error": "Unknown error: reindex completed without result or error"},
        )

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/documents/{document_id}/knowledge")
async def remove_document_knowledge(document_id: str):
    """Remove a document from the AI knowledge base, keeping the file.

    Deletes all chunks (embeddings + extracted images) and sets
    digestion_status="not_indexed" — the document stays in the list and
    can be re-indexed later via /reindex/stream.
    """
    doc_uuid = _parse_uuid(document_id)
    async with async_session_factory() as db:
        doc = await rag_service.remove_document_knowledge(db, doc_uuid)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return rag_service.document_to_dict(doc, include_chunks=False)


@router.get("/documents/{document_id}")
async def get_document(document_id: str):
    """Get a single document with its chunks."""
    doc_uuid = _parse_uuid(document_id)
    async with async_session_factory() as db:
        # Eager-load chunks via the relationship
        from sqlalchemy import select
        from sqlalchemy.orm import selectinload

        stmt = (
            select(rag_service.Document)
            .options(selectinload(rag_service.Document.chunks))
            .where(rag_service.Document.id == doc_uuid)
        )
        result = await db.execute(stmt)
        doc = result.scalar_one_or_none()
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return rag_service.document_to_dict(doc, include_chunks=True)


@router.get("/documents/{document_id}/download")
async def download_document(document_id: str):
    """Stream the raw uploaded file for download."""
    doc_uuid = _parse_uuid(document_id)
    async with async_session_factory() as db:
        doc = await rag_service.get_document(db, doc_uuid)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        file_path = rag_service.resolve_document_path(doc.file_path)
        if not file_path.exists():
            raise HTTPException(status_code=404, detail="File not found on disk")
        return FileResponse(
            path=str(file_path),
            filename=doc.filename,
            media_type=doc.mime_type or "application/octet-stream",
        )


@router.patch("/documents/{document_id}")
async def update_document(document_id: str, req: DocumentUpdateRequest):
    """Update a document (rename, toggle scope, and/or set collections)."""
    doc_uuid = _parse_uuid(document_id)
    async with async_session_factory() as db:
        # Rename first if requested
        if req.filename is not None:
            renamed = await rag_service.rename_document(db, doc_uuid, req.filename)
            if not renamed:
                raise HTTPException(status_code=404, detail="Document not found")

        # Then handle scope toggle if requested
        if req.scope is not None:
            if req.scope not in ("private", "public"):
                raise HTTPException(
                    status_code=400, detail="scope must be 'private' or 'public'"
                )
            new_conv_id = _parse_optional_uuid(req.conversation_id)
            if req.scope == "private" and not new_conv_id:
                raise HTTPException(
                    status_code=400,
                    detail="conversation_id is required when switching to 'private' scope",
                )
            try:
                toggled = await rag_service.toggle_document_scope(
                    db,
                    doc_uuid,
                    req.scope,
                    new_conv_id,
                )
                if not toggled:
                    raise HTTPException(status_code=404, detail="Document not found")
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))

        # Then handle collections if requested (full-list replacement)
        if req.collections is not None:
            updated = await rag_service.set_document_collections(
                db, doc_uuid, req.collections
            )
            if not updated:
                raise HTTPException(status_code=404, detail="Document not found")

        # Return the updated doc
        doc = await rag_service.get_document(db, doc_uuid)
        if not doc:
            raise HTTPException(status_code=404, detail="Document not found")
        return rag_service.document_to_dict(doc, include_chunks=False)


@router.delete("/documents/{document_id}")
async def delete_document(document_id: str):
    """Delete a document: DB rows (cascade to chunks) + on-disk files."""
    doc_uuid = _parse_uuid(document_id)
    async with async_session_factory() as db:
        ok = await rag_service.delete_document(db, doc_uuid)
        if not ok:
            raise HTTPException(status_code=404, detail="Document not found")
        return {"status": "deleted", "id": document_id}


# ---------------------------------------------------------------------------
# Endpoint — list by conversation (helper for the Brain page when picking
# a target conversation for a private upload)
# ---------------------------------------------------------------------------


@router.get("/conversations/{conversation_id}/documents")
async def list_documents_for_conversation(conversation_id: str):
    """List private documents tied to a specific conversation.

    Used by the chat UI to show which documents are attached to the
    current conversation.
    """
    conv_uuid = _parse_uuid(conversation_id)
    async with async_session_factory() as db:
        # Private docs tied to this conversation + all public docs
        from sqlalchemy import or_
        from app.db.models import Document
        from sqlalchemy import select

        stmt = (
            select(Document)
            .where(
                or_(
                    Document.conversation_id == conv_uuid,
                    Document.scope == "public",
                )
            )
            .order_by(Document.created_at.desc())
        )
        result = await db.execute(stmt)
        docs = list(result.scalars().all())
        return {
            "documents": [rag_service.document_to_dict(d) for d in docs],
            "total": len(docs),
        }
