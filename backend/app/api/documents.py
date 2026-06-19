"""
Documents API routes for RealOpen-AI.

Endpoints under /api/documents:
  POST   /documents/upload            — multipart upload (sync digestion)
  POST   /documents/upload/stream     — multipart upload with SSE progress
  GET    /documents                   — list all (optionally filter by scope/conversation)
  GET    /documents/{id}              — get one
  GET    /documents/{id}/download     — stream the raw file
  PATCH  /documents/{id}              — rename and/or toggle scope
  DELETE /documents/{id}              — delete (file + DB rows)

The /upload/stream endpoint is what the frontend uses — it emits
`document_digest_*` SSE events so the Brain page and chat upload can show
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


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_uuid(s: str) -> uuid.UUID:
    """Parse a UUID string, raising ValueError if invalid."""
    return uuid.UUID(s)


def _parse_optional_uuid(s: Optional[str]) -> Optional[uuid.UUID]:
    if not s:
        return None
    try:
        return uuid.UUID(s)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail=f"Invalid UUID: {s}")


# Acceptable upload extensions (matches the frontend accept attribute plus
# a few extras like .pptx that we may support later).
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
    """

    filename: Optional[str] = None
    scope: Optional[str] = None  # "private" | "public"
    conversation_id: Optional[str] = None


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
    print(
        "[documents] upload filename=%s size=%d scope=%s conv=%s",
        file.filename,
        len(file_bytes),
        scope,
        conv_id,
    )

    async with async_session_factory() as db:
        try:
            print("[documents] starting digestion for %s", file.filename)
            doc = await rag_service.digest_document(
                db,
                file_bytes=file_bytes,
                filename=file.filename or "upload",
                scope=scope,
                conversation_id=conv_id,
            )
            print("[documents] digestion completed for %s, id=%s", file.filename, doc.id)
            return rag_service.document_to_dict(doc, include_chunks=False)
        except Exception as e:
            print("[documents] upload failed: %s", e)
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
    print(
        "[documents] upload/stream filename=%s size=%d scope=%s conv=%s",
        filename,
        len(file_bytes),
        scope,
        conv_id,
    )

    # Capture progress events in a queue so the async generator can yield
    # them as SSE. We use a simple list-and-index approach since digestion
    # is synchronous (awaits internally but doesn't run in parallel with
    # this generator).
    progress_events: List[rag_service.DigestProgress] = []

    def collect_progress(p: rag_service.DigestProgress) -> None:
        progress_events.append(p)

    async def generate():
        # Run digestion in a background task so we can interleave progress
        # events with the SSE stream.
        import asyncio

        digestion_error: List[Optional[Exception]] = [None]
        digestion_result: List[Optional[object]] = [None]

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

        task = asyncio.create_task(run_digestion())

        # Poll for progress events while digestion runs
        seen = 0
        while not task.done():
            while seen < len(progress_events):
                yield _progress_to_sse(progress_events[seen])
                seen += 1
            await asyncio.sleep(0.05)

        # Drain any remaining events
        while seen < len(progress_events):
            yield _progress_to_sse(progress_events[seen])
            seen += 1

        # Await the task to surface any exception
        try:
            await task
        except Exception as e:
            print("[documents] digestion task failed: %s", e)

        if digestion_error[0] is not None:
            err = digestion_error[0]
            # Find the doc_id from the last progress event if possible
            doc_id = None
            for p in reversed(progress_events):
                if p.document_id:
                    doc_id = p.document_id
                    break
            yield _sse(
                "document_digest_error",
                {
                    "error": str(err),
                    "document_id": doc_id,
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
    """Update a document (rename and/or toggle scope)."""
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
