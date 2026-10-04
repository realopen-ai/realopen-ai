"""Tests for the setup wizard API (app/api/setup.py).

Scope:
  - GET  /api/setup/status     — marker detection + voice summary (3 fallbacks)
  - GET  /api/setup/hardware   — hardware.json passthrough + fallback payload
  - GET  /api/setup/profiles   — profiles.yml serialization
  - GET  /api/setup/modules    — module list w/ availability + requirements
  - POST /api/setup/apply      — validation errors + apply pipeline
  - POST /api/setup/pull-models — SSE streaming (Ollama probe/pull + voice)
  - POST /api/setup/complete   — marker file creation (+ OSError branch)
  - helpers: _get_project_root (incl. /app fallback), _get_data_dir (mkdir +
    swallowed OSError), _get_models_to_pull, _apply_profile_and_modules
    (ValueError branch), _apply_module_tools_for_setup, _mark_setup_complete

Mocks (no real I/O beyond the repo's own profiles.yml/modules.yml reads):
  - _get_data_dir → tmp_path (marker + hardware.json are sandboxed)
  - settings._update_env_file / _persist_module_state → recorders (real .env
    and state/ files are NEVER touched)
  - httpx.AsyncClient (Ollama probe/pull) → fake in-memory client
  - app.voice.models_store.voice_dependency_status / voice_model_installer.*
  - get_tool_registry → MagicMock

OLLAMA_BASE_URL is never contacted (the fake client raises if it would be).
"""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import setup as setup_api  # noqa: E402
from app.config import settings  # noqa: E402
from app.services import voice_model_installer as vmi  # noqa: E402
from app.voice import models_store as voice_models_store  # noqa: E402

app = FastAPI()
app.include_router(setup_api.router, prefix="/api")


# ── fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """Redirect the setup data dir (marker + hardware.json) into tmp_path."""
    d = tmp_path / "data"
    d.mkdir()
    monkeypatch.setattr(setup_api, "_get_data_dir", lambda: d)
    return d


@pytest.fixture
def settings_guard():
    """Snapshot + restore the process-global settings object around a test."""
    snapshot = {
        "HARDWARE_PROFILE": settings.HARDWARE_PROFILE,
        "ENABLED_MODULES": settings.ENABLED_MODULES,
        "_profiles": settings._profiles,
        "_modules": settings._modules,
        "_voice_config": settings._voice_config,
        "_enabled_modules_set": settings._enabled_modules_set,
    }
    yield
    for key, value in snapshot.items():
        setattr(settings, key, value)


@pytest.fixture
def no_persist(monkeypatch):
    """Neutralize settings persistence (.env / state file) and record calls."""
    calls: list = []
    monkeypatch.setattr(
        settings, "_update_env_file", lambda key, value: calls.append((key, value))
    )
    monkeypatch.setattr(
        settings, "_persist_module_state", lambda: calls.append(("persist", None))
    )
    return calls


@pytest_asyncio.fixture
async def client():
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _parse_sse(text: str) -> list:
    events = []
    for line in text.splitlines():
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
    return events


class _FakeResponse:
    def __init__(self, status_code=200, json_data=None, text="", content=b""):
        self.status_code = status_code
        self._json = json_data if json_data is not None else {}
        self.text = text
        self.content = content if content else text.encode()

    def json(self):
        return self._json

    @property
    def is_error(self):
        return self.status_code >= 400

    async def aread(self):
        return self.content


class _FakePullStream:
    """Async CM returned by FakeAsyncClient.stream()."""

    def __init__(self, lines=None, exc=None, status_code=200, error_body=b"boom"):
        self.lines = lines or []
        self.exc = exc
        self.status_code = status_code
        self.error_body = error_body

    async def __aenter__(self):
        if self.exc is not None:
            raise self.exc
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_lines(self):
        for line in self.lines:
            yield line

    async def aread(self):
        return self.error_body


class FakeAsyncClient:
    """Stand-in for httpx.AsyncClient used by the pull-models endpoint.

    The constructor records the URL so tests can assert that the only hosts
    contacted are the (mocked) Ollama endpoints — never a real network call.
    """

    probe_response: _FakeResponse = _FakeResponse(200)
    probe_exc: Exception | None = None
    pull_stream: _FakePullStream | None = None
    seen_urls: list = []

    def __init__(self, timeout=None):
        self.seen = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url):
        type(self).seen_urls.append(url)
        if type(self).probe_exc is not None:
            raise type(self).probe_exc
        return type(self).probe_response

    def stream(self, method, url, json=None):
        type(self).seen_urls.append(url)
        return type(self).pull_stream or _FakePullStream()


@pytest.fixture
def fake_ollama(monkeypatch):
    """Patch httpx.AsyncClient inside the setup module + reset fake state."""
    monkeypatch.setattr(setup_api.httpx, "AsyncClient", FakeAsyncClient)
    FakeAsyncClient.probe_response = _FakeResponse(200)
    FakeAsyncClient.probe_exc = None
    FakeAsyncClient.pull_stream = _FakePullStream()
    FakeAsyncClient.seen_urls = []
    return FakeAsyncClient


# ── GET /setup/status ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_status_incomplete_without_marker(client, data_dir, monkeypatch):
    monkeypatch.setattr(
        voice_models_store,
        "voice_dependency_status",
        lambda: {"configured": True, "ready": False, "asr": {}, "tts": {}},
    )
    r = await client.get("/api/setup/status")
    assert r.status_code == 200
    body = r.json()
    assert body["setup_complete"] is False
    assert body["profile"] is None
    assert body["voice"] == {"configured": True, "ready": False, "asr": {}, "tts": {}}


@pytest.mark.asyncio
async def test_status_complete_with_marker(client, data_dir, monkeypatch):
    (data_dir / ".setup-complete").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        voice_models_store,
        "voice_dependency_status",
        lambda: {"configured": True, "ready": True, "asr": {}, "tts": {}},
    )
    r = await client.get("/api/setup/status")
    assert r.status_code == 200
    body = r.json()
    assert body["setup_complete"] is True
    assert body["profile"] == settings.HARDWARE_PROFILE
    assert body["voice"]["ready"] is True


@pytest.mark.asyncio
async def test_status_voice_falls_back_to_installer(client, data_dir, monkeypatch):
    def _raise():
        raise RuntimeError("models_store broken")

    monkeypatch.setattr(voice_models_store, "voice_dependency_status", _raise)
    monkeypatch.setattr(
        vmi,
        "voice_status_summary",
        lambda: {"configured": False, "ready": False, "fallback": True},
    )
    r = await client.get("/api/setup/status")
    assert r.status_code == 200
    assert r.json()["voice"] == {"configured": False, "ready": False, "fallback": True}


@pytest.mark.asyncio
async def test_status_voice_both_providers_fail(client, data_dir, monkeypatch):
    def _raise():
        raise RuntimeError("no voice core")

    monkeypatch.setattr(voice_models_store, "voice_dependency_status", _raise)
    monkeypatch.setattr(vmi, "voice_status_summary", _raise)
    r = await client.get("/api/setup/status")
    assert r.status_code == 200
    voice = r.json()["voice"]
    assert voice["configured"] is False
    assert voice["ready"] is False
    assert "no voice core" in voice["error"]


# ── GET /setup/hardware ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hardware_info_from_file(client, data_dir):
    hw = {
        "platform": "darwin",
        "ram_gb": 36,
        "gpu_type": "apple",
        "gpu_vram_gb": 36,
        "recommended_profile": "apple_medium",
    }
    (data_dir / "hardware.json").write_text(json.dumps(hw), encoding="utf-8")
    r = await client.get("/api/setup/hardware")
    assert r.status_code == 200
    assert r.json() == hw


@pytest.mark.asyncio
async def test_hardware_info_fallback_when_missing(client, data_dir):
    r = await client.get("/api/setup/hardware")
    assert r.status_code == 200
    body = r.json()
    assert body["platform"] == "unknown"
    assert body["ram_gb"] == 8
    assert body["cpu_cores"] == 1
    assert body["gpu_type"] == "none"
    assert body["recommended_profile"] == "cpu_small"
    assert body["ollama_running"] is False


@pytest.mark.asyncio
async def test_hardware_info_invalid_json_falls_back(client, data_dir):
    (data_dir / "hardware.json").write_text("{not json", encoding="utf-8")
    r = await client.get("/api/setup/hardware")
    assert r.status_code == 200
    assert r.json()["platform"] == "unknown"


# ── GET /setup/profiles and /setup/modules ────────────────────────────


@pytest.mark.asyncio
async def test_profiles_lists_all_valid_profiles(client):
    r = await client.get("/api/setup/profiles")
    assert r.status_code == 200
    body = r.json()
    assert "cpu_small" in body
    for name, profile in body.items():
        assert profile["label"]
        assert profile["engine"]
        assert isinstance(profile["models"], list)
        for model in profile["models"]:
            assert {"id", "type", "role", "description", "size"} <= set(model)


@pytest.mark.asyncio
async def test_modules_payload_structure(client, data_dir):
    r = await client.get("/api/setup/modules")
    assert r.status_code == 200
    modules = {m["name"]: m for m in r.json()["modules"]}
    assert modules["assistant"]["required"] is True
    assert modules["assistant"]["requirements_met"] is True

    image_gen = modules["image_generation"]
    assert image_gen["required"] is False
    # cpu_small is not listed for image_generation → unavailable
    assert image_gen["availability"]["cpu_small"] is False
    assert image_gen["availability"]["apple_medium"] is True
    assert "profile_models" in image_gen
    assert "estimated_size" in image_gen


@pytest.mark.asyncio
async def test_modules_requirements_use_hardware_info(client, data_dir):
    (data_dir / "hardware.json").write_text(
        json.dumps({"ram_gb": 4, "gpu_vram_gb": 0, "recommended_profile": "cpu_small"}),
        encoding="utf-8",
    )
    r = await client.get("/api/setup/modules")
    modules = {m["name"]: m for m in r.json()["modules"]}
    # image_generation needs ram>=8, vram>=6 → not met on this fake host
    assert modules["image_generation"]["requirements_met"] is False
    # assistant has no minimum requirements → always met
    assert modules["assistant"]["requirements_met"] is True


# ── POST /setup/apply ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_apply_rejects_invalid_profile(client, data_dir):
    r = await client.post("/api/setup/apply", json={"profile": "nope", "enabled_modules": []})
    assert r.status_code == 400
    assert "Invalid profile 'nope'" in r.json()["detail"]
    assert "cpu_small" in r.json()["detail"]


@pytest.mark.asyncio
async def test_apply_rejects_unknown_module(client, data_dir):
    r = await client.post(
        "/api/setup/apply", json={"profile": "cpu_small", "enabled_modules": ["ghost"]}
    )
    assert r.status_code == 400
    assert "Unknown module 'ghost'" in r.json()["detail"]


@pytest.mark.asyncio
async def test_apply_rejects_module_unavailable_for_profile(client, data_dir):
    r = await client.post(
        "/api/setup/apply",
        json={"profile": "cpu_small", "enabled_modules": ["image_generation"]},
    )
    assert r.status_code == 400
    assert "not available for profile 'cpu_small'" in r.json()["detail"]


@pytest.mark.asyncio
async def test_apply_pipeline_response_contract(client, data_dir, monkeypatch):
    applied = {}
    tools_modules = []
    monkeypatch.setattr(
        setup_api,
        "_apply_profile_and_modules",
        lambda profile, modules: applied.update(profile=profile, modules=modules),
    )
    monkeypatch.setattr(
        setup_api, "_apply_module_tools_for_setup", lambda mods: tools_modules.append(mods)
    )
    monkeypatch.setattr(
        setup_api,
        "_get_models_to_pull",
        lambda profile, modules: [{"id": "qwen3:4b", "kind": "ollama"}],
    )
    r = await client.post(
        "/api/setup/apply",
        json={"profile": "cpu_medium", "enabled_modules": ["assistant"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "applied"
    assert body["profile"] == "cpu_medium"
    assert body["models_to_pull"] == [{"id": "qwen3:4b", "kind": "ollama"}]
    assert applied == {"profile": "cpu_medium", "modules": ["assistant"]}
    assert tools_modules == [["assistant"]]
    assert "assistant" in body["enabled_modules"]


@pytest.mark.asyncio
async def test_apply_updates_settings_in_memory(
    client, data_dir, settings_guard, no_persist, monkeypatch
):
    # Keep the process-global tool registry untouched (the apply pipeline
    # would otherwise unregister disabled modules' tools for real).
    registry = MagicMock()
    registry.restore_many.return_value = 0
    registry.unregister_many.return_value = 0
    monkeypatch.setattr(setup_api, "get_tool_registry", lambda: registry)

    r = await client.post(
        "/api/setup/apply",
        json={"profile": "cpu_medium", "enabled_modules": ["assistant"]},
    )
    assert r.status_code == 200
    # In-memory settings were switched
    assert settings.HARDWARE_PROFILE == "cpu_medium"
    assert "assistant" in settings.get_enabled_module_names()
    # Persistence went through the recorder instead of real files
    assert ("HARDWARE_PROFILE", "cpu_medium") in no_persist
    assert any(key == "ENABLED_MODULES" for key, _ in no_persist)
    assert ("persist", None) in no_persist


@pytest.mark.asyncio
async def test_apply_wraps_value_error_from_pipeline_in_400(client, data_dir, monkeypatch):
    # Defensive branch: the pipeline re-validates the profile and any
    # ValueError there must surface as 400, not 500.
    def _raise(profile, modules):
        raise ValueError("profiles.yml vanished mid-apply")

    monkeypatch.setattr(setup_api, "_apply_profile_and_modules", _raise)
    r = await client.post(
        "/api/setup/apply", json={"profile": "cpu_small", "enabled_modules": []}
    )
    assert r.status_code == 400
    assert "profiles.yml vanished mid-apply" in r.json()["detail"]


@pytest.mark.asyncio
async def test_apply_wraps_unexpected_error_in_500(
    client, data_dir, settings_guard, monkeypatch
):
    def _boom(profile, modules):
        raise OSError("disk full")

    monkeypatch.setattr(setup_api, "_apply_profile_and_modules", _boom)
    r = await client.post(
        "/api/setup/apply", json={"profile": "cpu_small", "enabled_modules": []}
    )
    assert r.status_code == 500
    assert "disk full" in r.json()["detail"]


def test_apply_profile_and_modules_rejects_invalid_profile_direct():
    # The helper's own guard (the endpoint pre-validates, so this is the
    # defensive second gate) — nothing is mutated before it raises.
    with pytest.raises(ValueError, match="Invalid profile: bogus"):
        setup_api._apply_profile_and_modules("bogus", [])


def test_apply_module_tools_registers_and_unregisters(monkeypatch):
    registry = MagicMock()
    registry.restore_many.return_value = 2
    registry.unregister_many.return_value = 1
    monkeypatch.setattr(setup_api, "get_tool_registry", lambda: registry)

    setup_api._apply_module_tools_for_setup(["assistant"])

    restored = {tuple(call.args[0]) for call in registry.restore_many.call_args_list}
    unregistered = {
        tuple(call.args[0]) for call in registry.unregister_many.call_args_list
    }
    modules = settings.get_modules()
    # Every module gets exactly one restore/unregister decision
    assert len(restored | unregistered) == len(modules)
    assert tuple(modules["assistant"].tools) in restored
    assert tuple(modules["image_generation"].tools) in unregistered


# ── _get_models_to_pull ───────────────────────────────────────────────


def test_get_models_to_pull_includes_profile_and_voice_entries(monkeypatch):
    monkeypatch.setattr(
        vmi,
        "voice_runtime_entries",
        lambda profile: [{"id": "torch-cpu", "kind": "voice_runtime", "provider": "pip"}],
    )
    monkeypatch.setattr(
        vmi,
        "voice_model_entries",
        lambda profile: [
            {"id": "qwen3-asr", "kind": "voice_model", "provider": "qwen3-asr"},
            {"id": "pocket-tts", "kind": "voice_model", "provider": "pocket-tts"},
        ],
    )
    models = setup_api._get_models_to_pull("cpu_small", ["assistant"])

    assert models, "profile models must be present"
    ollama_models = [m for m in models if m["kind"] == "ollama"]
    assert ollama_models, "cpu_small profile defines ollama models"
    for entry in ollama_models:
        assert entry["module"] == "assistant"
        assert entry["provider"] == "ollama"
        assert entry["id"]

    kinds = [m["kind"] for m in models]
    assert "voice_runtime" in kinds
    assert kinds.count("voice_model") == 2
    # Sequential plan: ollama first, then voice runtime, then voice models
    assert kinds[0] == "ollama"
    assert kinds[-1] == "voice_model"


def test_get_models_to_pull_includes_available_optional_module(monkeypatch):
    # image_generation is optional and HAS models for apple_medium → its
    # models are appended with module="image_generation".
    monkeypatch.setattr(
        vmi, "voice_enabled_for_profile", lambda profile, modules=None: False
    )
    models = setup_api._get_models_to_pull("apple_medium", ["image_generation"])
    module_entries = [m for m in models if m["module"] == "image_generation"]
    assert module_entries, "optional module models must be in the pull plan"
    assert all(
        m["kind"] == "ollama" and m["provider"] == "ollama" for m in module_entries
    )
    assert module_entries[0]["id"] == "x/flux2-klein:4b"
    assert module_entries[0]["role"] == "default_image_gen"


def test_get_models_to_pull_skips_unavailable_module(monkeypatch):
    monkeypatch.setattr(vmi, "voice_runtime_entries", lambda profile: [])
    monkeypatch.setattr(vmi, "voice_model_entries", lambda profile: [])
    models = setup_api._get_models_to_pull("cpu_small", ["image_generation"])
    module_names = {m["module"] for m in models}
    # image_generation has no cpu_small models → nothing from that module
    assert "image_generation" not in module_names


def test_get_models_to_pull_unknown_profile_still_adds_voice(monkeypatch):
    monkeypatch.setattr(
        vmi, "voice_runtime_entries", lambda profile: [{"id": "pip-x", "kind": "voice_runtime"}]
    )
    monkeypatch.setattr(vmi, "voice_model_entries", lambda profile: [])
    models = setup_api._get_models_to_pull("definitely_not_a_profile", [])
    assert [m["id"] for m in models] == ["pip-x"]


def test_voice_entries_omitted_when_voice_module_disabled(monkeypatch):
    fake_modules = dict(settings.get_modules())

    class _DisabledModule:
        required = False

        def is_available_for_profile(self, profile):
            return False

    fake_modules["voice"] = _DisabledModule()
    monkeypatch.setattr(settings, "_modules", fake_modules)
    monkeypatch.setattr(settings, "_enabled_modules_set", {"assistant"})
    monkeypatch.setattr(vmi, "voice_runtime_entries", lambda profile: [])
    monkeypatch.setattr(vmi, "voice_model_entries", lambda profile: [])
    # voice_enabled_for_profile consults get_modules() itself; patch the gate
    monkeypatch.setattr(
        vmi, "voice_enabled_for_profile", lambda profile, modules=None: False
    )
    models = setup_api._get_models_to_pull("cpu_small", [])
    assert all(m.get("kind") == "ollama" for m in models)


# ── POST /setup/pull-models (SSE) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_pull_models_rejects_invalid_profile(client, data_dir):
    r = await client.post(
        "/api/setup/pull-models",
        json={"profile": "not-a-profile", "enabled_modules": []},
    )
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_pull_models_no_models_short_circuits(client, data_dir, monkeypatch):
    monkeypatch.setattr(setup_api, "_get_models_to_pull", lambda profile, modules: [])
    r = await client.post(
        "/api/setup/pull-models",
        json={"profile": "cpu_small", "enabled_modules": ["assistant"]},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "no_models", "message": "No models to pull"}


@pytest.mark.asyncio
async def test_pull_models_sse_happy_path(client, data_dir, monkeypatch, fake_ollama):
    monkeypatch.setattr(
        setup_api,
        "_get_models_to_pull",
        lambda profile, modules: [
            {"id": "chat:4b", "module": "assistant", "provider": "ollama", "kind": "ollama"},
            {"id": "torch-cpu", "module": "voice", "provider": "pip", "kind": "voice_runtime"},
        ],
    )
    FakeAsyncClient.pull_stream = _FakePullStream(
        lines=[
            json.dumps({"status": "verifying sha256 digest"}),
            "   ",  # blank line → skipped
            "not-json{{",  # malformed → skipped
            json.dumps({"status": "pulling 5 MB", "completed": 5, "total": 10}),
            json.dumps({"status": "success"}),
        ]
    )

    async def fake_voice_stream(*args, **kwargs):
        yield {"event": "pull_start", "model": "torch-cpu", "provider": "pip",
               "kind": "voice_runtime"}
        yield {"event": "pull_done", "model": "torch-cpu", "provider": "pip",
               "kind": "voice_runtime"}

    monkeypatch.setattr(
        vmi, "stream_install_voice_dependencies", fake_voice_stream
    )

    r = await client.post(
        "/api/setup/pull-models",
        json={"profile": "cpu_small", "enabled_modules": ["assistant"]},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _parse_sse(r.text)
    names = [e["event"] for e in events]

    assert names[0] == "pull_start_all"
    assert events[0]["total"] == 2
    assert "pull_start" in names
    assert "pull_progress" in names
    assert "pull_status" in names
    assert "pull_done" in names
    assert names[-1] == "pull_all_done"

    progress = next(e for e in events if e["event"] == "pull_progress")
    assert progress["percent"] == 50
    assert progress["completed"] == 5
    assert progress["provider"] == "ollama"
    assert progress["index"] == 0
    assert progress["total_models"] == 2

    # Only the mocked Ollama endpoints were "contacted"
    assert all("11434" in url for url in FakeAsyncClient.seen_urls)


@pytest.mark.asyncio
async def test_pull_models_ollama_unreachable_reports_error_per_model(
    client, data_dir, monkeypatch, fake_ollama
):
    monkeypatch.setattr(
        setup_api,
        "_get_models_to_pull",
        lambda profile, modules: [
            {"id": "chat:4b", "module": "assistant", "provider": "ollama", "kind": "ollama"},
        ],
    )
    FakeAsyncClient.probe_exc = httpx.ConnectError("refused")

    r = await client.post(
        "/api/setup/pull-models",
        json={"profile": "cpu_small", "enabled_modules": ["assistant"]},
    )
    events = _parse_sse(r.text)
    names = [e["event"] for e in events]
    assert names == ["pull_start_all", "pull_start", "pull_error", "pull_all_done"]
    error = events[2]
    assert "Cannot connect to Ollama" in error["error"]
    assert error["model"] == "chat:4b"


@pytest.mark.asyncio
async def test_pull_models_probe_non_200(client, data_dir, monkeypatch, fake_ollama):
    monkeypatch.setattr(
        setup_api,
        "_get_models_to_pull",
        lambda profile, modules: [
            {"id": "m", "module": "assistant", "provider": "ollama", "kind": "ollama"},
        ],
    )
    FakeAsyncClient.probe_response = _FakeResponse(503)
    r = await client.post(
        "/api/setup/pull-models",
        json={"profile": "cpu_small", "enabled_modules": []},
    )
    events = _parse_sse(r.text)
    error = next(e for e in events if e["event"] == "pull_error")
    assert "not reachable" in error["error"]


@pytest.mark.asyncio
async def test_pull_models_stream_http_error_status(client, data_dir, monkeypatch, fake_ollama):
    monkeypatch.setattr(
        setup_api,
        "_get_models_to_pull",
        lambda profile, modules: [
            {"id": "m", "module": "assistant", "provider": "ollama", "kind": "ollama"},
        ],
    )
    FakeAsyncClient.pull_stream = _FakePullStream(
        status_code=404, error_body=b"model not found"
    )
    r = await client.post(
        "/api/setup/pull-models",
        json={"profile": "cpu_small", "enabled_modules": []},
    )
    events = _parse_sse(r.text)
    error = next(e for e in events if e["event"] == "pull_error")
    assert "model not found" in error["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "exc,expected",
    [
        (httpx.TimeoutException("slow"), "timed out"),
        (httpx.ConnectError("down"), "Cannot connect to Ollama"),
        (RuntimeError("weird"), "weird"),
    ],
)
async def test_pull_models_stream_exception_paths(
    client, data_dir, monkeypatch, fake_ollama, exc, expected
):
    monkeypatch.setattr(
        setup_api,
        "_get_models_to_pull",
        lambda profile, modules: [
            {"id": "m", "module": "assistant", "provider": "ollama", "kind": "ollama"},
        ],
    )
    FakeAsyncClient.pull_stream = _FakePullStream(exc=exc)
    r = await client.post(
        "/api/setup/pull-models",
        json={"profile": "cpu_small", "enabled_modules": []},
    )
    events = _parse_sse(r.text)
    error = next(e for e in events if e["event"] == "pull_error")
    assert expected in error["error"]
    assert events[-1]["event"] == "pull_all_done"


@pytest.mark.asyncio
async def test_pull_models_voice_only_plan_never_probes_ollama(
    client, data_dir, monkeypatch, fake_ollama
):
    monkeypatch.setattr(
        setup_api,
        "_get_models_to_pull",
        lambda profile, modules: [
            {"id": "torch-cpu", "module": "voice", "provider": "pip",
             "kind": "voice_runtime"},
            {"id": "qwen3-asr", "module": "voice", "provider": "qwen3-asr",
             "kind": "voice_model"},
        ],
    )

    async def fake_voice_stream(*args, **kwargs):
        yield {"event": "pull_done", "model": "torch-cpu", "kind": "voice_runtime"}
        yield {"event": "pull_done", "model": "qwen3-asr", "kind": "voice_model"}

    monkeypatch.setattr(
        vmi, "stream_install_voice_dependencies", fake_voice_stream
    )
    r = await client.post(
        "/api/setup/pull-models",
        json={"profile": "cpu_small", "enabled_modules": []},
    )
    events = _parse_sse(r.text)
    names = [e["event"] for e in events]
    # No ollama models in the plan → zero httpx calls at all
    assert FakeAsyncClient.seen_urls == []
    assert names == ["pull_start_all", "pull_done", "pull_done", "pull_all_done"]


@pytest.mark.asyncio
async def test_pull_models_voice_stream_receives_index_offset(
    client, data_dir, monkeypatch, fake_ollama
):
    captured = {}

    monkeypatch.setattr(
        setup_api,
        "_get_models_to_pull",
        lambda profile, modules: [
            {"id": "m1", "module": "assistant", "provider": "ollama", "kind": "ollama"},
            {"id": "m2", "module": "assistant", "provider": "ollama", "kind": "ollama"},
            {"id": "vm", "module": "voice", "provider": "pip", "kind": "voice_runtime"},
        ],
    )

    async def fake_voice_stream(profile, modules, index_offset=0, total_models=None):
        captured["index_offset"] = index_offset
        captured["total_models"] = total_models
        yield {"event": "pull_done", "model": "vm", "kind": "voice_runtime"}

    monkeypatch.setattr(vmi, "stream_install_voice_dependencies", fake_voice_stream)
    r = await client.post(
        "/api/setup/pull-models",
        json={"profile": "cpu_small", "enabled_modules": []},
    )
    assert r.status_code == 200
    assert captured == {"index_offset": 2, "total_models": 3}


# ── POST /setup/complete ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_complete_setup_writes_marker(client, data_dir):
    r = await client.post("/api/setup/complete")
    assert r.status_code == 200
    assert r.json()["status"] == "complete"
    assert r.json()["profile"] == settings.HARDWARE_PROFILE

    marker = data_dir / ".setup-complete"
    assert marker.exists()
    payload = json.loads(marker.read_text(encoding="utf-8"))
    assert payload["profile"] == settings.HARDWARE_PROFILE
    assert "assistant" in payload["enabled_modules"]

    # The status endpoint now reports complete
    r2 = await client.get("/api/setup/status")
    assert r2.json()["setup_complete"] is True


@pytest.mark.asyncio
async def test_mark_setup_complete_swallows_oserror(client, tmp_path, monkeypatch):
    # A data dir whose parent is a *file* → mkdir fails (swallowed) and the
    # marker write raises NotADirectoryError (an OSError) → logged, not raised.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setattr(setup_api, "_get_data_dir", lambda: blocker / "data")
    r = await client.post("/api/setup/complete")
    assert r.status_code == 200
    assert r.json()["status"] == "complete"


# ── helpers ───────────────────────────────────────────────────────────


class _FakeRootPath:
    """Path stand-in where no candidate has profiles.yml/.env."""

    def __init__(self, value):
        self._value = str(value)

    @property
    def parent(self):
        return self  # .parent chains to itself (only used for the repo-root probe)

    def __truediv__(self, other):
        return _FakeRootPath(f"{self._value}/{other}")

    def exists(self):
        return False

    def __eq__(self, other):
        return isinstance(other, _FakeRootPath) and other._value == self._value

    def __repr__(self):
        return f"_FakeRootPath({self._value!r})"


def test_get_project_root_falls_back_to_app(monkeypatch):
    # Neither candidate ("/app" nor the repo root) has profiles.yml/.env
    # → the documented /app fallback is returned.
    monkeypatch.setattr(setup_api, "Path", _FakeRootPath)
    root = setup_api._get_project_root()
    assert root == _FakeRootPath("/app")


def test_get_data_dir_creates_and_returns_data_dir(monkeypatch, tmp_path):
    monkeypatch.setattr(setup_api, "_get_project_root", lambda: tmp_path)
    data_dir = setup_api._get_data_dir()
    assert data_dir == tmp_path / "data"
    assert data_dir.is_dir()  # created (parents=True, exist_ok=True)


def test_get_data_dir_swallows_mkdir_oserror(monkeypatch, tmp_path):
    # The data dir's parent exists as a FILE → mkdir raises
    # (NotADirectoryError, an OSError) → swallowed, path still returned.
    blocker = tmp_path / "blocker"
    blocker.write_text("i am a file", encoding="utf-8")
    monkeypatch.setattr(setup_api, "_get_project_root", lambda: blocker)
    assert setup_api._get_data_dir() == blocker / "data"


def test_get_project_root_contains_profiles():
    root = setup_api._get_project_root()
    assert (root / "profiles.yml").exists() or (root / ".env").exists()


def test_marker_filename_constant():
    assert setup_api.SETUP_COMPLETE_FILENAME == ".setup-complete"


def test_apply_setup_request_models_hold_lists():
    req = setup_api.ApplySetupRequest(profile="cpu_small", enabled_modules=["assistant"])
    assert req.profile == "cpu_small"
    assert req.enabled_modules == ["assistant"]
    req2 = setup_api.InstallSetupModelsRequest(profile="cpu_small", enabled_modules=[])
    assert req2.enabled_modules == []
