"""
API-level tests for app/api/workspace.py — PPTX template management,
template validation, thumbnails and the generated-files listing.

Endpoints covered (httpx + ASGITransport against the real router):
  - GET    /api/workspace/templates              list + serialization shape
  - GET    /api/workspace/templates/{id}         400 invalid UUID / 404 / 200
  - POST   /api/workspace/templates              409 slug collision / 200
                                                 (file written, tags parsed,
                                                 auto-thumbnail fallback)
  - PUT    /api/workspace/templates/{id}         rename + file move, 409
                                                 collision, default-template
                                                 protection, metadata updates
  - DELETE /api/workspace/templates/{id}         file removed from disk,
                                                 default template untouched
  - POST   /api/workspace/templates/validate     real python-pptx validation
                                                 (valid 16:9 deck, empty file,
                                                 corrupt file, missing title /
                                                 content layouts, placeholder-
                                                 free blank layout; fake-pptx
                                                 doubles for <3 layouts, add-
                                                 slide failure and python-pptx
                                                 missing)
  - GET    /api/workspace/generated-files        row mapping, file_type SQL
                                                 filter, file sizes, pptx
                                                 thumbnail URLs
  - POST   /api/workspace/templates/{id}/thumbnail  resize + re-encode to
                                                 200x150 JPEG base64; Pillow
                                                 missing → 500

What is mocked:
  - get_db dependency → FakeSession with queued results (no live Postgres)
  - workspace._get_templates_dir / _get_data_dir → pytest tmp_path (no
    writes into the repository, no real data dir)
  - app.services.thumbnail_gen.generate_schematic_thumbnail → constant
    (auto-thumbnail fallback path)

python-pptx and Pillow are exercised for real (pure in-memory/bytes work).
"""

import base64
import io
import sys
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from PIL import Image

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import workspace  # noqa: E402
from app.db.session import get_db  # noqa: E402

app = FastAPI()
app.include_router(workspace.router, prefix="/api")


# ══════════════════════════════════════════════════════════════════════
# Fakes
# ══════════════════════════════════════════════════════════════════════


class FakeResult:
    def __init__(self, value=None):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return self

    def all(self):
        return self._value


class FakeSession:
    """AsyncSession stand-in with a queue of prepared results."""

    def __init__(self, results=None):
        self._results = list(results or [])
        self.executed = []
        self.added = []
        self.flush_count = 0

    async def execute(self, stmt, *a, **kw):
        self.executed.append(stmt)
        if self._results:
            return self._results.pop(0)
        return FakeResult(None)

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        self.flush_count += 1


class SessionCtx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


def make_template(**kw):
    now = datetime.utcnow()
    data = dict(
        id=uuid.uuid4(),
        display_name="Quarterly Review",
        slug="quarterly_review",
        description=None,
        tags=["finance"],
        thumbnail=None,
        path="quarterly_review.pptx",
        created_at=now,
        updated_at=now,
    )
    data.update(kw)
    return SimpleNamespace(**data)


def valid_pptx_bytes() -> bytes:
    """A real 16:9 PPTX with standard title+content layouts (python-pptx)."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def _mutated_deck(mutate) -> bytes:
    """Build a 16:9 deck and apply an in-place mutation to its layouts."""
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width = Inches(13.333)
    prs.slide_height = Inches(7.5)
    mutate(prs)
    buf = io.BytesIO()
    prs.save(buf)
    return buf.getvalue()


def deck_with_placeholderless_blank_layout() -> bytes:
    """Valid 16:9 deck whose Blank layout has no placeholders at all."""

    def mutate(prs):
        blank = prs.slide_layouts[6]  # "Blank Layout" in the default template
        for ph in list(blank.placeholders):
            ph._element.getparent().remove(ph._element)

    return _mutated_deck(mutate)


def deck_without_title_placeholders() -> bytes:
    """Valid 16:9 deck where every layout's title (idx=0) placeholder is gone."""

    def mutate(prs):
        for layout in prs.slide_layouts:
            for ph in list(layout.placeholders):
                if ph.placeholder_format.idx == 0:
                    ph._element.getparent().remove(ph._element)

    return _mutated_deck(mutate)


def deck_without_content_layout() -> bytes:
    """Valid 16:9 deck with title-only layouts (no layout has idx=0 AND idx=1)."""

    def mutate(prs):
        for layout in prs.slide_layouts:
            idxs = {ph.placeholder_format.idx for ph in layout.placeholders}
            if {0, 1} <= idxs:
                for ph in list(layout.placeholders):
                    if ph.placeholder_format.idx == 1:
                        ph._element.getparent().remove(ph._element)

    return _mutated_deck(mutate)


@pytest.fixture
def dirs(monkeypatch, tmp_path):
    templates_dir = tmp_path / "templates"
    data_dir = tmp_path / "data"
    templates_dir.mkdir()
    data_dir.mkdir()
    monkeypatch.setattr(workspace, "_get_templates_dir", lambda: templates_dir)
    monkeypatch.setattr(workspace, "_get_data_dir", lambda: data_dir)
    return SimpleNamespace(templates=templates_dir, data=data_dir)


@pytest.fixture
def db():
    session = FakeSession()

    async def _override():
        yield session

    app.dependency_overrides[get_db] = _override
    yield session
    app.dependency_overrides.clear()


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url="http://test")


# ══════════════════════════════════════════════════════════════════════
# Pure helpers
# ══════════════════════════════════════════════════════════════════════


def test_slugify_normalizes_names():
    assert workspace._slugify("Quarterly Review") == "quarterly_review"
    assert workspace._slugify("  My-Cool  Deck! ") == "my_cool_deck"
    assert workspace._slugify("!!!") == "template"
    assert workspace._slugify("Ünïcode?") == "ünïcode"  # unicode word chars kept


def test_get_templates_dir_points_at_custom_templates():
    p = workspace._get_templates_dir()
    assert isinstance(p, Path)
    assert p.parts[-3:] == ("templates", "pptx", "custom")


class _FakePath:
    """Path stand-in for _get_data_dir (parent chains collapse onto self)."""

    def __init__(self, p):
        self.p = str(p)

    def resolve(self):
        return self

    @property
    def parent(self):
        return self

    def __truediv__(self, other):
        return _FakePath(self.p + "/" + str(other))

    def exists(self):
        return self.p == "/app/data"

    def mkdir(self, parents=True, exist_ok=True):
        raise AssertionError("mkdir must not run when a candidate exists")


def test_get_data_dir_returns_first_existing_candidate(monkeypatch):
    monkeypatch.setattr(workspace, "Path", _FakePath)
    assert workspace._get_data_dir().p == "/app/data"


def test_get_data_dir_fallback_creates_missing_dir(monkeypatch):
    created = []

    class _MissingPath(_FakePath):
        def exists(self):
            return False

        def mkdir(self, parents=True, exist_ok=True):
            created.append(self.p)

    monkeypatch.setattr(workspace, "Path", _MissingPath)
    result = workspace._get_data_dir()
    assert created == ["/app/data"]
    assert result.p == "/app/data"


# ══════════════════════════════════════════════════════════════════════
# GET templates
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_templates_empty(db, client):
    db._results = [FakeResult([])]
    r = await client.get("/api/workspace/templates")
    assert r.status_code == 200
    assert r.json() == {"templates": []}


@pytest.mark.asyncio
async def test_list_templates_serializes_rows(db, client):
    t = make_template()
    db._results = [FakeResult([t])]
    r = await client.get("/api/workspace/templates")
    assert r.status_code == 200
    body = r.json()
    assert len(body["templates"]) == 1
    row = body["templates"][0]
    assert row["id"] == str(t.id)
    assert row["display_name"] == "Quarterly Review"
    assert row["slug"] == "quarterly_review"
    assert row["tags"] == ["finance"]
    assert row["path"] == "quarterly_review.pptx"
    assert row["created_at"] == int(t.created_at.timestamp() * 1000)


@pytest.mark.asyncio
async def test_get_template_by_id(db, client):
    t = make_template()
    db._results = [FakeResult(t)]
    r = await client.get(f"/api/workspace/templates/{t.id}")
    assert r.status_code == 200
    assert r.json()["id"] == str(t.id)


@pytest.mark.asyncio
async def test_get_template_invalid_uuid_400(db, client):
    r = await client.get("/api/workspace/templates/not-a-uuid")
    assert r.status_code == 400
    assert r.json()["detail"] == "Invalid template ID"


@pytest.mark.asyncio
async def test_get_template_unknown_404(db, client):
    db._results = [FakeResult(None)]
    r = await client.get(f"/api/workspace/templates/{uuid.uuid4()}")
    assert r.status_code == 404
    assert r.json()["detail"] == "Template not found"


# ══════════════════════════════════════════════════════════════════════
# POST templates
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_template_writes_file_and_row(db, client, dirs):
    db._results = [FakeResult(None)]  # no slug collision
    payload = b"PK\x03\x04 fake pptx"
    r = await client.post(
        "/api/workspace/templates",
        data={
            "display_name": "Quarterly Review",
            "description": "Finance deck",
            "tags": "finance, review",
            "thumbnail": "b64provided",
        },
        files={"file": ("q.pptx", payload, "application/vnd.ms-powerpoint")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["slug"] == "quarterly_review"
    assert body["display_name"] == "Quarterly Review"
    assert body["description"] == "Finance deck"
    assert body["tags"] == ["finance", "review"]
    assert body["thumbnail"] == "b64provided"
    assert body["path"] == "quarterly_review.pptx"
    # file written to the (patched) templates dir
    saved = dirs.templates / "quarterly_review.pptx"
    assert saved.read_bytes() == payload
    # DB row added + flushed
    assert len(db.added) == 1 and db.flush_count == 1


@pytest.mark.asyncio
async def test_create_template_slug_collision_409(db, client, dirs):
    db._results = [FakeResult(make_template())]  # existing slug row
    r = await client.post(
        "/api/workspace/templates",
        data={"display_name": "Quarterly Review"},
        files={"file": ("q.pptx", b"data", "application/vnd.ms-powerpoint")},
    )
    assert r.status_code == 409
    assert "already exists" in r.json()["detail"]
    assert db.added == []
    assert not (dirs.templates / "quarterly_review.pptx").exists()


@pytest.mark.asyncio
async def test_create_template_auto_thumbnail_fallback(db, client, dirs, monkeypatch):
    import app.services.thumbnail_gen as thumb_mod

    db._results = [FakeResult(None)]
    monkeypatch.setattr(
        thumb_mod, "generate_schematic_thumbnail", lambda path, name: "b64auto"
    )
    r = await client.post(
        "/api/workspace/templates",
        data={"display_name": "Auto Thumb"},
        files={"file": ("a.pptx", b"data", "application/vnd.ms-powerpoint")},
    )
    assert r.status_code == 200
    assert r.json()["thumbnail"] == "b64auto"


@pytest.mark.asyncio
async def test_create_template_auto_thumbnail_failure_is_non_fatal(
    db, client, dirs, monkeypatch
):
    import app.services.thumbnail_gen as thumb_mod

    def broken(path, name):
        raise RuntimeError("no fontconfig")

    db._results = [FakeResult(None)]
    monkeypatch.setattr(thumb_mod, "generate_schematic_thumbnail", broken)
    r = await client.post(
        "/api/workspace/templates",
        data={"display_name": "Auto Thumb"},
        files={"file": ("a.pptx", b"data", "application/vnd.ms-powerpoint")},
    )
    assert r.status_code == 200
    assert r.json()["thumbnail"] is None


# ══════════════════════════════════════════════════════════════════════
# PUT templates
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_update_template_rename_moves_file(db, client, dirs):
    t = make_template(
        display_name="Old Name", slug="old_name", path="old_name.pptx"
    )
    old_file = dirs.templates / "old_name.pptx"
    old_file.write_bytes(b"payload")
    db._results = [FakeResult(t), FakeResult(None)]  # lookup, no collision

    r = await client.put(
        f"/api/workspace/templates/{t.id}",
        data={"display_name": "New Name"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["slug"] == "new_name"
    assert body["display_name"] == "New Name"
    assert body["path"] == "new_name.pptx"
    assert not old_file.exists()
    assert (dirs.templates / "new_name.pptx").read_bytes() == b"payload"
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_update_template_rename_collision_409(db, client, dirs):
    t = make_template(display_name="Old Name", slug="old_name")
    db._results = [FakeResult(t), FakeResult(make_template())]  # collision
    r = await client.put(
        f"/api/workspace/templates/{t.id}", data={"display_name": "New Name"}
    )
    assert r.status_code == 409
    assert "already in use" in r.json()["detail"]


@pytest.mark.asyncio
async def test_update_template_metadata_only(db, client, dirs):
    t = make_template()
    db._results = [FakeResult(t), FakeResult(None)]
    r = await client.put(
        f"/api/workspace/templates/{t.id}",
        data={
            "description": "New desc",
            "tags": "a, b",
            "thumbnail": "b64new",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["description"] == "New desc"
    assert body["tags"] == ["a", "b"]
    assert body["thumbnail"] == "b64new"
    assert body["slug"] == "quarterly_review"  # unchanged


@pytest.mark.asyncio
async def test_update_template_default_template_never_renamed(db, client, dirs):
    t = make_template(
        display_name="Corporate", slug="corporate", path="corporate.pptx"
    )
    db._results = [FakeResult(t)]
    r = await client.put(
        f"/api/workspace/templates/{t.id}", data={"display_name": "Renamed Default"}
    )
    assert r.status_code == 200
    body = r.json()
    # slug in AVAILABLE_TEMPLATES → display_name/slug/path stay untouched
    assert body["slug"] == "corporate"
    assert body["display_name"] == "Corporate"


@pytest.mark.asyncio
async def test_update_template_invalid_uuid_400(db, client):
    r = await client.put(
        "/api/workspace/templates/bogus", data={"description": "x"}
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_update_template_unknown_404(db, client):
    db._results = [FakeResult(None)]
    r = await client.put(
        f"/api/workspace/templates/{uuid.uuid4()}", data={"description": "x"}
    )
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# DELETE templates
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_delete_template_removes_file_and_row(db, client, dirs):
    t = make_template(slug="custom_one", path="custom_one.pptx")
    target = dirs.templates / "custom_one.pptx"
    target.write_bytes(b"bye")
    db._results = [FakeResult(t), FakeResult(None)]

    r = await client.delete(f"/api/workspace/templates/{t.id}")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "deleted": str(t.id)}
    assert not target.exists()
    assert db.flush_count == 1


@pytest.mark.asyncio
async def test_delete_template_default_keeps_file(db, client, dirs):
    t = make_template(slug="modern", path="modern.pptx")
    target = dirs.templates / "modern.pptx"
    target.write_bytes(b"keep-me")
    db._results = [FakeResult(t), FakeResult(None)]

    r = await client.delete(f"/api/workspace/templates/{t.id}")
    assert r.status_code == 200
    assert target.exists()  # default template file untouched


@pytest.mark.asyncio
async def test_delete_template_missing_file_still_ok(db, client, dirs):
    t = make_template(slug="ghost", path="ghost.pptx")
    db._results = [FakeResult(t), FakeResult(None)]
    r = await client.delete(f"/api/workspace/templates/{t.id}")
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_delete_template_invalid_uuid_400(db, client):
    r = await client.delete("/api/workspace/templates/nope")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_delete_template_unknown_404(db, client):
    db._results = [FakeResult(None)]
    r = await client.delete(f"/api/workspace/templates/{uuid.uuid4()}")
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# POST templates/validate
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_validate_accepts_16x9_deck_with_standard_layouts(client):
    r = await client.post(
        "/api/workspace/templates/validate",
        files={"file": ("deck.pptx", valid_pptx_bytes(), "application/x-pptx")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is True
    details = body["details"]
    assert details["layouts"] >= 3
    assert details["title_layout_index"] == 0
    # default template's layout 0 carries both idx=0 and idx=1 placeholders,
    # so it also satisfies the content-layout check
    assert details["content_layout_index"] == 0
    assert details["aspect_ratio"] == "1.778"


@pytest.mark.asyncio
async def test_validate_rejects_empty_upload(client):
    r = await client.post(
        "/api/workspace/templates/validate",
        files={"file": ("empty.pptx", b"", "application/x-pptx")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert body["error"] == "Empty file upload"


@pytest.mark.asyncio
async def test_validate_rejects_corrupt_file(client):
    r = await client.post(
        "/api/workspace/templates/validate",
        files={"file": ("junk.pptx", b"definitely not a zip", "application/x-pptx")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["valid"] is False
    assert "Failed to open PPTX" in body["error"]


@pytest.mark.asyncio
async def test_validate_rejects_4x3_aspect_ratio(client):
    from pptx import Presentation

    prs = Presentation()  # default 4:3 (10in x 7.5in)
    buf = io.BytesIO()
    prs.save(buf)
    r = await client.post(
        "/api/workspace/templates/validate",
        files={"file": ("deck.pptx", buf.getvalue(), "application/x-pptx")},
    )
    body = r.json()
    assert body["valid"] is False
    assert "aspect ratio" in body["error"].lower()


@pytest.mark.asyncio
async def test_validate_reports_placeholderless_blank_layout(client):
    """A layout with zero placeholders is reported as the blank layout index."""
    r = await client.post(
        "/api/workspace/templates/validate",
        files={
            "file": (
                "deck.pptx",
                deck_with_placeholderless_blank_layout(),
                "application/x-pptx",
            )
        },
    )
    body = r.json()
    assert body["valid"] is True
    assert body["details"]["blank_layout_index"] == 6


@pytest.mark.asyncio
async def test_validate_rejects_deck_without_title_placeholder(client):
    r = await client.post(
        "/api/workspace/templates/validate",
        files={
            "file": (
                "deck.pptx",
                deck_without_title_placeholders(),
                "application/x-pptx",
            )
        },
    )
    body = r.json()
    assert body["valid"] is False
    assert "No layout has a title placeholder" in body["error"]


@pytest.mark.asyncio
async def test_validate_rejects_deck_without_content_layout(client):
    r = await client.post(
        "/api/workspace/templates/validate",
        files={
            "file": (
                "deck.pptx",
                deck_without_content_layout(),
                "application/x-pptx",
            )
        },
    )
    body = r.json()
    assert body["valid"] is False
    assert "content/body (idx=1)" in body["error"]


def _install_fake_pptx(monkeypatch, presentation_cls):
    """Swap sys.modules['pptx'] for a stub exposing a fake Presentation."""
    monkeypatch.setitem(
        sys.modules, "pptx", SimpleNamespace(Presentation=presentation_cls)
    )


@pytest.mark.asyncio
async def test_validate_rejects_deck_with_too_few_layouts(client, monkeypatch):
    class FakePrs:
        def __init__(self, *a):
            self.slide_layouts = [object(), object()]  # 2 < 3

    _install_fake_pptx(monkeypatch, FakePrs)
    r = await client.post(
        "/api/workspace/templates/validate",
        files={"file": ("deck.pptx", b"bytes", "application/x-pptx")},
    )
    body = r.json()
    assert body["valid"] is False
    assert "only 2 slide layouts" in body["error"]


@pytest.mark.asyncio
async def test_validate_reports_add_slide_failure(client, monkeypatch):
    class _PH:
        def __init__(self, idx):
            self.placeholder_format = SimpleNamespace(idx=idx)

    class _Layout:
        def __init__(self, *idxs):
            self.placeholders = [_PH(i) for i in idxs]

    class _Slides:
        def add_slide(self, layout):
            raise RuntimeError("layout corrupt")

    class FakePrs:
        def __init__(self, *a):
            self.slide_layouts = [_Layout(0, 1), _Layout(0), _Layout()]
            self.slide_width = 12192000  # 16:9 in EMU
            self.slide_height = 6858000
            self.slides = _Slides()

    _install_fake_pptx(monkeypatch, FakePrs)
    r = await client.post(
        "/api/workspace/templates/validate",
        files={"file": ("deck.pptx", b"bytes", "application/x-pptx")},
    )
    body = r.json()
    assert body["valid"] is False
    assert "Failed to add a test slide (layout 0)" in body["error"]
    assert "layout corrupt" not in body["error"]


@pytest.mark.asyncio
async def test_validate_without_python_pptx_installed(client, monkeypatch):
    # a None entry in sys.modules makes `from pptx import ...` raise ImportError
    monkeypatch.setitem(sys.modules, "pptx", None)
    r = await client.post(
        "/api/workspace/templates/validate",
        files={"file": ("deck.pptx", b"bytes", "application/x-pptx")},
    )
    assert r.json() == {"valid": False, "error": "python-pptx is not installed"}


# ══════════════════════════════════════════════════════════════════════
# GET generated-files
# ══════════════════════════════════════════════════════════════════════


def deliverable_row(**overrides):
    row = [
        "Report Q4.pdf",           # filename
        "pdf",                     # file_type (d->>'format')
        "report",                  # deliverable_type
        "/api/reports/r1/download",  # download_url
        "r1",                      # report_id
        1_700_000_000_000,         # created_at epoch
        "m1",                      # message_id
        "c1",                      # conversation_id
        "Q4 Chat",                 # conversation_title
        "assistant answer",        # message_content
        datetime(2024, 1, 1),      # message_created_at
    ]
    for i, v in overrides.items():
        row[int(i)] = v
    return tuple(row)


@pytest.mark.asyncio
async def test_generated_files_maps_rows(db, client, dirs):
    reports = dirs.data / "reports"
    reports.mkdir()
    (reports / "r1.pdf").write_bytes(b"x" * 1234)

    db._results = [FakeResult([deliverable_row()])]
    r = await client.get("/api/workspace/generated-files")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1
    item = body["files"][0]
    assert item["filename"] == "Report Q4.pdf"
    assert item["file_type"] == "pdf"
    assert item["download_url"] == "/api/reports/r1/download"
    assert item["report_id"] == "r1"
    assert item["created_at"] == 1_700_000_000_000
    assert item["message_id"] == "m1"
    assert item["conversation_id"] == "c1"
    assert item["conversation_title"] == "Q4 Chat"
    assert item["file_size"] == 1234  # stat'd from the reports dir
    assert item["thumbnail_url"] is None  # only pptx gets one


@pytest.mark.asyncio
async def test_generated_files_pptx_thumbnail_url_and_fallbacks(db, client, dirs):
    row = deliverable_row(**{
        "0": "Deck.pptx",
        "1": "pptx",
        "2": "presentation",
        "4": "r9",          # report_id with no file on disk
        "5": None,          # no epoch → fall back to message_created_at
        "8": None,          # no conversation title → "Untitled"
    })
    db._results = [FakeResult([row])]
    r = await client.get("/api/workspace/generated-files")
    item = r.json()["files"][0]
    assert item["file_size"] is None
    assert item["thumbnail_url"] == "/api/reports/r9/thumbnail"
    assert item["created_at"] == int(datetime(2024, 1, 1).timestamp())
    assert item["conversation_title"] == "Untitled"


@pytest.mark.asyncio
async def test_generated_files_file_type_filter_reaches_sql(db, client, dirs):
    db._results = [FakeResult([])]
    r = await client.get(
        "/api/workspace/generated-files", params={"file_type": "pdf", "limit": 5}
    )
    assert r.status_code == 200
    assert r.json() == {"files": [], "total": 0}
    sql_text = db.executed[0].text
    assert "AND d->>'format' = :ft" in sql_text
    assert "ORDER BY m.created_at DESC LIMIT :lim" in sql_text


@pytest.mark.asyncio
async def test_generated_files_without_filter_has_no_where(db, client, dirs):
    db._results = [FakeResult([])]
    await client.get("/api/workspace/generated-files")
    sql_text = db.executed[0].text
    # no file_type filter → no extra predicate beyond the base WHERE
    assert "AND d->>'format'" not in sql_text
    assert "WHERE jsonb_typeof" in sql_text
    assert "jsonb_array_elements" in sql_text


# ══════════════════════════════════════════════════════════════════════
# POST templates/{id}/thumbnail
# ══════════════════════════════════════════════════════════════════════


def png_bytes(width=400, height=300, color=(120, 40, 200)) -> bytes:
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.mark.asyncio
async def test_upload_thumbnail_resizes_to_200x150_jpeg(db, client):
    t = make_template()
    db._results = [FakeResult(t)]
    r = await client.post(
        f"/api/workspace/templates/{t.id}/thumbnail",
        files={"file": ("thumb.png", png_bytes(), "image/png")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["thumbnail"].endswith("...")
    # decoded payload is a 200x150 JPEG
    raw = base64.b64decode(t.thumbnail)
    assert raw[:2] == b"\xff\xd8"  # JPEG magic
    img = Image.open(io.BytesIO(raw))
    assert img.size == (200, 150)


@pytest.mark.asyncio
async def test_upload_thumbnail_invalid_uuid_400(db, client):
    r = await client.post(
        "/api/workspace/templates/bogus/thumbnail",
        files={"file": ("t.png", png_bytes(), "image/png")},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_upload_thumbnail_unknown_template_404(db, client):
    db._results = [FakeResult(None)]
    r = await client.post(
        f"/api/workspace/templates/{uuid.uuid4()}/thumbnail",
        files={"file": ("t.png", png_bytes(), "image/png")},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_upload_thumbnail_without_pillow_500(db, client, monkeypatch):
    t = make_template()
    db._results = [FakeResult(t)]
    # the payload must be encoded BEFORE PIL is hidden (Pillow's own save
    # machinery re-imports from the PIL package lazily)
    payload = png_bytes()
    monkeypatch.setitem(sys.modules, "PIL", None)  # forces the ImportError branch
    r = await client.post(
        f"/api/workspace/templates/{t.id}/thumbnail",
        files={"file": ("t.png", payload, "image/png")},
    )
    assert r.status_code == 500
    assert r.json()["detail"] == "Pillow not installed for image processing"
    assert t.thumbnail is None  # row untouched


@pytest.mark.asyncio
async def test_upload_thumbnail_corrupt_image_500(db):
    """A corrupt image is NOT caught by the endpoint — it escapes as a 500
    (starlette's error middleware turns it into Internal Server Error).
    Verified with an ASGI transport that reports instead of raising."""
    t = make_template()
    db._results = [FakeResult(t)]
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        r = await c.post(
            f"/api/workspace/templates/{t.id}/thumbnail",
            files={"file": ("t.png", b"not an image", "image/png")},
        )
    assert r.status_code == 500
