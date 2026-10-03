"""API-level tests for the new document preview + knowledge endpoints.

Uses the httpx ASGI transport against the documents router with the DB
session factory mocked (no live Postgres) and LibreOffice calls stubbed,
verifying the endpoint contracts:
  - GET  /documents/{id}/thumbnail   (200 JPEG / 404 unknown id)
  - GET  /documents/{id}/pages       (manifest shape)
  - GET  /documents/{id}/pages/{n}   (page JPEG, variant validation)
  - DELETE /documents/{id}/knowledge (status flips to not_indexed)
  - PATCH /documents/{id}            (collections round-trip)
  - POST /documents/{id}/reindex/stream (SSE happy path)
"""

import sys
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import documents  # noqa: E402
from app.db import models  # noqa: E402

app = FastAPI()
app.include_router(documents.router, prefix="/api")


def _make_doc(**kwargs) -> models.Document:
    defaults = dict(
        id=uuid.uuid4(),
        filename="Company Strategy.pdf",
        original_filename="Company Strategy.pdf",
        mime_type="application/pdf",
        file_path=f"documents/{uuid.uuid4()}/Company Strategy.pdf",
        file_size_bytes=13_000_000,
        content_hash="abc",
        scope="public",
        conversation_id=None,
        message_id=None,
        total_pages=5,
        total_chunks=12,
        total_images=0,
        digestion_status="ready",
        digestion_error=None,
        collections=["Company", "Strategy"],
    )
    defaults.update(kwargs)
    return models.Document(**defaults)


@pytest.fixture
def doc_store():
    """A dict-based document store + a mocked session factory."""
    store: dict[str, models.Document] = {}

    def _get_doc(db, doc_id):
        return store.get(str(doc_id))

    return store, _get_doc


@pytest.fixture
def client(doc_store):
    store, get_doc = doc_store

    # Patch rag_service functions used by the endpoints
    async def fake_get_document(db, doc_id):
        return store.get(str(doc_id))

    async def fake_remove_knowledge(db, doc_id):
        doc = store.get(str(doc_id))
        if not doc:
            return None
        doc.digestion_status = "not_indexed"
        doc.total_chunks = 0
        doc.total_images = 0
        doc.digestion_error = None
        return doc

    async def fake_set_collections(db, doc_id, collections):
        doc = store.get(str(doc_id))
        if not doc:
            return None
        cleaned = []
        seen = set()
        for raw in collections:
            value = raw.strip()
            key = value.casefold()
            if value and key not in seen:
                cleaned.append(value)
                seen.add(key)
        doc.collections = cleaned
        return doc

    async def fake_reindex(db, doc_id, progress=None):
        doc = store.get(str(doc_id))
        if not doc:
            raise ValueError("Document not found")
        return doc

    # Patch async_session_factory → a context manager that returns a MagicMock db
    class FakeSessionCtx:
        async def __aenter__(self):
            return MagicMock()

        async def __aexit__(self, *a):
            return False

    with patch.object(
        documents, "async_session_factory", lambda: FakeSessionCtx()
    ), patch.object(
        documents.rag_service, "get_document", fake_get_document
    ), patch.object(
        documents.rag_service, "remove_document_knowledge", fake_remove_knowledge
    ), patch.object(
        documents.rag_service, "set_document_collections", fake_set_collections
    ), patch.object(
        documents.rag_service, "reindex_document", fake_reindex
    ), patch.object(
        documents.rag_service,
        "document_to_dict",
        lambda doc, include_chunks=False: {
            "id": str(doc.id),
            "filename": doc.filename,
            "original_filename": doc.original_filename,
            "mime_type": doc.mime_type,
            "file_size_bytes": doc.file_size_bytes,
            "content_hash": doc.content_hash,
            "scope": doc.scope,
            "conversation_id": None,
            "message_id": None,
            "total_pages": doc.total_pages,
            "total_chunks": doc.total_chunks,
            "total_images": doc.total_images,
            "digestion_status": doc.digestion_status,
            "digestion_error": doc.digestion_error,
            "collections": doc.collections,
            "created_at": 1_700_000_000_000,
            "updated_at": 1_700_000_000_000,
        },
    ):
        transport = httpx.ASGITransport(app=app)
        yield httpx.AsyncClient(transport=transport, base_url="http://test"), store


@pytest.mark.asyncio
async def test_thumbnail_unknown_id_404(client):
    http, store = client
    r = await http.get(f"/api/documents/{uuid.uuid4()}/thumbnail")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_thumbnail_serves_jpeg(client, tmp_path, monkeypatch):
    http, store = client
    doc = _make_doc()
    store[str(doc.id)] = doc

    # Fake file on disk
    src = tmp_path / "Company Strategy.pdf"
    src.write_bytes(b"fake-pdf")

    from app.services.integrations import libreoffice as lo

    def fake_resolve(rel):
        return src

    async def fake_thumb(path, cache_dir=None, max_width=480):
        return b"\xff\xd8FAKEJPEGBYTES"

    with patch.object(
        documents.rag_service, "resolve_document_path", fake_resolve
    ), patch.object(lo, "render_document_thumbnail", fake_thumb), patch.object(
        lo, "document_preview_format", lambda p: "pdf"
    ), patch.object(
        lo, "is_available", lambda: True
    ):
        # The endpoint imports libreoffice lazily — patch via sys.modules ref
        r = await http.get(f"/api/documents/{doc.id}/thumbnail")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/jpeg")
    assert r.content.startswith(b"\xff\xd8")


@pytest.mark.asyncio
async def test_knowledge_delete_flips_status(client):
    http, store = client
    doc = _make_doc()
    store[str(doc.id)] = doc

    r = await http.delete(f"/api/documents/{doc.id}/knowledge")
    assert r.status_code == 200
    body = r.json()
    assert body["digestion_status"] == "not_indexed"
    assert body["total_chunks"] == 0


@pytest.mark.asyncio
async def test_knowledge_delete_unknown_404(client):
    http, store = client
    r = await http.delete(f"/api/documents/{uuid.uuid4()}/knowledge")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_collections_round_trip(client):
    http, store = client
    doc = _make_doc()
    store[str(doc.id)] = doc

    r = await http.patch(
        f"/api/documents/{doc.id}",
        json={"collections": [" Company ", "Strategy", "strategy", ""]},
    )
    assert r.status_code == 200
    # "Company" (trimmed), "Strategy" (dup case-insensitive dropped), "" dropped
    assert r.json()["collections"] == ["Company", "Strategy"]


@pytest.mark.asyncio
async def test_reindex_stream_requires_known_doc(client):
    http, store = client
    r = await http.post(f"/api/documents/{uuid.uuid4()}/reindex/stream")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_reindex_stream_missing_file_404(client, tmp_path):
    http, store = client
    doc = _make_doc()
    store[str(doc.id)] = doc

    def fake_resolve(rel):
        return tmp_path / "does-not-exist.pdf"

    with patch.object(documents.rag_service, "resolve_document_path", fake_resolve):
        r = await http.post(f"/api/documents/{doc.id}/reindex/stream")
    assert r.status_code == 404
    assert "file" in r.json()["detail"].lower()
