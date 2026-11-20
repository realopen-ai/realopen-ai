"""Regression checks for hardware selection, image inputs and search fusion."""

from io import BytesIO
from uuid import UUID

from PIL import Image
import pytest

from app.services.lens.config import get_lens_config
from app.services.lens.encoders import image_mime, image_rgb, model_version
from app.services.lens.search import rank
from app.services.model_manager.lens_models import model_spec


def test_lens_model_precision_and_versions_follow_hardware_profiles():
    medium = get_lens_config("apple_medium")
    large = get_lens_config("apple_large")
    assert medium.siglip2 == "int8"
    assert large.siglip2 == "fp32"
    assert medium.e5 == large.e5 == "int8"
    assert medium.threads == large.threads == 2
    assert model_version("siglip2", medium.siglip2) != model_version("siglip2", large.siglip2)
    for variant in model_spec("siglip2").variants:
        assert len(variant.files) == 3
        assert all(file.sha256 and len(file.sha256) == 64 for file in variant.files)


def test_lens_uses_image_content_and_flattens_transparency():
    with Image.new("RGBA", (10, 10), (0, 0, 0, 0)) as transparent, BytesIO() as buffer:
        transparent.save(buffer, "PNG")
        data = buffer.getvalue()
    assert image_mime(data) == "image/png"
    with image_rgb(data) as result:
        assert result.getpixel((0, 0)) == (255, 255, 255)
    assert image_mime(b'<svg xmlns="http://www.w3.org/2000/svg"/>') is None


def test_lens_exact_and_semantic_fallbacks_survive_missing_visual_index():
    exact, semantic, weak, visual = (UUID(int=i) for i in range(4))
    assert rank([exact], {}, {}, 0) == [exact]
    assert rank([exact], {}, {semantic: 0.9, weak: 0.81}, 0.7) == [exact, semantic]
    assert rank([exact], {visual: 0.6, semantic: 0.55}, {semantic: 0.94}, 0.7) == [
        exact,
        semantic,
        visual,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("result", "status"),
    [
        ("Visible text", 200),
        ("", 200),
        (RuntimeError("missing"), 503),
        (ValueError("invalid"), 422),
    ],
)
async def test_viewer_ocr_processes_uploads_without_a_database(monkeypatch, result, status):
    import httpx
    from fastapi import FastAPI
    from unittest.mock import AsyncMock
    from app.api import lens

    with Image.new("RGB", (12, 8), "white") as image, BytesIO() as buffer:
        image.save(buffer, "PNG")
        data = buffer.getvalue()
    recognize = AsyncMock(
        side_effect=result if isinstance(result, Exception) else None,
        return_value=lens.ocr.OCRResult(result, "tesseract", "eng"),
    )
    monkeypatch.setattr(lens.ocr, "default_language", lambda: "eng")
    monkeypatch.setattr(lens.ocr, "recognize", recognize)
    app = FastAPI()
    app.include_router(lens.router, prefix="/api")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/lens/ocr", files={"file": ("image.png", data, "image/png")}
        )
    assert response.status_code == status
    recognize.assert_awaited_once_with(data, "eng")
    if status == 200:
        assert response.json() == {"text": result}


@pytest.mark.asyncio
async def test_lens_download_preserves_the_original_image_and_its_format(monkeypatch):
    import httpx
    from fastapi import FastAPI
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.api import lens

    with Image.new("RGB", (12, 8), "red") as image, BytesIO() as buffer:
        image.save(buffer, "PNG")
        original = buffer.getvalue()
    db = SimpleNamespace(get=AsyncMock(return_value=object()))
    monkeypatch.setattr(lens, "image_detail", AsyncMock(return_value={"filename": "source.pdf"}))
    monkeypatch.setattr(lens, "source_bytes", AsyncMock(return_value=original))
    app = FastAPI()
    app.include_router(lens.router, prefix="/api")
    app.dependency_overrides[lens.get_db] = lambda: db
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/lens/images/{UUID(int=1)}/download")
    assert response.content == original
    assert response.headers["content-type"] == "image/png"
    assert response.headers["content-disposition"].endswith("source.png")
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_agent_image_scope_and_reference_are_execution_owned(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from app.agent.tools import search_images

    @asynccontextmanager
    async def session():
        yield object()

    ident = UUID(int=1)
    find = AsyncMock(
        return_value=[
            {
                "id": ident,
                "filename": "screen.png",
                "ocr_text": "ERR_CONN_1042",
            }
        ]
    )
    monkeypatch.setattr(search_images, "async_session_factory", session)
    monkeypatch.setattr(search_images, "find_images", find)
    tool = search_images.SearchImagesTool()
    result = await tool.execute(query="screen", scope="library")
    assert result.success
    assert find.await_args.kwargs["conversation_id"] is None
    assert result.tool_call.image_scope == "library"
    result = await tool.execute(query="screen", scope="chat", _conversation_id=str(ident))
    assert result.success
    assert find.await_args.kwargs["conversation_id"] == ident
    assert result.tool_call.image_results == [
        {"id": str(ident), "filename": "screen.png", "excerpt": "ERR_CONN_1042"}
    ]
    # No conversation ID is supplied in a temporary chat. Read-only global
    # search uses the staged reference attachment directly.
    result = await tool.execute(reference_image=1, _images=["YWJj"])
    assert result.success
    assert find.await_args.kwargs["conversation_id"] is None
    assert find.await_args.kwargs["reference"] == b"abc"

    find.reset_mock()
    find.side_effect = [[], find.return_value]
    result = await tool.execute(query="captura de pantalla", _library_query="screen")
    assert result.success
    assert [call.kwargs["query"] for call in find.await_args_list] == [
        "captura de pantalla",
        "screen",
    ]
    assert all(call.kwargs["conversation_id"] is None for call in find.await_args_list)


@pytest.mark.asyncio
@pytest.mark.parametrize("reply", ["library", "{}"])
async def test_image_scope_classifier_ignores_invalid_responses(monkeypatch, reply):
    from unittest.mock import AsyncMock

    from app.agent.tools import search_images

    monkeypatch.setattr(
        search_images.providers,
        "chat_once",
        AsyncMock(return_value={"message": {"content": reply}}),
    )
    assert (
        await search_images.classify_library_search(
            'Summarize "search my image library"', "local-model", []
        )
        is None
    )


@pytest.mark.asyncio
async def test_lens_source_switches_default_on_and_filter_agent_search(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock

    from app.agent.tools import config_store, search_images
    from app.services.lens.search import DOCUMENT_IMAGES

    settings = config_store.merged_config("search_images", None)
    switches = settings["custom"]["sources"]
    assert len(switches) == 4 and all(switches.values())
    assert config_store.merged_config(
        "search_images", {"custom": {"sources": {DOCUMENT_IMAGES: False}}}
    )["custom"]["sources"] == {**switches, DOCUMENT_IMAGES: False}

    @asynccontextmanager
    async def session():
        yield object()

    fake = AsyncMock(return_value=[])
    monkeypatch.setattr(search_images, "async_session_factory", session)
    monkeypatch.setattr(search_images, "find_images", fake)
    monkeypatch.setattr(config_store, "get_tool_config", lambda _: settings)
    tool = search_images.SearchImagesTool()
    for source in switches:
        switches[source] = source == DOCUMENT_IMAGES
    await tool.execute(query="diagram", _conversation_id=UUID(int=1))
    assert fake.await_args.kwargs["source_types"] == (DOCUMENT_IMAGES,)

    switches[DOCUMENT_IMAGES] = False
    fake.reset_mock()
    result = await tool.execute(query="diagram", _conversation_id=UUID(int=1))
    assert "turned off" in result.output
    fake.assert_not_awaited()


def test_lens_rejects_cloud_placeholders_before_opening_models():
    from types import SimpleNamespace
    from app.services.lens.encoders import require_local_models, LensEncoderUnavailable, SF_DATALESS

    file = SimpleNamespace(stat=lambda: SimpleNamespace(st_flags=SF_DATALESS))
    with pytest.raises(LensEncoderUnavailable, match="offloaded"):
        require_local_models({"model.onnx": file})


@pytest.mark.asyncio
async def test_lens_encoder_deadline_releases_slot_and_stops_worker(monkeypatch):
    import time
    import multiprocessing
    from app.services.lens import encoders

    before = {child.pid for child in multiprocessing.active_children()}
    monkeypatch.setattr(encoders, "ENCODER_TIMEOUT", 0.1)
    with pytest.raises(encoders.LensEncoderUnavailable, match="timed out"):
        await encoders._encode(time.sleep, 30)
    assert not encoders.encoder_slot.locked()
    assert {child.pid for child in multiprocessing.active_children()} == before
    monkeypatch.setattr(encoders, "ENCODER_TIMEOUT", 60)
    result = await encoders._encode(encoders._normalize, [[3, 4]])
    assert result[0] == pytest.approx([0.6, 0.8])


@pytest.mark.asyncio
async def test_lens_ocr_match_returns_without_waiting_for_encoders(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from app.services.lens import search

    ident = UUID(int=1)
    row = {"id": ident, "filename": "screen.png"}
    monkeypatch.setattr(search, "exact_candidates", AsyncMock(return_value=[ident]))
    query_encoder = AsyncMock(side_effect=AssertionError("Encoder must not run"))
    monkeypatch.setattr(search, "_query_vector", query_encoder)
    db = SimpleNamespace(
        execute=AsyncMock(
            return_value=SimpleNamespace(
                mappings=lambda: [row],
            )
        )
    )
    result = await search.find_images(db, get_lens_config(), query="ERR_CONN_1042")
    assert result == [row]
    query_encoder.assert_not_awaited()
