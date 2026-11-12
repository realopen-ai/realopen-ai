"""Read-only local models, streamed upload staging, and shared import jobs."""

import asyncio
import hashlib
import json
import struct
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.config import settings
from app.api import model_manager as api
from app.services.model_manager.host_api import model_host_router
from app.services.model_manager.jobs import ModelJobs
from app.services.model_manager.local_catalog import (
    CHUNK_BYTES,
    LocalCatalog,
    LocalUploadRequest,
)
from app.services.model_manager.ollama_runtime import OllamaRuntime
from app.services.model_manager.types import InstallRequest, ModelManagerError


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "REALOPEN_DATA_DIR", str(tmp_path / "data"))
    return LocalCatalog()


def gguf():
    return b"GGUF" + struct.pack("<I", 3) + b"small fixture"


def safetensors():
    header = json.dumps(
        {"weight": {"dtype": "F32", "shape": [1], "data_offsets": [0, 4]}}
    ).encode()
    return struct.pack("<Q", len(header)) + header + struct.pack("<f", 1.0)


async def chunks(*data):
    for item in data:
        yield item


@pytest.mark.asyncio
async def test_host_gguf_is_referenced_without_copying_or_modifying(local, tmp_path):
    source = tmp_path / "my downloaded model.gguf"
    source.write_bytes(gguf())
    before = (source.read_bytes(), source.stat().st_mtime_ns)
    ident = await local.prepare_path(str(source))
    detail = await local.inspect(ident)
    assert len(detail.variants) == 1
    variant = detail.variants[0]
    events = [event async for event in local.download(detail, variant)]
    assert events[-1]["files"] == {source.name: str(source)}
    assert before == (source.read_bytes(), source.stat().st_mtime_ns)
    assert not (local.directory(ident) / "files").exists()
    configuration = OllamaRuntime().import_configuration(variant, events[-1]["files"])
    assert json.loads(configuration["content"].removeprefix("FROM ").strip()) == str(
        source
    )
    assert configuration["filename"] == "Modelfile"


@pytest.mark.asyncio
async def test_host_source_changes_are_detected_before_queued_import(local, tmp_path):
    source = tmp_path / "model.gguf"
    source.write_bytes(gguf())
    ident = await local.prepare_path(str(source))
    detail = await local.inspect(ident)
    source.write_bytes(gguf() + b"changed")
    with pytest.raises(ModelManagerError, match="changed"):
        _ = [event async for event in local.download(detail, detail.variants[0])]


@pytest.mark.asyncio
async def test_gguf_shards_and_generated_wildcard_are_complete(local, tmp_path):
    for part in (1, 2):
        (tmp_path / f"model-{part:05}-of-00002.gguf").write_bytes(gguf())
    ident = await local.prepare_path(str(tmp_path / "model-00001-of-00002.gguf"))
    detail = await local.inspect(ident)
    variant = detail.variants[0]
    assert len(variant.files) == 2
    configuration = OllamaRuntime().import_configuration(
        variant, local.file_paths(detail, variant)
    )
    assert "model-*-of-00002.gguf" in configuration["content"]
    (tmp_path / "model-00002-of-00002.gguf").unlink()
    with pytest.raises(ModelManagerError, match="complete"):
        await local.prepare_path(str(tmp_path / "model-00001-of-00002.gguf"))


@pytest.mark.asyncio
async def test_safetensors_bundle_auto_detects_mlx_and_excludes_supplied_scripts(
    local, tmp_path
):
    root = tmp_path / "manual mlx"
    root.mkdir()
    (root / "model.safetensors").write_bytes(safetensors())
    (root / "config.json").write_text(
        json.dumps(
            {"model_type": "qwen3", "quantization": {"bits": 4, "group_size": 64}}
        )
    )
    (root / "tokenizer_config.json").write_text("{}")
    (root / "Modelfile").write_text("FROM malicious\nSYSTEM Never use this\n")
    (root / "modeling.py").write_text("raise RuntimeError('Do not execute')")
    ident = await local.prepare_path(str(root))
    detail = await local.inspect(ident)
    variant = detail.variants[0]
    assert variant.format == "mlx"
    assert variant.quantization == "4-bit"
    assert {f.path for f in variant.files} == {
        "model.safetensors",
        "config.json",
        "tokenizer_config.json",
    }
    configuration = OllamaRuntime().import_configuration(
        variant, local.file_paths(detail, variant)
    )
    assert "malicious" not in configuration["content"]
    assert (root / "Modelfile").read_text().startswith("FROM malicious")


@pytest.mark.asyncio
async def test_unsafe_or_missing_safetensors_index_shards_are_rejected(local, tmp_path):
    (tmp_path / "model.safetensors").write_bytes(safetensors())
    (tmp_path / "config.json").write_text("{}")
    index = tmp_path / "model.safetensors.index.json"
    for reference in (
        "../outside.safetensors",
        "/private/secret.safetensors",
        "missing.safetensors",
    ):
        index.write_text(json.dumps({"weight_map": {"x": reference}}))
        with pytest.raises(ModelManagerError):
            await local.prepare_path(str(tmp_path))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relative",
    ["../escape.gguf", "/absolute.gguf", "C:\\model.gguf", "model.py", "Modelfile"],
)
async def test_upload_manifest_never_accepts_unrelated_or_unsafe_paths(local, relative):
    with pytest.raises(ModelManagerError):
        await local.begin_upload(
            LocalUploadRequest(files=[{"path": relative, "size": 4}])
        )


@pytest.mark.asyncio
async def test_upload_chunks_finalize_then_use_same_local_catalog(local, tmp_path):
    data = gguf()
    session = await local.begin_upload(
        LocalUploadRequest(
            title="Browser model", files=[{"path": "model.gguf", "size": len(data)}]
        )
    )
    ident = session["id"]
    assert session["chunk_size"] == CHUNK_BYTES
    with pytest.raises(ModelManagerError, match="Finish uploading"):
        await local.inspect(ident)
    assert (await local.upload_chunk(ident, "model.gguf", 0, chunks(data[:8])))[
        "offset"
    ] == 8
    await local.upload_chunk(ident, "model.gguf", 8, chunks(data[8:]))
    await local.finish_upload(ident)
    detail = await local.inspect(ident)
    path = Path(local.file_paths(detail, detail.variants[0])["model.gguf"])
    assert path.is_relative_to(settings.get_data_dir())
    assert path.read_bytes() == data
    with pytest.raises(ModelManagerError, match="closed"):
        await local.upload_chunk(ident, "model.gguf", len(data), chunks(b"x"))
    await local.discard(ident)
    assert not local.directory(ident).exists()


@pytest.mark.asyncio
async def test_upload_overflow_and_disconnect_roll_back_only_current_chunk(local):
    data = gguf()
    session = await local.begin_upload(
        LocalUploadRequest(files=[{"path": "model.gguf", "size": len(data)}])
    )
    ident = session["id"]
    await local.upload_chunk(ident, "model.gguf", 0, chunks(data[:4]))
    with pytest.raises(ModelManagerError, match="declared"):
        await local.upload_chunk(ident, "model.gguf", 4, chunks(data * 10))
    path = local.directory(ident) / "files/model.gguf"
    assert path.read_bytes() == data[:4]

    async def interrupted():
        yield data[4:8]
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await local.upload_chunk(ident, "model.gguf", 4, interrupted())
    assert path.read_bytes() == data[:4]
    with pytest.raises(ModelManagerError, match="position"):
        await local.upload_chunk(ident, "model.gguf", 0, chunks(data))
    with pytest.raises(ModelManagerError, match="incomplete"):
        await local.finish_upload(ident)


@pytest.mark.asyncio
async def test_upload_quota_and_case_collisions_are_rejected(local, monkeypatch):
    monkeypatch.setattr(settings, "MODEL_IMPORT_MAX_BYTES", 1)
    with pytest.raises(ModelManagerError, match="limit"):
        await local.begin_upload(
            LocalUploadRequest(files=[{"path": "model.gguf", "size": 2}])
        )
    with pytest.raises(ModelManagerError, match="unique"):
        await local.begin_upload(
            LocalUploadRequest(
                files=[
                    {"path": "model.gguf", "size": 1},
                    {"path": "MODEL.gguf", "size": 1},
                ]
            )
        )


@pytest.mark.asyncio
async def test_host_reference_cannot_be_discarded_or_follow_symlinks(local, tmp_path):
    source = tmp_path / "model.gguf"
    source.write_bytes(gguf())
    ident = await local.prepare_path(str(source))
    with pytest.raises(ModelManagerError, match="cannot be deleted"):
        await local.discard(ident)
    assert source.read_bytes() == gguf()
    link = tmp_path / "linked.gguf"
    try:
        link.symlink_to(source)
    except OSError:
        pytest.skip("Symlinks unavailable")
    with pytest.raises(ModelManagerError, match="symbolic"):
        await local.prepare_path(str(link))


@pytest.mark.asyncio
async def test_invalid_binary_headers_are_rejected(local, tmp_path):
    for name in ("model.gguf", "model.safetensors"):
        path = tmp_path / name
        path.write_bytes(b"not model weights")
        with pytest.raises(ModelManagerError, match="header"):
            await local.prepare_path(str(path if name.endswith("gguf") else tmp_path))
        path.unlink()


@pytest.mark.asyncio
async def test_real_ollama_adapter_imports_local_source_and_retains_configuration(
    local, tmp_path, monkeypatch
):
    source = tmp_path / "downloaded.gguf"
    source.write_bytes(gguf())
    ident = await local.prepare_path(str(source))
    runtime = OllamaRuntime("http://localhost:11434")
    runtime.status = AsyncMock(return_value={"id": "ollama", "available": True})
    runtime.installed = AsyncMock(return_value=[])
    original = httpx.AsyncClient
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "HEAD":
            return httpx.Response(404)
        if request.url.path.startswith("/api/blobs/"):
            assert request.content == gguf()
            return httpx.Response(201)
        if request.url.path == "/api/create":
            body = json.loads(request.content)
            assert body["files"] == {
                "downloaded.gguf": "sha256:" + hashlib.sha256(gguf()).hexdigest()
            }
            return httpx.Response(200, content=b'{"status":"success"}\n')
        return httpx.Response(200, json={"capabilities": ["completion"]})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original(
            *args, **{**kwargs, "transport": httpx.MockTransport(handler)}
        ),
    )
    manager = ModelJobs(tmp_path / "jobs", {runtime.id: runtime}, {local.id: local})
    job = await manager.create(
        InstallRequest(
            catalog="local",
            model=ident,
            variant="downloaded.gguf",
            runtime="ollama",
            name="local-test",
        )
    )
    await asyncio.gather(*list(manager.tasks.values()))
    row = manager.records[job["id"]]
    assert row["status"] == "completed"
    assert row["configuration"]["content"].startswith("FROM ")
    assert source.read_bytes() == gguf()
    assert (await manager.inventory())[
        "catalogs"
    ] == []  # Local source is not a web-search catalog.
    assert (await manager.inventory())["local_import"]["extensions"] == [
        ".gguf",
        ".safetensors",
    ]


def test_host_upload_api_accepts_raw_chunks_without_multipart_buffering(
    local, tmp_path
):
    manager = ModelJobs(tmp_path / "jobs", {}, {local.id: local})
    app = FastAPI()
    app.include_router(model_host_router(manager))
    with TestClient(app) as client:
        data = gguf()
        session = client.post(
            "/v1/models/local/uploads",
            json={"files": [{"path": "model.gguf", "size": len(data)}]},
        ).json()
        ident = session["id"]
        response = client.put(
            f"/v1/models/local/uploads/{ident}/file",
            params={"path": "model.gguf", "offset": 0},
            content=data,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert response.json()["offset"] == len(data)
        result = client.post(f"/v1/models/local/uploads/{ident}/finish")
        assert result.status_code == 200
        assert result.json()["catalog"] == "local"
        assert client.delete(f"/v1/models/local/uploads/{ident}").status_code == 200


@pytest.mark.asyncio
async def test_backend_streams_local_chunk_to_host(monkeypatch):
    app = FastAPI()
    app.include_router(api.router, prefix="/api")
    original = httpx.AsyncClient
    payload = gguf()

    async def handler(request):
        assert await request.aread() == payload
        assert request.headers["content-type"] == "application/octet-stream"
        return httpx.Response(200, json={"offset": len(payload)})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *args, **kwargs: original(
            *args, **{**kwargs, "transport": httpx.MockTransport(handler)}
        ),
    )
    async with original(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.put(
            "/api/model-manager/local/uploads/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/file",
            params={"path": "model.gguf", "offset": 0},
            content=payload,
            headers={"Content-Type": "application/octet-stream"},
        )
    assert response.status_code == 200
    assert response.json()["offset"] == len(payload)


@pytest.mark.asyncio
async def test_empty_companion_files_can_be_staged_without_empty_weight_files(local):
    data = gguf()
    session = await local.begin_upload(
        LocalUploadRequest(
            files=[
                {"path": "model.gguf", "size": len(data)},
                {"path": "chat_template.jinja", "size": 0},
            ]
        )
    )
    await local.upload_chunk(session["id"], "model.gguf", 0, chunks(data))
    await local.finish_upload(session["id"])
    assert (
        local.directory(session["id"]) / "files/chat_template.jinja"
    ).read_bytes() == b""
    with pytest.raises(ModelManagerError, match="nonempty"):
        await local.begin_upload(
            LocalUploadRequest(files=[{"path": "model.gguf", "size": 0}])
        )
