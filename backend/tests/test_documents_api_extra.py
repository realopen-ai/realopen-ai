"""Additional API tests for the documents router (app/api/documents.py).

The existing test_documents_workspace_api.py covers thumbnail 200/404,
knowledge-delete, patch-collections and the reindex pre-check 404s. This
file extends coverage to the remaining endpoints and branches:

  - POST /documents/upload          — validation (extension/scope/conv id),
                                      success, digestion failure → 500
  - POST /documents/upload/stream   — SSE happy path with progress events,
                                      digestion error event, "returned None"
                                      generic error, bad-scope 400, and a
                                      flaky progress queue (enqueue failure,
                                      sentinel-push failure, drain timeout)
  - GET  /documents                 — list + scope/conversation validation
  - GET  /documents/chunks/{id}/image — 400/404/200 + mime detection
  - GET  /documents/{id}            — 400/404/200 with chunks
  - GET  /documents/{id}/download   — 404 doc / 404 file / 200 FileResponse
  - GET  /documents/{id}/thumbnail  — no-preview-format 404, render-failed
                                      404 (LibreOffice installed vs not),
                                      missing file on disk 404
  - GET  /documents/{id}/pages      — manifest 200 + unavailable 404s
  - GET  /documents/{id}/pages/{n}  — variant/page validation, 404 page
                                      not available / not rendered, 200s
  - POST /documents/{id}/reindex/stream — SSE happy path, error event,
                                      reindex-returned-None generic error
  - PATCH /documents/{id}           — rename, scope toggle (ok/404/400/ValueError),
                                      collections, final-get 404
  - DELETE /documents/{id}          — 200 / 404
  - GET  /conversations/{id}/documents — 400 / 200
  - helpers _parse_uuid / _parse_optional_uuid / _validate_extension /
    _sse / _progress_to_sse / _log bad-format fallback

Mocks:
  - documents.async_session_factory → an in-memory FakeSession (no Postgres)
  - app.services.rag (rag_service) functions — no real digestion, no
    embeddings, no Ollama calls.
  - app.services.integrations.libreoffice — render/manifest calls return
    canned results; no soffice subprocess is ever spawned.
  - resolve_document_path → tmp_path files where a real file is needed.
"""

import asyncio
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import documents  # noqa: E402
from app.db import models  # noqa: E402
from app.services import rag as rag_service  # noqa: E402

app = FastAPI()
app.include_router(documents.router, prefix="/api")


# ── fakes ─────────────────────────────────────────────────────────────


class FakeResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class FakeSession:
    def __init__(self):
        self.execute_results = []
        self.commits = 0

    async def execute(self, stmt):
        if self.execute_results:
            item = self.execute_results.pop(0)
            return item(stmt) if callable(item) else item
        return FakeResult([])

    async def commit(self):
        self.commits += 1

    async def rollback(self):
        pass

    async def close(self):
        pass


def _make_chunk(doc_id, **kwargs) -> models.DocumentChunk:
    defaults = dict(
        id=uuid.uuid4(),
        document_id=doc_id,
        chunk_index=0,
        text="chunk text",
        page_number=1,
        line_start=1,
        line_end=2,
        chunk_type="text",
        embedding=None,
        image_path=None,
    )
    defaults.update(kwargs)
    return models.DocumentChunk(**defaults)


def _make_doc(**kwargs) -> models.Document:
    defaults = dict(
        id=uuid.uuid4(),
        filename="notes.txt",
        original_filename="notes.txt",
        mime_type="text/plain",
        file_path=f"documents/{uuid.uuid4()}/notes.txt",
        file_size_bytes=64,
        content_hash="deadbeef",
        scope="public",
        conversation_id=None,
        message_id=None,
        total_pages=1,
        total_chunks=2,
        total_images=0,
        digestion_status="ready",
        digestion_error=None,
        collections=[],
        created_at=datetime(2024, 3, 1, 9, 0, 0),
        updated_at=datetime(2024, 3, 1, 9, 0, 0),
    )
    defaults.update(kwargs)
    doc = models.Document(**defaults)
    return doc


@pytest_asyncio.fixture
async def client(tmp_path):
    session = FakeSession()

    class SessionCtx:
        async def __aenter__(self):
            return session

        async def __aexit__(self, *args):
            return False

    doc = _make_doc()

    async def fake_get_document(db, doc_id):
        return doc if str(doc_id) == str(doc.id) else None

    async def fake_digest(db, file_bytes, filename, scope, conversation_id,
                          progress=None):
        if progress:
            progress(
                rag_service.DigestProgress(
                    stage="started", percent=0, details=filename
                )
            )
            progress(
                rag_service.DigestProgress(
                    stage="done", percent=100, document_id=str(doc.id),
                    total_chunks=2,
                )
            )
        return doc

    async def fake_list(db, scope=None, conversation_id=None):
        return [doc]

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(documents, "async_session_factory", lambda: SessionCtx())
        mp.setattr(rag_service, "digest_document", fake_digest)
        mp.setattr(rag_service, "list_documents", fake_list)
        mp.setattr(rag_service, "get_document", fake_get_document)
        mp.setattr(
            rag_service, "resolve_document_path", lambda rel: tmp_path / "stored.bin"
        )
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://test"
        ) as c:
            yield c, session, doc, tmp_path


def _parse_sse(text: str) -> list:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


class FlakyQueue(asyncio.Queue):
    """asyncio.Queue whose first `fail_puts` put_nowait calls raise.

    Used to exercise the upload/reindex SSE endpoints' defensive branches
    around the progress queue (enqueue failure, sentinel-push failure).
    """

    fail_puts = 0

    def put_nowait(self, item):
        if type(self).fail_puts > 0:
            type(self).fail_puts -= 1
            raise RuntimeError("queue put failed")
        return super().put_nowait(item)


# ── helpers (unit level) ──────────────────────────────────────────────


def test_parse_uuid_accepts_valid():
    value = uuid.uuid4()
    assert documents._parse_uuid(str(value)) == value


@pytest.mark.parametrize("bad", ["nope", "", None, 42])
def test_parse_uuid_rejects_invalid(bad):
    with pytest.raises(Exception) as excinfo:
        documents._parse_uuid(bad)
    assert excinfo.value.status_code == 400


def test_parse_optional_uuid_none_passthrough():
    assert documents._parse_optional_uuid(None) is None
    assert documents._parse_optional_uuid("") is None


def test_parse_optional_uuid_invalid_raises_400():
    with pytest.raises(Exception) as excinfo:
        documents._parse_optional_uuid("xyz")
    assert "Invalid UUID" in excinfo.value.detail


@pytest.mark.parametrize(
    "filename",
    ["a.txt", "b.md", "c.csv", "d.pdf", "e.docx", "f.doc", "g.xlsx", "h.pptx",
     "I.PPT", "j.MarkDown"],
)
def test_validate_extension_accepts_supported(filename):
    assert documents._validate_extension(filename) is None


@pytest.mark.parametrize("filename", ["a.exe", "b.zip", "noext", "c.mp3"])
def test_validate_extension_rejects_unsupported(filename):
    with pytest.raises(Exception) as excinfo:
        documents._validate_extension(filename)
    assert "Unsupported file type" in excinfo.value.detail


def test_log_bad_format_args_fall_back_to_concatenation():
    # %d fed a str → TypeError → fallback path, no raise.
    assert documents._log("value=%d", "oops") is None
    # No args at all.
    assert documents._log("plain") is None


def test_sse_formatter_includes_event_and_fields():
    line = documents._sse("document_digest_done", {"document": {"id": "x"}})
    assert line.startswith("data: ")
    assert line.endswith("\n\n")
    payload = json.loads(line[len("data: ") :])
    assert payload["event"] == "document_digest_done"
    assert payload["document"] == {"id": "x"}


def test_progress_to_sse_maps_all_fields():
    p = rag_service.DigestProgress(
        stage="embedding", percent=42, details="batch 3",
        document_id="doc-1", total_chunks=7, total_images=2,
    )
    payload = json.loads(documents._progress_to_sse(p)[len("data: ") :])
    assert payload["event"] == "document_digest_progress"
    assert payload["stage"] == "embedding"
    assert payload["percent"] == 42
    assert payload["details"] == "batch 3"
    assert payload["document_id"] == "doc-1"
    assert payload["total_chunks"] == 7
    assert payload["total_images"] == 2


# ── POST /documents/upload ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_extension(client):
    http, *_ = client
    r = await http.post(
        "/api/documents/upload",
        files={"file": ("virus.exe", b"MZ", "application/octet-stream")},
        data={"scope": "public"},
    )
    assert r.status_code == 400
    assert "Unsupported file type" in r.json()["detail"]


@pytest.mark.asyncio
async def test_upload_rejects_bad_scope(client):
    http, *_ = client
    r = await http.post(
        "/api/documents/upload",
        files={"file": ("a.txt", b"hi", "text/plain")},
        data={"scope": "team"},
    )
    assert r.status_code == 400
    assert "scope must be" in r.json()["detail"]


@pytest.mark.asyncio
async def test_upload_private_requires_conversation(client):
    http, *_ = client
    r = await http.post(
        "/api/documents/upload",
        files={"file": ("a.txt", b"hi", "text/plain")},
        data={"scope": "private"},
    )
    assert r.status_code == 400
    assert "conversation_id is required" in r.json()["detail"]


@pytest.mark.asyncio
async def test_upload_rejects_invalid_conversation_uuid(client):
    http, *_ = client
    r = await http.post(
        "/api/documents/upload",
        files={"file": ("a.txt", b"hi", "text/plain")},
        data={"scope": "private", "conversation_id": "abc"},
    )
    assert r.status_code == 400
    assert "Invalid UUID" in r.json()["detail"]


@pytest.mark.asyncio
async def test_upload_public_success(client):
    http, session, doc, _ = client
    r = await http.post(
        "/api/documents/upload",
        files={"file": ("notes.txt", b"hello world", "text/plain")},
        data={"scope": "public"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(doc.id)
    assert body["digestion_status"] == "ready"
    assert "chunks" not in body  # include_chunks=False


@pytest.mark.asyncio
async def test_upload_private_with_conversation(client, monkeypatch):
    http, session, doc, _ = client
    conv_id = uuid.uuid4()
    captured = {}

    async def fake_digest(db, file_bytes, filename, scope, conversation_id,
                          progress=None):
        captured.update(scope=scope, conversation_id=conversation_id)
        return doc

    monkeypatch.setattr(rag_service, "digest_document", fake_digest)
    r = await http.post(
        "/api/documents/upload",
        files={"file": ("notes.txt", b"hi", "text/plain")},
        data={"scope": "private", "conversation_id": str(conv_id)},
    )
    assert r.status_code == 200
    assert captured == {"scope": "private", "conversation_id": conv_id}


@pytest.mark.asyncio
async def test_upload_digestion_failure_500(client, monkeypatch):
    http, *_ = client

    async def failing_digest(db, **kwargs):
        raise ValueError("extraction exploded")

    monkeypatch.setattr(rag_service, "digest_document", failing_digest)
    r = await http.post(
        "/api/documents/upload",
        files={"file": ("notes.txt", b"hi", "text/plain")},
        data={"scope": "public"},
    )
    assert r.status_code == 500
    assert "Digestion failed" in r.json()["detail"]
    assert "extraction exploded" in r.json()["detail"]


# ── POST /documents/upload/stream ─────────────────────────────────────


@pytest.mark.asyncio
async def test_upload_stream_validation_errors(client):
    http, *_ = client
    r = await http.post(
        "/api/documents/upload/stream",
        files={"file": ("a.exe", b"x", "application/octet-stream")},
        data={"scope": "public"},
    )
    assert r.status_code == 400
    r = await http.post(
        "/api/documents/upload/stream",
        files={"file": ("a.txt", b"x", "text/plain")},
        data={"scope": "private"},
    )
    assert r.status_code == 400
    r = await http.post(
        "/api/documents/upload/stream",
        files={"file": ("a.txt", b"x", "text/plain")},
        data={"scope": "team"},
    )
    assert r.status_code == 400
    assert "scope must be" in r.json()["detail"]


@pytest.mark.asyncio
async def test_upload_stream_emits_progress_and_done(client):
    http, session, doc, _ = client
    r = await http.post(
        "/api/documents/upload/stream",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"scope": "public"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(r.text)
    names = [e["event"] for e in events]
    assert names[0] == "document_digest_progress"
    assert names[-1] == "document_digest_done"
    assert names.count("document_digest_progress") == 2

    first = events[0]
    assert first["stage"] == "started"
    assert first["percent"] == 0
    done = events[-1]
    assert done["document"]["id"] == str(doc.id)


@pytest.mark.asyncio
async def test_upload_stream_emits_error_event_on_failure(client, monkeypatch):
    http, *_ = client

    async def failing_digest(db, **kwargs):
        raise RuntimeError("embedding backend down")

    monkeypatch.setattr(rag_service, "digest_document", failing_digest)
    r = await http.post(
        "/api/documents/upload/stream",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"scope": "public"},
    )
    events = _parse_sse(r.text)
    assert [e["event"] for e in events] == ["document_digest_error"]
    assert "embedding backend down" in events[0]["error"]


@pytest.mark.asyncio
async def test_upload_stream_digest_returning_none_is_generic_error(
    client, monkeypatch
):
    # A digestion that "succeeds" but yields no document row → the
    # catch-all error event, not a done event.
    http, *_ = client

    async def none_digest(db, **kwargs):
        return None

    monkeypatch.setattr(rag_service, "digest_document", none_digest)
    r = await http.post(
        "/api/documents/upload/stream",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"scope": "public"},
    )
    events = _parse_sse(r.text)
    assert [e["event"] for e in events] == ["document_digest_error"]
    assert "Unknown error" in events[0]["error"]
    assert "without result or error" in events[0]["error"]


@pytest.mark.asyncio
async def test_upload_stream_progress_enqueue_failure_is_dropped(client, monkeypatch):
    """The first progress event fails to enqueue → logged + skipped; the
    remaining events and the done event still flow."""
    http, *_ = client
    FlakyQueue.fail_puts = 1  # first put_nowait raises, later ones succeed
    monkeypatch.setattr(asyncio, "Queue", FlakyQueue)
    r = await http.post(
        "/api/documents/upload/stream",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"scope": "public"},
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)
    # "started" was dropped, "done" (the second progress) survived
    assert [e["event"] for e in events] == [
        "document_digest_progress",
        "document_digest_done",
    ]
    assert events[0]["stage"] == "done"


@pytest.mark.asyncio
async def test_upload_stream_survives_sentinel_push_failure(client, monkeypatch):
    """Every queue put fails (progress + sentinel). The drain loop times
    out, notices the finished task, breaks — and the done event is still
    emitted from the task's result."""
    http, *_ = client
    FlakyQueue.fail_puts = 99  # exhausts across all puts in this request
    monkeypatch.setattr(asyncio, "Queue", FlakyQueue)
    r = await http.post(
        "/api/documents/upload/stream",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"scope": "public"},
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert [e["event"] for e in events] == ["document_digest_done"]
    assert events[0]["document"]["id"]  # the doc id from the task result


@pytest.mark.asyncio
async def test_upload_stream_drain_retries_while_digestion_runs(client, monkeypatch):
    """A digestion slower than the 0.1s drain timeout: the loop keeps
    polling (timeout → continue) until the sentinel finally arrives."""
    http, session, doc, _ = client

    async def slow_silent_digest(db, **kwargs):
        await asyncio.sleep(0.25)  # > 2x the drain timeout
        return doc

    monkeypatch.setattr(rag_service, "digest_document", slow_silent_digest)
    r = await http.post(
        "/api/documents/upload/stream",
        files={"file": ("notes.txt", b"hello", "text/plain")},
        data={"scope": "public"},
    )
    assert r.status_code == 200
    events = _parse_sse(r.text)
    # No progress events were emitted — only the terminal done event.
    assert [e["event"] for e in events] == ["document_digest_done"]
    assert events[0]["document"]["id"] == str(doc.id)


# ── GET /documents ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_documents(client):
    http, session, doc, _ = client
    r = await http.get("/api/documents")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["documents"][0]["id"] == str(doc.id)


@pytest.mark.asyncio
async def test_list_documents_with_filters(client, monkeypatch):
    http, *_ = client
    conv_id = uuid.uuid4()
    captured = {}

    async def fake_list(db, scope=None, conversation_id=None):
        captured.update(scope=scope, conversation_id=conversation_id)
        return []

    monkeypatch.setattr(rag_service, "list_documents", fake_list)
    r = await http.get(
        f"/api/documents?scope=private&conversation_id={conv_id}"
    )
    assert r.status_code == 200
    assert r.json() == {"documents": [], "total": 0}
    assert captured == {"scope": "private", "conversation_id": conv_id}


@pytest.mark.asyncio
async def test_list_documents_rejects_bad_scope(client):
    http, *_ = client
    r = await http.get("/api/documents?scope=shared")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_list_documents_rejects_bad_conversation_uuid(client):
    http, *_ = client
    r = await http.get("/api/documents?conversation_id=zzz")
    assert r.status_code == 400


# ── GET /documents/chunks/{id}/image ──────────────────────────────────


@pytest.mark.asyncio
async def test_chunk_image_invalid_uuid_400(client):
    http, *_ = client
    r = await http.get("/api/documents/chunks/not-a-uuid/image")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_chunk_image_no_image_404(client, monkeypatch):
    http, *_ = client
    async def fake_path(db, chunk_id):
        return None

    monkeypatch.setattr(rag_service, "get_chunk_image_path", fake_path)
    r = await http.get(f"/api/documents/chunks/{uuid.uuid4()}/image")
    assert r.status_code == 404
    assert "no image" in r.json()["detail"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "ext,mime",
    [(".png", "image/png"), (".jpg", "image/jpeg"), (".webp", "image/webp"),
     (".bin", "image/png")],  # unknown extension → default png
)
async def test_chunk_image_serves_file(client, monkeypatch, tmp_path, ext, mime):
    http, *_ = client
    payload = b"\x89PNG-fakebytes"
    image_file = tmp_path / f"chunk-image{ext}"
    image_file.write_bytes(payload)

    async def fake_path(db, chunk_id):
        return "documents/x/img.png"

    monkeypatch.setattr(rag_service, "get_chunk_image_path", fake_path)
    monkeypatch.setattr(rag_service, "resolve_document_path", lambda rel: image_file)
    r = await http.get(f"/api/documents/chunks/{uuid.uuid4()}/image")
    assert r.status_code == 200
    assert r.content == payload
    assert r.headers["content-type"] == mime
    assert "max-age=86400" in r.headers["cache-control"]


@pytest.mark.asyncio
async def test_chunk_image_missing_on_disk_404(client, monkeypatch, tmp_path):
    http, *_ = client
    async def fake_path(db, chunk_id):
        return "documents/x/img.png"

    monkeypatch.setattr(rag_service, "get_chunk_image_path", fake_path)
    monkeypatch.setattr(
        rag_service, "resolve_document_path",
        lambda rel: tmp_path / "missing.png",
    )
    r = await http.get(f"/api/documents/chunks/{uuid.uuid4()}/image")
    assert r.status_code == 404
    assert "not found on disk" in r.json()["detail"].lower()


# ── GET /documents/{id} ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_document_invalid_uuid_400(client):
    http, *_ = client
    r = await http.get("/api/documents/xyz")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_get_document_unknown_404(client):
    http, *_ = client
    r = await http.get(f"/api/documents/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_get_document_with_chunks(client):
    http, session, doc, _ = client
    chunk = _make_chunk(
        doc.id, chunk_index=1, text="x" * 600, page_number=2,
        line_start=10, line_end=20,
    )
    doc.chunks = [chunk]
    session.execute_results.append(FakeResult([doc]))
    r = await http.get(f"/api/documents/{doc.id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(doc.id)
    assert len(body["chunks"]) == 1
    assert body["chunks"][0]["text"] == "x" * 500  # truncated
    assert body["chunks"][0]["has_image"] is False


# ── GET /documents/{id}/download ──────────────────────────────────────


@pytest.mark.asyncio
async def test_download_unknown_doc_404(client, monkeypatch):
    http, *_ = client
    async def none_doc(db, doc_id):
        return None

    monkeypatch.setattr(rag_service, "get_document", none_doc)
    r = await http.get(f"/api/documents/{uuid.uuid4()}/download")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_download_missing_file_404(client, monkeypatch, tmp_path):
    http, session, doc, _ = client
    monkeypatch.setattr(
        rag_service, "resolve_document_path",
        lambda rel: tmp_path / "not-there.bin",
    )
    r = await http.get(f"/api/documents/{doc.id}/download")
    assert r.status_code == 404
    assert "not found on disk" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_download_streams_file(client, tmp_path):
    http, session, doc, _ = client
    payload = b"raw document bytes"
    (tmp_path / "stored.bin").write_bytes(payload)
    r = await http.get(f"/api/documents/{doc.id}/download")
    assert r.status_code == 200
    assert r.content == payload
    assert r.headers["content-type"].startswith("text/plain")
    assert 'filename="notes.txt"' in r.headers.get("content-disposition", "")


# ── GET /documents/{id}/thumbnail ─────────────────────────────────────


@pytest_asyncio.fixture
async def preview_doc(client, tmp_path, monkeypatch):
    """A doc whose file exists on disk, with libreoffice patched out."""
    from app.services.integrations import libreoffice as lo

    http, session, doc, _ = client
    src = tmp_path / "notes.txt"
    src.write_bytes(b"hello world")
    monkeypatch.setattr(rag_service, "resolve_document_path", lambda rel: src)
    yield http, doc, lo, src


@pytest.mark.asyncio
async def test_thumbnail_file_missing_on_disk_404(client, monkeypatch, tmp_path):
    http, session, doc, _ = client
    monkeypatch.setattr(
        rag_service, "resolve_document_path", lambda rel: tmp_path / "gone.bin"
    )
    r = await http.get(f"/api/documents/{doc.id}/thumbnail")
    assert r.status_code == 404
    assert "not found on disk" in r.json()["detail"].lower()


@pytest.mark.asyncio
async def test_thumbnail_unpreviewable_format_404(preview_doc, monkeypatch):
    http, doc, lo, src = preview_doc
    monkeypatch.setattr(lo, "document_preview_format", lambda p: None)
    r = await http.get(f"/api/documents/{doc.id}/thumbnail")
    assert r.status_code == 404
    assert "No preview available for .txt files" in r.json()["detail"]


@pytest.mark.asyncio
async def test_thumbnail_render_failed_with_libreoffice_installed_404(
    preview_doc, monkeypatch
):
    http, doc, lo, src = preview_doc
    monkeypatch.setattr(lo, "document_preview_format", lambda p: "pdf")
    monkeypatch.setattr(lo, "is_available", lambda: True)

    async def failing_render(path, **kwargs):
        return None

    monkeypatch.setattr(lo, "render_document_thumbnail", failing_render)
    r = await http.get(f"/api/documents/{doc.id}/thumbnail")
    assert r.status_code == 404
    assert "rendering failed for this file" in r.json()["detail"]


@pytest.mark.asyncio
async def test_thumbnail_render_failed_without_libreoffice_404(
    preview_doc, monkeypatch
):
    http, doc, lo, src = preview_doc
    monkeypatch.setattr(lo, "document_preview_format", lambda p: "pdf")
    monkeypatch.setattr(lo, "is_available", lambda: False)

    async def failing_render(path, **kwargs):
        return None

    monkeypatch.setattr(lo, "render_document_thumbnail", failing_render)
    r = await http.get(f"/api/documents/{doc.id}/thumbnail")
    assert r.status_code == 404
    assert "LibreOffice is not installed" in r.json()["detail"]


# ── GET /documents/{id}/pages ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_pages_manifest_shape(preview_doc, monkeypatch):
    http, doc, lo, src = preview_doc

    async def fake_pages(path):
        return {"count": 3, "width": 800, "height": 600, "source_mtime": 1234}

    monkeypatch.setattr(lo, "render_document_pages", fake_pages)
    r = await http.get(f"/api/documents/{doc.id}/pages")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3
    assert body["width"] == 800
    assert body["height"] == 600
    assert body["source_mtime"] == 1234
    assert body["document_id"] == str(doc.id)
    assert body["filename"] == doc.filename


@pytest.mark.asyncio
async def test_pages_manifest_missing_width_defaults_to_zero(preview_doc, monkeypatch):
    http, doc, lo, src = preview_doc

    async def sparse_manifest(path):
        return {"count": 1}

    monkeypatch.setattr(lo, "render_document_pages", sparse_manifest)
    r = await http.get(f"/api/documents/{doc.id}/pages")
    assert r.status_code == 200
    body = r.json()
    assert body["width"] == 0
    assert body["height"] == 0
    assert body["source_mtime"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("available", [True, False])
async def test_pages_manifest_render_none_404(preview_doc, monkeypatch, available):
    http, doc, lo, src = preview_doc
    monkeypatch.setattr(lo, "is_available", lambda: available)

    async def no_pages(path):
        return None

    monkeypatch.setattr(lo, "render_document_pages", no_pages)
    r = await http.get(f"/api/documents/{doc.id}/pages")
    assert r.status_code == 404
    expected = "this file could not be rendered" if available else (
        "LibreOffice is not installed"
    )
    assert expected in r.json()["detail"]


# ── GET /documents/{id}/pages/{n} ─────────────────────────────────────


@pytest.mark.asyncio
async def test_page_image_rejects_bad_variant(client):
    http, *_ = client
    r = await http.get(f"/api/documents/{uuid.uuid4()}/pages/1?variant=huge")
    assert r.status_code == 400
    assert "variant" in r.json()["detail"]


@pytest.mark.asyncio
async def test_page_image_rejects_zero_page(client):
    http, *_ = client
    r = await http.get(f"/api/documents/{uuid.uuid4()}/pages/0")
    assert r.status_code == 400
    assert "page_number must be >= 1" in r.json()["detail"]


@pytest.mark.asyncio
async def test_page_image_beyond_count_404(preview_doc, monkeypatch):
    http, doc, lo, src = preview_doc

    async def two_pages(path):
        return {"count": 2}

    monkeypatch.setattr(lo, "render_document_pages", two_pages)
    r = await http.get(f"/api/documents/{doc.id}/pages/3")
    assert r.status_code == 404
    assert "Page 3 not available" in r.json()["detail"]


@pytest.mark.asyncio
async def test_page_image_not_rendered_yet_404(preview_doc, monkeypatch, tmp_path):
    http, doc, lo, src = preview_doc

    async def one_page(path):
        return {"count": 1}

    monkeypatch.setattr(lo, "render_document_pages", one_page)
    monkeypatch.setattr(lo, "document_preview_cache_dir", lambda p: tmp_path / "cache")
    r = await http.get(f"/api/documents/{doc.id}/pages/1")
    assert r.status_code == 404
    assert "Page 1 not rendered" in r.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "variant,expected_name",
    [("full", "slide_001.jpg"), ("thumb", "slide_001_t.jpg")],
)
async def test_page_image_serves_jpeg(
    preview_doc, monkeypatch, tmp_path, variant, expected_name
):
    http, doc, lo, src = preview_doc
    cache = tmp_path / "cache"
    cache.mkdir()
    payload = b"\xff\xd8fakejpeg"
    (cache / expected_name).write_bytes(payload)

    async def one_page(path):
        return {"count": 1}

    monkeypatch.setattr(lo, "render_document_pages", one_page)
    monkeypatch.setattr(lo, "document_preview_cache_dir", lambda p: cache)
    r = await http.get(f"/api/documents/{doc.id}/pages/1?variant={variant}")
    assert r.status_code == 200
    assert r.content == payload
    assert r.headers["content-type"].startswith("image/jpeg")
    assert "max-age=86400" in r.headers["cache-control"]


# ── POST /documents/{id}/reindex/stream ───────────────────────────────


@pytest.mark.asyncio
async def test_reindex_stream_happy_path(client, monkeypatch, tmp_path):
    http, session, doc, _ = client
    src = tmp_path / "notes.txt"
    src.write_bytes(b"reindex me")
    monkeypatch.setattr(rag_service, "resolve_document_path", lambda rel: src)

    async def fake_reindex(db, doc_id, progress=None):
        assert str(doc_id) == str(doc.id)
        if progress:
            progress(
                rag_service.DigestProgress(stage="chunking", percent=50)
            )
            progress(
                rag_service.DigestProgress(
                    stage="done", percent=100, document_id=str(doc.id)
                )
            )
        return doc

    monkeypatch.setattr(rag_service, "reindex_document", fake_reindex)
    r = await http.post(f"/api/documents/{doc.id}/reindex/stream")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(r.text)
    names = [e["event"] for e in events]
    assert names[0] == "document_digest_progress"
    assert names[-1] == "document_digest_done"
    assert events[0]["stage"] == "chunking"
    assert events[-1]["document"]["id"] == str(doc.id)


@pytest.mark.asyncio
async def test_reindex_stream_emits_error_event(client, monkeypatch, tmp_path):
    http, session, doc, _ = client
    src = tmp_path / "notes.txt"
    src.write_bytes(b"reindex me")
    monkeypatch.setattr(rag_service, "resolve_document_path", lambda rel: src)

    async def failing_reindex(db, doc_id, progress=None):
        raise RuntimeError("reindex exploded")

    monkeypatch.setattr(rag_service, "reindex_document", failing_reindex)
    r = await http.post(f"/api/documents/{doc.id}/reindex/stream")
    events = _parse_sse(r.text)
    assert [e["event"] for e in events] == ["document_digest_error"]
    assert "reindex exploded" in events[0]["error"]
    assert events[0]["document_id"] == str(doc.id)


@pytest.mark.asyncio
async def test_reindex_stream_none_result_is_generic_error(client, monkeypatch, tmp_path):
    http, session, doc, _ = client
    src = tmp_path / "notes.txt"
    src.write_bytes(b"reindex me")
    monkeypatch.setattr(rag_service, "resolve_document_path", lambda rel: src)

    async def none_reindex(db, doc_id, progress=None):
        return None

    monkeypatch.setattr(rag_service, "reindex_document", none_reindex)
    r = await http.post(f"/api/documents/{doc.id}/reindex/stream")
    events = _parse_sse(r.text)
    assert [e["event"] for e in events] == ["document_digest_error"]
    assert "Unknown error" in events[0]["error"]
    assert "reindex completed without result" in events[0]["error"]


@pytest.mark.asyncio
async def test_reindex_stream_invalid_uuid_400(client):
    http, *_ = client
    r = await http.post("/api/documents/bad-id/reindex/stream")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_reindex_stream_progress_enqueue_failure_is_dropped(
    client, monkeypatch, tmp_path
):
    http, session, doc, _ = client
    src = tmp_path / "notes.txt"
    src.write_bytes(b"reindex me")
    monkeypatch.setattr(rag_service, "resolve_document_path", lambda rel: src)

    async def fake_reindex(db, doc_id, progress=None):
        if progress:
            progress(rag_service.DigestProgress(stage="chunking", percent=50))
            progress(
                rag_service.DigestProgress(
                    stage="done", percent=100, document_id=str(doc.id)
                )
            )
        return doc

    monkeypatch.setattr(rag_service, "reindex_document", fake_reindex)
    FlakyQueue.fail_puts = 1  # first progress event fails to enqueue
    monkeypatch.setattr(asyncio, "Queue", FlakyQueue)
    r = await http.post(f"/api/documents/{doc.id}/reindex/stream")
    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert [e["event"] for e in events] == [
        "document_digest_progress",
        "document_digest_done",
    ]
    assert events[0]["stage"] == "done"  # "chunking" was the dropped one


@pytest.mark.asyncio
async def test_reindex_stream_survives_sentinel_push_failure(
    client, monkeypatch, tmp_path
):
    http, session, doc, _ = client
    src = tmp_path / "notes.txt"
    src.write_bytes(b"reindex me")
    monkeypatch.setattr(rag_service, "resolve_document_path", lambda rel: src)

    async def fake_reindex(db, doc_id, progress=None):
        return doc

    monkeypatch.setattr(rag_service, "reindex_document", fake_reindex)
    FlakyQueue.fail_puts = 99  # every put fails — sentinel included
    monkeypatch.setattr(asyncio, "Queue", FlakyQueue)
    r = await http.post(f"/api/documents/{doc.id}/reindex/stream")
    assert r.status_code == 200
    events = _parse_sse(r.text)
    # Drain loop timed out, saw the finished task, broke; result reported.
    assert [e["event"] for e in events] == ["document_digest_done"]
    assert events[0]["document"]["id"] == str(doc.id)


@pytest.mark.asyncio
async def test_reindex_stream_drain_retries_while_reindex_runs(
    client, monkeypatch, tmp_path
):
    http, session, doc, _ = client
    src = tmp_path / "notes.txt"
    src.write_bytes(b"reindex me")
    monkeypatch.setattr(rag_service, "resolve_document_path", lambda rel: src)

    async def slow_reindex(db, doc_id, progress=None):
        await asyncio.sleep(0.25)  # > 2x the drain timeout, no progress
        return doc

    monkeypatch.setattr(rag_service, "reindex_document", slow_reindex)
    r = await http.post(f"/api/documents/{doc.id}/reindex/stream")
    assert r.status_code == 200
    events = _parse_sse(r.text)
    assert [e["event"] for e in events] == ["document_digest_done"]
    assert events[0]["document"]["id"] == str(doc.id)


# ── PATCH /documents/{id} ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_patch_rename(client, monkeypatch):
    http, session, doc, _ = client
    async def fake_rename(db, doc_id, filename):
        doc.filename = filename
        return doc

    monkeypatch.setattr(rag_service, "rename_document", fake_rename)
    r = await http.patch(f"/api/documents/{doc.id}", json={"filename": "New Name.txt"})
    assert r.status_code == 200
    assert r.json()["filename"] == "New Name.txt"


@pytest.mark.asyncio
async def test_patch_rename_unknown_404(client, monkeypatch):
    http, *_ = client
    async def none_rename(db, doc_id, filename):
        return None

    monkeypatch.setattr(rag_service, "rename_document", none_rename)
    r = await http.patch(
        f"/api/documents/{uuid.uuid4()}", json={"filename": "x.txt"}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_rejects_bad_scope(client):
    http, *_ = client
    r = await http.patch(
        f"/api/documents/{uuid.uuid4()}", json={"scope": "shared"}
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_patch_private_requires_conversation(client):
    http, *_ = client
    r = await http.patch(
        f"/api/documents/{uuid.uuid4()}", json={"scope": "private"}
    )
    assert r.status_code == 400
    assert "conversation_id is required" in r.json()["detail"]


@pytest.mark.asyncio
async def test_patch_rejects_invalid_conversation_uuid(client):
    http, *_ = client
    r = await http.patch(
        f"/api/documents/{uuid.uuid4()}",
        json={"scope": "private", "conversation_id": "bad"},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_patch_scope_toggle(client, monkeypatch):
    http, session, doc, _ = client
    conv_id = uuid.uuid4()
    captured = {}

    async def fake_toggle(db, doc_id, scope, conversation_id):
        captured.update(scope=scope, conversation_id=conversation_id)
        doc.scope = scope
        return doc

    monkeypatch.setattr(rag_service, "toggle_document_scope", fake_toggle)
    r = await http.patch(
        f"/api/documents/{doc.id}",
        json={"scope": "private", "conversation_id": str(conv_id)},
    )
    assert r.status_code == 200
    assert r.json()["scope"] == "private"
    assert captured == {"scope": "private", "conversation_id": conv_id}


@pytest.mark.asyncio
async def test_patch_toggle_unknown_404(client, monkeypatch):
    http, *_ = client
    async def none_toggle(db, doc_id, scope, conversation_id):
        return None

    monkeypatch.setattr(rag_service, "toggle_document_scope", none_toggle)
    r = await http.patch(
        f"/api/documents/{uuid.uuid4()}", json={"scope": "public"}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_toggle_value_error_400(client, monkeypatch):
    http, *_ = client
    async def bad_toggle(db, doc_id, scope, conversation_id):
        raise ValueError("cannot detach from conversation")

    monkeypatch.setattr(rag_service, "toggle_document_scope", bad_toggle)
    r = await http.patch(
        f"/api/documents/{uuid.uuid4()}", json={"scope": "public"}
    )
    assert r.status_code == 400
    assert "cannot detach" in r.json()["detail"]


@pytest.mark.asyncio
async def test_patch_collections_unknown_404(client, monkeypatch):
    http, *_ = client
    async def none_collections(db, doc_id, collections):
        return None

    monkeypatch.setattr(rag_service, "set_document_collections", none_collections)
    r = await http.patch(
        f"/api/documents/{uuid.uuid4()}", json={"collections": ["a"]}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_patch_combined_update(client, monkeypatch):
    http, session, doc, _ = client

    async def fake_rename(db, doc_id, filename):
        doc.filename = filename
        return doc

    async def fake_toggle(db, doc_id, scope, conversation_id):
        doc.scope = scope
        return doc

    async def fake_collections(db, doc_id, collections):
        doc.collections = collections
        return doc

    monkeypatch.setattr(rag_service, "rename_document", fake_rename)
    monkeypatch.setattr(rag_service, "toggle_document_scope", fake_toggle)
    monkeypatch.setattr(
        rag_service, "set_document_collections", fake_collections
    )
    r = await http.patch(
        f"/api/documents/{doc.id}",
        json={
            "filename": "renamed.txt",
            "scope": "public",
            "collections": ["Knowledge"],
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["filename"] == "renamed.txt"
    assert body["scope"] == "public"
    assert body["collections"] == ["Knowledge"]


@pytest.mark.asyncio
async def test_patch_final_get_missing_404(client, monkeypatch):
    http, *_ = client
    calls = {"n": 0}

    async def fake_rename(db, doc_id, filename):
        return object()  # rename "succeeds"

    async def vanishing_get(db, doc_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the final refresh can't find the doc anymore
        return None

    monkeypatch.setattr(rag_service, "rename_document", fake_rename)
    monkeypatch.setattr(rag_service, "get_document", vanishing_get)
    r = await http.patch(
        f"/api/documents/{uuid.uuid4()}", json={"filename": "x.txt"}
    )
    assert r.status_code == 404


# ── DELETE /documents/{id} ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_document_success(client, monkeypatch):
    http, *_ = client
    async def ok_delete(db, doc_id):
        return True

    monkeypatch.setattr(rag_service, "delete_document", ok_delete)
    doc_id = uuid.uuid4()
    r = await http.delete(f"/api/documents/{doc_id}")
    assert r.status_code == 200
    assert r.json() == {"status": "deleted", "id": str(doc_id)}


@pytest.mark.asyncio
async def test_delete_document_unknown_404(client, monkeypatch):
    http, *_ = client
    async def nope_delete(db, doc_id):
        return False

    monkeypatch.setattr(rag_service, "delete_document", nope_delete)
    r = await http.delete(f"/api/documents/{uuid.uuid4()}")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_delete_document_invalid_uuid_400(client):
    http, *_ = client
    r = await http.delete("/api/documents/oops")
    assert r.status_code == 400


# ── GET /conversations/{id}/documents ─────────────────────────────────


@pytest.mark.asyncio
async def test_conversation_documents_invalid_uuid_400(client):
    http, *_ = client
    r = await http.get("/api/conversations/bad-id/documents")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_conversation_documents_lists_rows(client):
    http, session, doc, _ = client
    session.execute_results.append(FakeResult([doc]))
    r = await http.get(f"/api/conversations/{uuid.uuid4()}/documents")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    assert body["documents"][0]["id"] == str(doc.id)
    # chunks are included by default in this listing
    assert "chunks" not in body["documents"][0]


@pytest.mark.asyncio
async def test_conversation_documents_empty(client):
    http, session, _, _ = client
    session.execute_results.append(FakeResult([]))
    r = await http.get(f"/api/conversations/{uuid.uuid4()}/documents")
    assert r.json() == {"documents": [], "total": 0}
