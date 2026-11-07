"""Catalog/runtime separation, safe downloads, and durable cancellable model jobs."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.config import settings
from app.api import model_manager as api
from app.api import models as models_api
from app.services.model_manager import ollama_local
from app.services.model_manager import downloads
from app.services.model_manager.host_api import model_host_router
from app.services.model_manager.huggingface_catalog import HuggingFaceCatalog
from app.services.model_manager.jobs import ModelJobs
from app.services.model_manager.ollama_catalog import OllamaCatalog
from app.services.model_manager.ollama_runtime import OllamaRuntime
from app.services.model_manager.types import (
    CatalogModel,
    InstallRequest,
    ModelFile,
    ModelManagerError,
    Variant,
)
from app.services.model_manager.validation import (
    model_name,
    repository,
    resolve_reference,
)


@pytest.mark.parametrize(
    "validator,value",
    [
        (repository, "ornith-ai/Ornith-1.5-9B-MLX-4bit"),
        (repository, "0" * 119 + "/" + "0" * 120),
        (model_name, "org/model.name-1:q4_K_M"),
        (model_name, "model"),
        (model_name, "0" * 200),
    ],
)
def test_model_identifiers_accept_valid_components(validator, value):
    assert validator(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "/model",
        "owner/",
        "owner/model/extra",
        "../model",
        "owner/.model",
        "owner/model:tag",
        "owner/modél",
        "owner/model\n",
        "0" * 240,
        "0" * 119 + "/" + "0" * 119 + "!",
        "owner/" + "0" * 240,
    ],
)
def test_repository_rejects_malformed_components(value):
    with pytest.raises(ModelManagerError):
        repository(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "model:",
        "model:tag:extra",
        "owner/model/extra",
        "model/.hidden",
        "model:é",
        "model\n",
        "0" * 199 + "!",
        "0" * 201,
    ],
)
def test_model_names_reject_malformed_components(value):
    with pytest.raises(ModelManagerError):
        model_name(value)


@pytest.fixture
def transport(monkeypatch):
    original = httpx.AsyncClient

    def install(handler):
        monkeypatch.setattr(
            httpx,
            "AsyncClient",
            lambda *args, **kwargs: original(
                *args, **{**kwargs, "transport": httpx.MockTransport(handler)}
            ),
        )

    return install


@pytest.mark.parametrize(
    "value,catalog,expected",
    [
        ("qwen3:8b", "ollama", ("ollama", "qwen3:8b", None, None)),
        (
            "https://ollama.com/library/qwen3:8b",
            "huggingface",
            ("ollama", "qwen3:8b", None, None),
        ),
        (
            "https://huggingface.co/owner/model/tree/main",
            "ollama",
            ("huggingface", "owner/model", None, None),
        ),
        (
            "https://huggingface.co/owner/model/blob/main/q4.gguf",
            "ollama",
            ("huggingface", "owner/model", "q4.gguf", None),
        ),
    ],
)
def test_direct_references(value, catalog, expected):
    assert resolve_reference(value, catalog) == expected


@pytest.mark.parametrize(
    "value",
    [
        "http://huggingface.co/owner/model",
        "https://huggingface.co.evil.test/owner/model",
        "https://user:secret@huggingface.co/owner/model",
        "https://127.0.0.1/private",
        "https://huggingface.co/owner/model?token=secret",
        "https://ollama.com/library/../secret",
        "https://huggingface.co/owner/model/blob/main/../../secret",
        "https://huggingface.co:444/owner/model",
    ],
)
def test_untrusted_references_are_rejected(value):
    with pytest.raises(ModelManagerError):
        resolve_reference(value, "ollama")


@pytest.mark.asyncio
async def test_ollama_search_and_exact_tag_inspection(transport):
    def handler(request):
        if request.url.path == "/search":
            return httpx.Response(
                200,
                text='<a href="/library/qwen3"><h2>Qwen3</h2><p>A local model</p></a><a href="https://evil.test">Ignore</a>',
            )
        return httpx.Response(
            200,
            text='<a href="/library/qwen3:8b">8b 5.2GB Q4_K_M</a><a href="/library/qwen3:8b">8b</a><a href="/library/qwen3:32b">32b 20GB Q4_K_M</a>',
        )

    transport(handler)
    catalog = OllamaCatalog()
    rows = await catalog.search("qwen")
    assert [r.id for r in rows] == ["qwen3"]
    detail = await catalog.inspect("qwen3:8b")
    assert len(detail.variants) == 2
    assert detail.variants[0].size == 5_200_000_000
    with pytest.raises(ModelManagerError):
        await catalog.inspect("qwen3:invented")


def hf_data():
    return {
        "sha": "a" * 40,
        "library_name": "transformers",
        "config": {"model_type": "qwen3"},
        "cardData": {"license": "apache-2.0"},
        "siblings": [
            {"rfilename": "model-Q4_K_M-00001-of-00002.gguf", "size": 4},
            {"rfilename": "model-Q4_K_M-00002-of-00002.gguf", "size": 5},
            {"rfilename": "model.safetensors", "size": 10},
            {"rfilename": "config.json", "size": 2},
            {"rfilename": "tokenizer_config.json", "size": 2},
            {"rfilename": "README.md", "size": 20000},
            {"rfilename": "modeling_unsafe.py", "size": 40000},
        ],
    }


@pytest.mark.asyncio
async def test_hf_metadata_groups_shards_excludes_code_and_pins_revision(transport):
    transport(lambda _: httpx.Response(200, json=hf_data()))
    detail = await HuggingFaceCatalog().inspect("owner/model")
    assert detail.revision == "a" * 40
    assert detail.license == "apache-2.0"
    assert len(detail.variants) == 2
    assert detail.variants[0].size == 9
    assert len(detail.variants[0].files) == 2
    assert len(detail.variants[1].files) == 3
    assert all(not f.path.endswith(".py") for v in detail.variants for f in v.files)


@pytest.mark.asyncio
async def test_hf_mlx_is_explicit_and_gated_models_cannot_install(transport):
    data = hf_data()
    data.update(library_name="mlx", gated="manual")
    transport(lambda _: httpx.Response(200, json=data))
    detail = await HuggingFaceCatalog().inspect("owner/model")
    runtime = OllamaRuntime("http://remote.test:11434")
    assert detail.variants[-1].format == "mlx"
    assert not runtime.compatibility(detail, detail.variants[-1])["supported"]
    detail.gated = False
    assert not runtime.compatibility(detail, detail.variants[-1])["supported"]
    runtime.local_mlx = True
    assert runtime.compatibility(detail, detail.variants[-1])["supported"]


class FakeCatalog:
    id = "fake_catalog"
    label = "Test catalog"

    def __init__(self):
        self.downloaded = []

    async def inspect(self, model, revision=None):
        return CatalogModel(
            catalog=self.id,
            id=model,
            title=model,
            url="https://huggingface.co/owner/model",
            revision=revision or "a" * 40,
            variants=[Variant(id="q4", label="Q4", format="test", size=4)],
        )

    async def download(self, model, variant):
        self.downloaded.append((model.id, model.revision))
        yield {
            "stage": "downloading",
            "completed": 4,
            "total": 4,
            "message": "Downloaded weights",
        }
        yield {"files": {}}


class FakeRuntime:
    id = "other_runtime"
    label = "Second runtime"

    def __init__(self):
        self.models = []
        self.wait = None
        self.fail = False
        self.active = 0
        self.max_active = 0

    def normalize_name(self, name):
        return name

    async def status(self):
        return {"id": self.id, "label": self.label, "available": True}

    async def installed(self):
        return self.models.copy()

    def compatibility(self, model, variant):
        return {"supported": variant.format == "test", "reason": "Test format"}

    async def install(self, model, variant, name, files):
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            if self.wait:
                await self.wait.wait()
            if self.fail:
                raise RuntimeError("Private path and secret: must stay in logs")
            self.models.append({"name": name, "runtime": self.id})
            yield {"stage": "verifying", "capabilities": ["completion"]}
        finally:
            self.active -= 1

    async def remove(self, name):
        self.models = [m for m in self.models if m["name"] != name]


@pytest.fixture
def manager(tmp_path):
    source, runtime = FakeCatalog(), FakeRuntime()
    return ModelJobs(tmp_path / "jobs", {runtime.id: runtime}, {source.id: source})


def request(name="new-model"):
    return InstallRequest(
        catalog="fake_catalog",
        model="owner/model",
        variant="q4",
        runtime="other_runtime",
        name=name,
    )


async def finish(manager):
    await asyncio.gather(*list(manager.tasks.values()))


@pytest.mark.asyncio
async def test_second_runtime_install_persists_then_survives_restart(manager):
    await manager.start()
    row = await manager.create(request())
    await finish(manager)
    assert manager.records[row["id"]]["status"] == "completed"
    stored = json.loads((manager.directory / (row["id"] + ".json")).read_text())
    assert stored["request"]["revision"] == "a" * 40
    restored = ModelJobs(manager.directory, manager.runtimes, manager.catalogs)
    await restored.start()
    assert restored.snapshot()[0]["status"] == "completed"
    assert (await manager.inventory())["runtimes"][0]["id"] == "other_runtime"


@pytest.mark.asyncio
async def test_serial_downloads_duplicate_guard_and_cancellation(manager):
    runtime = manager.runtime("other_runtime")
    runtime.wait = asyncio.Event()
    one = await manager.create(request())
    with pytest.raises(ModelManagerError, match="active"):
        await manager.create(request())
    two = await manager.create(
        request("different-name").model_copy(update={"model": "owner/different"})
    )
    await asyncio.sleep(0)
    assert runtime.max_active <= 1
    await manager.cancel(two["id"])
    await manager.cancel(one["id"])
    assert all(r["status"] == "cancelled" for r in manager.records.values())
    assert not manager.tasks


@pytest.mark.asyncio
async def test_errors_are_safe_retry_pins_source_and_does_not_overwrite(manager):
    runtime = manager.runtime("other_runtime")
    runtime.fail = True
    row = await manager.create(request())
    await finish(manager)
    assert manager.records[row["id"]]["status"] == "failed"
    assert "secret" not in manager.records[row["id"]]["message"]
    runtime.fail = False
    await manager.retry(row["id"])
    await finish(manager)
    assert manager.snapshot()[0]["status"] == "completed"
    with pytest.raises(ModelManagerError, match="already installed"):
        await manager.create(request())
    await manager.remove("other_runtime", "new-model")
    assert runtime.models == []


@pytest.mark.asyncio
async def test_shutdown_and_restart_preserve_interrupted_jobs(manager):
    manager.runtime("other_runtime").wait = asyncio.Event()
    row = await manager.create(request())
    await asyncio.sleep(0)
    await manager.close()
    restored = ModelJobs(manager.directory, manager.runtimes, manager.catalogs)
    await restored.start()
    assert restored.records[row["id"]]["status"] == "interrupted"


@pytest.mark.asyncio
async def test_preflight_rejects_incompatible_variant_before_download(manager):
    manager.runtime("other_runtime").compatibility = lambda *_: {
        "supported": False,
        "reason": "Not supported",
    }
    with pytest.raises(ModelManagerError, match="Not supported"):
        await manager.create(request())
    assert not manager.catalog("fake_catalog").downloaded


@pytest.mark.asyncio
async def test_hf_download_resumes_verifies_hash_and_reuses_cache(
    tmp_path, monkeypatch, transport
):
    monkeypatch.setattr(settings, "REALOPEN_DATA_DIR", str(tmp_path))
    content = b"GGUFtest"
    variant = Variant(
        id="model.gguf",
        label="Q4",
        format="gguf",
        size=len(content),
        files=[
            ModelFile(
                path="model.gguf",
                size=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
            )
        ],
    )
    model = CatalogModel(
        catalog="huggingface",
        id="owner/model",
        title="Model",
        url="https://huggingface.co/owner/model",
        revision="a" * 40,
    )
    root = downloads.cache_root(model)
    (root / "model.gguf.part").write_bytes(content[:4])
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["range"] == "bytes=4-"
        assert "a" * 40 in str(request.url)
        return httpx.Response(
            206,
            content=content[4:],
            headers={"Content-Range": f"bytes 4-{len(content)-1}/{len(content)}"},
        )

    transport(handler)
    events = [event async for event in downloads.download_files(model, variant)]
    assert Path(events[-1]["files"]["model.gguf"]).read_bytes() == content
    assert not (root / "model.gguf.part").exists()
    assert len(seen) == 1
    _ = [event async for event in downloads.download_files(model, variant)]
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_hf_download_checks_storage_and_rejects_redirects(
    tmp_path, monkeypatch, transport
):
    monkeypatch.setattr(settings, "REALOPEN_DATA_DIR", str(tmp_path))
    model = CatalogModel(
        catalog="huggingface",
        id="owner/model",
        title="Model",
        url="https://huggingface.co/owner/model",
        revision="a" * 40,
    )
    variant = Variant(
        id="model.gguf",
        label="Q4",
        format="gguf",
        size=4,
        files=[ModelFile(path="model.gguf", size=4)],
    )
    monkeypatch.setattr(
        downloads.shutil, "disk_usage", lambda _: SimpleNamespace(free=0)
    )
    with pytest.raises(ModelManagerError, match="free space"):
        _ = [e async for e in downloads.download_files(model, variant)]
    monkeypatch.setattr(
        downloads.shutil, "disk_usage", lambda _: SimpleNamespace(free=10**12)
    )
    transport(
        lambda _: httpx.Response(302, headers={"Location": "http://127.0.0.1/private"})
    )
    with pytest.raises(ModelManagerError, match="unsupported host"):
        _ = [e async for e in downloads.download_files(model, variant)]


@pytest.mark.asyncio
async def test_ollama_pull_stream_verifies_registration_without_loading(transport):
    calls = []

    def handler(request):
        calls.append(request.url.path)
        if request.url.path == "/api/pull":
            return httpx.Response(
                200,
                content=b'{"status":"pulling","total":100,"completed":50}\n{"status":"success"}\n',
            )
        return httpx.Response(200, json={"capabilities": ["completion", "tools"]})

    transport(handler)
    model = CatalogModel(
        catalog="ollama",
        id="qwen3",
        title="Qwen",
        url="https://ollama.com/library/qwen3",
    )
    variant = Variant(id="8b", label="8B", format="ollama")
    rows = [
        e
        async for e in OllamaRuntime("http://localhost:11434").install(
            model, variant, "qwen3:8b", {}
        )
    ]
    assert rows[-1]["capabilities"] == ["completion", "tools"]
    assert calls == ["/api/pull", "/api/show"]


@pytest.mark.asyncio
async def test_ollama_stream_without_success_is_not_completed(transport):
    transport(lambda _: httpx.Response(200, content=b'{"status":"pulling"}\n'))
    with pytest.raises(ModelManagerError, match="before confirming"):
        _ = [e async for e in OllamaRuntime()._stream("/api/pull", {"model": "test"})]


def test_host_control_plane_exposes_registry_and_jobs(manager):
    app = FastAPI()
    app.include_router(model_host_router(manager))
    with TestClient(app) as client:
        response = client.get("/v1/models")
        assert response.json()["catalogs"][0]["id"] == "fake_catalog"
        assert client.get("/v1/models/jobs").json() == {"jobs": []}
        assert client.post("/v1/models/jobs/no-such-job/cancel").status_code == 400


@pytest.mark.asyncio
async def test_backend_proxy_unavailable_is_actionable(monkeypatch):
    original = httpx.AsyncClient

    def fail(request):
        raise httpx.ConnectError("secret internal address", request=request)

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda *a, **kw: original(*a, **{**kw, "transport": httpx.MockTransport(fail)}),
    )
    with pytest.raises(HTTPException) as exc:
        await api.host_call("GET", "")
    assert exc.value.status_code == 503
    assert "host runtime" in exc.value.detail
    assert "secret" not in exc.value.detail


@pytest.mark.asyncio
async def test_profile_selector_keeps_roles_and_adds_installed_models(monkeypatch):
    monkeypatch.setattr(
        models_api,
        "settings",
        SimpleNamespace(
            HARDWARE_PROFILE="test",
            get_current_profile=lambda: SimpleNamespace(
                label="Local", engine="ollama", description="Test"
            ),
            get_available_models=lambda: [
                {"id": "profile:latest", "role": "default", "type": "chat"}
            ],
        ),
    )
    monkeypatch.setattr(
        models_api.providers,
        "_fetch_ollama_tags",
        AsyncMock(
            return_value=[
                {
                    "id": "downloaded:latest",
                    "type": "chat",
                    "description": "Downloaded",
                    "size": "1 GB",
                }
            ]
        ),
    )
    listing = await models_api.get_profile_models()
    assert listing["models"][0]["role"] == "default"
    assert listing["models"][1]["id"] == "downloaded:latest"
    assert listing["models"][1]["role"] == ""


@pytest.mark.asyncio
async def test_ollama_mlx_import_uses_generated_modelfile_and_no_quantization(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "REALOPEN_DATA_DIR", str(tmp_path))
    calls = []

    class Output:
        def __init__(self):
            self.done = False

        async def read(self, size):
            if self.done:
                return b""
            self.done = True
            return b"Importing original weights\n"

    process = SimpleNamespace(
        stdout=Output(), returncode=0, wait=AsyncMock(return_value=0)
    )

    async def spawn(*args, **kwargs):
        calls.append((args, kwargs))
        return process

    monkeypatch.setattr(ollama_local.asyncio, "create_subprocess_exec", spawn)
    model = CatalogModel(
        catalog="huggingface",
        id="owner/model",
        title="Model",
        url="https://huggingface.co/owner/model",
        revision="a" * 40,
    )
    events = [
        e
        async for e in ollama_local.install(
            "http://localhost:11434", model, "local-model:q4"
        )
    ]
    args, options = calls[0]
    assert args[:3] == ("ollama", "create", "local-model:q4")
    assert "--experimental" in args
    assert "--quantize" not in args
    assert not options.get("shell")
    assert Path(args[-1]).read_text() == "FROM .\n"
    assert events[-1]["stage"] == "importing"


@pytest.mark.asyncio
async def test_hf_checksum_failure_discards_bad_partial(
    tmp_path, monkeypatch, transport
):
    monkeypatch.setattr(settings, "REALOPEN_DATA_DIR", str(tmp_path))
    transport(lambda _: httpx.Response(200, content=b"bad!"))
    model = CatalogModel(
        catalog="huggingface",
        id="owner/model",
        title="Model",
        url="https://huggingface.co/owner/model",
        revision="a" * 40,
    )
    variant = Variant(
        id="model.gguf",
        label="Q4",
        format="gguf",
        size=4,
        files=[ModelFile(path="model.gguf", size=4, sha256="a" * 64)],
    )
    with pytest.raises(ModelManagerError, match="checksum"):
        _ = [e async for e in downloads.download_files(model, variant)]
    assert not (downloads.cache_root(model) / "model.gguf.part").exists()
    assert not (downloads.cache_root(model) / "model.gguf").exists()
