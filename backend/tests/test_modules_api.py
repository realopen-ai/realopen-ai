"""
API-level tests for app/api/modules.py — module management endpoints.

Endpoints covered (httpx + ASGITransport against the real router):
  - GET  /api/modules                       list with status/requirements/
                                            download flags per module
  - POST /api/modules/toggle                enable/disable with tool-registry
                                            side effects, all 4xx branches
  - GET  /api/modules/{name}/status         per-model download status
  - POST /api/modules/install               SSE model-pull stream (progress,
                                            done, error, cancel, non-200)
  - GET  /api/modules/models/downloaded     enabled-module model status map

What is mocked — no Ollama/network/DB access:
  - settings.get_modules / is_module_enabled / toggle_module (monkeypatched
    on the settings singleton; real ModuleConfig objects are used so all
    profile/requirement logic runs for real)
  - settings.HARDWARE_PROFILE (pinned to "cpu_small"; _get_hardware_info's
    own profile-estimate fallbacks are tested directly with a stubbed
    settings object + patched pathlib.Path)
  - app.api.modules._get_hardware_info       → fixed RAM/VRAM
  - ModuleConfig.check_model_downloaded      → table-driven async stub
  - app.api.modules.get_tool_registry        → recording fake registry
  - app.api.modules.httpx                    → fake AsyncClient whose
    .stream() yields scripted Ollama pull lines (never touches
    OLLAMA_BASE_URL for real)
"""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.api import modules as modules_api  # noqa: E402
from app.config import ModuleConfig, settings  # noqa: E402

app = FastAPI()
app.include_router(modules_api.router, prefix="/api")

PROFILE = "cpu_small"


def make_modules():
    """assistant (required) + three optional modules covering every branch."""
    return {
        "assistant": ModuleConfig(
            "assistant",
            {
                "required": True,
                "label": "AI Assistant",
                "description": "Core assistant",
                "icon": "bot",
                "tools": ["use_websearch", "use_code_exec"],
                "models": [{"role": "default"}],
            },
        ),
        "image_generation": ModuleConfig(
            "image_generation",
            {
                "label": "Image Generation",
                "description": "Draws pictures",
                "icon": "image",
                "tools": ["use_image_gen"],
                "models": {
                    PROFILE: [
                        {
                            "id": "sd:1b",
                            "type": "image",
                            "role": "image",
                            "description": "tiny diffusion",
                            "size": "1 GB",
                        }
                    ]
                },
                "minimum_requirements": {"ram": 8, "vram": 0},
                "estimated_size": "2 GB",
            },
        ),
        # only available on gpu_large → unavailable for the test profile
        "workspace_coder": ModuleConfig(
            "workspace_coder",
            {
                "label": "Workspace Coder",
                "description": "Agent that codes",
                "tools": ["use_code_exec"],
                "models": {
                    "gpu_large": [
                        {"id": "qwen3-coder:30b", "role": "coder", "size": "18 GB"}
                    ]
                },
                "minimum_requirements": {"ram": 32, "vram": 16},
            },
        ),
        # available but needs more RAM than the fake hardware has
        "heavy_module": ModuleConfig(
            "heavy_module",
            {
                "label": "Heavy",
                "description": "Needs a big box",
                "tools": [],
                "models": {PROFILE: [{"id": "big:70b", "role": "chat", "size": "40 GB"}]},
                "minimum_requirements": {"ram": 64, "vram": 32},
            },
        ),
    }


def sse_events(body: str):
    events = []
    for frame in body.strip().split("\n\n"):
        if frame.startswith("data: "):
            events.append(json.loads(frame[6:]))
    return events


class FakePullResponse:
    def __init__(self, status_code, lines, error_bytes):
        self.status_code = status_code
        self._lines = lines
        self._error_bytes = error_bytes

    async def aread(self):
        return self._error_bytes

    def aiter_lines(self):
        async def _gen():
            for line in self._lines:
                yield line

        return _gen()


class FakePullClient:
    """httpx.AsyncClient double for the Ollama pull stream."""

    def __init__(self, env, timeout=None):
        self._env = env

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def stream(self, method, url, json=None):
        env = self._env
        env.pull_urls.append(url)
        spec = env.pull_response
        exc = spec.get("exc")

        class _StreamCtx:
            async def __aenter__(self):
                if exc is not None:
                    raise exc
                return FakePullResponse(
                    spec.get("status_code", 200),
                    spec.get("lines", []),
                    spec.get("error_bytes", b""),
                )

            async def __aexit__(self, *a):
                return False

        return _StreamCtx()


@pytest.fixture
def env(monkeypatch):
    e = SimpleNamespace(
        modules=make_modules(),
        enabled={"assistant"},
        enabled_calls=[],
        toggle_calls=[],
        toggle_result=True,
        hw={"ram_gb": 8, "gpu_type": "cpu", "gpu_vram_gb": 0},
        downloaded={"sd:1b": True},
        check_calls=[],
        registry_calls=[],
        pull_urls=[],
        pull_response={"status_code": 200, "lines": [], "error_bytes": b"err"},
    )

    def fake_is_enabled(name):
        e.enabled_calls.append(name)
        return name in e.enabled

    def fake_toggle(name, enabled):
        e.toggle_calls.append({"module": name, "enabled": enabled})
        if not e.toggle_result:
            return False
        if enabled:
            e.enabled.add(name)
        else:
            e.enabled.discard(name)
        return True

    async def fake_hw():
        return dict(e.hw)

    async def fake_check(model_id):
        e.check_calls.append(model_id)
        return e.downloaded.get(model_id, False)

    registry = SimpleNamespace(
        restore_many=lambda names: e.registry_calls.append(("restore", list(names)))
        or len(names),
        unregister_many=lambda names: e.registry_calls.append(
            ("unregister", list(names))
        )
        or len(names),
    )

    # Settings is a pydantic BaseSettings — attributes cannot be assigned on
    # the instance, so the whole `settings` NAME in the modules module is
    # swapped for a plain namespace with the same surface.
    fake_settings = SimpleNamespace(
        HARDWARE_PROFILE=PROFILE,
        OLLAMA_BASE_URL=settings.OLLAMA_BASE_URL,
        get_modules=lambda: e.modules,
        is_module_enabled=fake_is_enabled,
        toggle_module=fake_toggle,
    )

    monkeypatch.setattr(modules_api, "settings", fake_settings)
    monkeypatch.setattr(modules_api, "_get_hardware_info", fake_hw)
    monkeypatch.setattr(ModuleConfig, "check_model_downloaded", fake_check)
    monkeypatch.setattr(modules_api, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(
        modules_api,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kw: FakePullClient(e, **kw),
            ConnectError=httpx.ConnectError,
            TimeoutException=httpx.TimeoutException,
        ),
    )
    return e


@pytest.fixture
def client():
    transport = httpx.ASGITransport(app=app)
    yield httpx.AsyncClient(transport=transport, base_url="http://test")


# ══════════════════════════════════════════════════════════════════════
# GET /modules
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_hardware_info_reads_hardware_json(monkeypatch):
    def fake_exists(self):
        return self.name == "hardware.json"

    def fake_read_text(self, encoding="utf-8"):
        return json.dumps({"ram_gb": 24, "gpu_type": "nvidia", "gpu_vram_gb": 12})

    monkeypatch.setattr(Path, "exists", fake_exists)
    monkeypatch.setattr(Path, "read_text", fake_read_text)
    info = await modules_api._get_hardware_info()
    assert info == {"ram_gb": 24, "gpu_type": "nvidia", "gpu_vram_gb": 12}


@pytest.mark.asyncio
async def test_get_hardware_info_corrupt_json_falls_back_to_profile(monkeypatch):
    monkeypatch.setattr(Path, "exists", lambda self: True)
    monkeypatch.setattr(Path, "read_text", lambda self, encoding="utf-8": "{broken")
    monkeypatch.setattr(
        modules_api, "settings", SimpleNamespace(HARDWARE_PROFILE="cpu_small")
    )
    info = await modules_api._get_hardware_info()
    assert info == {"ram_gb": 8, "gpu_type": "cpu", "gpu_vram_gb": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile,expected",
    [
        ("cpu_small", {"ram_gb": 8, "gpu_type": "cpu", "gpu_vram_gb": 0}),
        ("cpu_medium", {"ram_gb": 16, "gpu_type": "unknown", "gpu_vram_gb": 8}),
        ("gpu_large", {"ram_gb": 32, "gpu_type": "unknown", "gpu_vram_gb": 16}),
        ("mystery_profile", {"ram_gb": 64, "gpu_type": "unknown", "gpu_vram_gb": 32}),
    ],
)
async def test_get_hardware_info_profile_estimates(monkeypatch, profile, expected):
    monkeypatch.setattr(Path, "exists", lambda self: False)
    monkeypatch.setattr(
        modules_api, "settings", SimpleNamespace(HARDWARE_PROFILE=profile)
    )
    assert await modules_api._get_hardware_info() == expected


@pytest.mark.asyncio
async def test_apply_module_tools_unknown_module_is_noop(env):
    modules_api._apply_module_tools("ghost", True)
    assert env.registry_calls == []


@pytest.mark.asyncio
async def test_check_module_models_downloaded_no_models_for_profile(env):
    # workspace_coder only defines models for gpu_large → nothing to check
    result = await modules_api._check_module_models_downloaded(
        env.modules["workspace_coder"], PROFILE
    )
    assert result is True
    assert env.check_calls == []


@pytest.mark.asyncio
async def test_list_modules_shape_and_flags(client, env):
    r = await client.get("/api/modules")
    assert r.status_code == 200
    body = r.json()
    assert body["profile"] == PROFILE
    by_name = {m["name"]: m for m in body["modules"]}
    assert set(by_name) == {
        "assistant",
        "image_generation",
        "workspace_coder",
        "heavy_module",
    }

    assistant = by_name["assistant"]
    assert assistant["required"] is True
    assert assistant["enabled"] is True
    assert assistant["available"] is True
    assert assistant["can_toggle"] is False
    assert assistant["requirements_met"] is True
    assert assistant["models_downloaded"] is True  # not checked for required
    assert assistant["model_roles"] == ["default"]

    image = by_name["image_generation"]
    assert image["required"] is False
    assert image["enabled"] is False
    assert image["available"] is True
    assert image["can_toggle"] is True
    assert image["requirements_met"] is True  # 8 GB RAM requirement met
    assert image["models_downloaded"] is True  # sd:1b in the downloaded table
    assert image["models"][0]["id"] == "sd:1b"

    coder = by_name["workspace_coder"]
    assert coder["available"] is False
    assert coder["requirements_met"] is False  # needs 32 GB RAM

    heavy = by_name["heavy_module"]
    assert heavy["available"] is True
    assert heavy["requirements_met"] is False  # needs 64 GB RAM


@pytest.mark.asyncio
async def test_list_modules_reports_missing_models(client, env):
    env.downloaded = {}  # nothing downloaded
    r = await client.get("/api/modules")
    by_name = {m["name"]: m for m in r.json()["modules"]}
    assert by_name["image_generation"]["models_downloaded"] is False


# ══════════════════════════════════════════════════════════════════════
# POST /modules/toggle
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_toggle_unknown_module_404(client, env):
    r = await client.post(
        "/api/modules/toggle", json={"module": "ghost", "enabled": True}
    )
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]
    assert env.toggle_calls == []


@pytest.mark.asyncio
async def test_toggle_required_module_400(client, env):
    r = await client.post(
        "/api/modules/toggle", json={"module": "assistant", "enabled": False}
    )
    assert r.status_code == 400
    assert "Cannot toggle required module" in r.json()["detail"]


@pytest.mark.asyncio
async def test_toggle_enable_unavailable_profile_400(client, env):
    r = await client.post(
        "/api/modules/toggle", json={"module": "workspace_coder", "enabled": True}
    )
    assert r.status_code == 400
    assert "not available for hardware profile" in r.json()["detail"]


@pytest.mark.asyncio
async def test_toggle_enable_insufficient_hardware_400(client, env):
    r = await client.post(
        "/api/modules/toggle", json={"module": "heavy_module", "enabled": True}
    )
    assert r.status_code == 400
    detail = r.json()["detail"]
    assert "requires 64 GB RAM" in detail
    assert "32 GB VRAM" in detail
    assert "Your hardware has 8 GB RAM" in detail


@pytest.mark.asyncio
async def test_toggle_enable_restores_tools(client, env):
    r = await client.post(
        "/api/modules/toggle", json={"module": "image_generation", "enabled": True}
    )
    assert r.status_code == 200
    assert r.json() == {
        "module": "image_generation",
        "enabled": True,
        "models_downloaded": True,
    }
    assert env.toggle_calls == [{"module": "image_generation", "enabled": True}]
    assert env.registry_calls == [("restore", ["use_image_gen"])]
    assert "image_generation" in env.enabled


@pytest.mark.asyncio
async def test_toggle_disable_unregisters_tools(client, env):
    env.enabled.add("image_generation")
    r = await client.post(
        "/api/modules/toggle", json={"module": "image_generation", "enabled": False}
    )
    assert r.status_code == 200
    # disabling skips the download check → models_downloaded stays True
    assert r.json() == {
        "module": "image_generation",
        "enabled": False,
        "models_downloaded": True,
    }
    assert env.registry_calls == [("unregister", ["use_image_gen"])]
    assert "image_generation" not in env.enabled


@pytest.mark.asyncio
async def test_toggle_failure_500(client, env):
    env.toggle_result = False
    r = await client.post(
        "/api/modules/toggle", json={"module": "image_generation", "enabled": True}
    )
    assert r.status_code == 500
    assert "Failed to toggle" in r.json()["detail"]


# ══════════════════════════════════════════════════════════════════════
# GET /modules/{name}/status
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_module_status_lists_models_with_download_state(client, env):
    env.downloaded = {"sd:1b": True}
    r = await client.get("/api/modules/image_generation/status")
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "image_generation"
    assert body["enabled"] is False
    assert body["available"] is True
    assert body["models"] == [
        {
            "id": "sd:1b",
            "role": "image",
            "description": "tiny diffusion",
            "size": "1 GB",
            "downloaded": True,
        }
    ]


@pytest.mark.asyncio
async def test_module_status_not_downloaded(client, env):
    env.downloaded = {}
    r = await client.get("/api/modules/image_generation/status")
    assert r.json()["models"][0]["downloaded"] is False


@pytest.mark.asyncio
async def test_module_status_unavailable_module_has_no_models(client, env):
    r = await client.get("/api/modules/workspace_coder/status")
    assert r.status_code == 200
    body = r.json()
    assert body["available"] is False
    assert body["models"] == []


@pytest.mark.asyncio
async def test_module_status_unknown_404(client, env):
    r = await client.get("/api/modules/nope/status")
    assert r.status_code == 404


# ══════════════════════════════════════════════════════════════════════
# POST /modules/install (SSE pull stream)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_install_streams_pull_progress(client, env):
    env.enabled.add("image_generation")
    env.pull_response = {
        "status_code": 200,
        "lines": [
            "",
            '{"status": "pulling manifest"}',
            '{"status": "verifying sha256 digest"}',
            '{"status": "pulling 5B", "completed": 50, "total": 100}',
            "not-json-line",
            '{"status": "success"}',
        ],
    }
    r = await client.post(
        "/api/modules/install", json={"module": "image_generation"}
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = sse_events(r.text)

    kinds = [ev["event"] for ev in events]
    assert kinds == [
        "pull_start",
        "pull_progress",  # "pulling manifest" → progress with pct 0
        "pull_status",    # any other status line
        "pull_progress",
        "pull_done",
        "install_complete",
    ]
    assert events[0] == {
        "event": "pull_start",
        "model": "sd:1b",
        "index": 0,
        "total": 1,
    }
    assert events[1]["percent"] == 0  # no completed/total on the manifest line
    assert events[3]["percent"] == 50
    assert events[4] == {
        "event": "pull_done",
        "model": "sd:1b",
        "index": 0,
        "total": 1,
    }
    assert events[5] == {"event": "install_complete", "module": "image_generation"}
    # the pull went to (fake) Ollama with the model name
    assert env.pull_urls and env.pull_urls[0].endswith("/api/pull")


@pytest.mark.asyncio
async def test_install_unknown_module_404(client, env):
    r = await client.post("/api/modules/install", json={"module": "ghost"})
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_install_required_module_400(client, env):
    r = await client.post("/api/modules/install", json={"module": "assistant"})
    assert r.status_code == 400
    assert "pulled during setup" in r.json()["detail"]


@pytest.mark.asyncio
async def test_install_unavailable_module_400(client, env):
    r = await client.post("/api/modules/install", json={"module": "workspace_coder"})
    assert r.status_code == 400
    assert "not available for profile" in r.json()["detail"]


@pytest.mark.asyncio
async def test_install_module_without_models_400(client, env):
    env.modules["empty_module"] = ModuleConfig(
        "empty_module", {"label": "Empty", "models": {PROFILE: []}}
    )
    r = await client.post("/api/modules/install", json={"module": "empty_module"})
    assert r.status_code == 400
    assert "No models defined" in r.json()["detail"]


@pytest.mark.asyncio
async def test_install_non_200_response_yields_pull_error(client, env):
    env.enabled.add("image_generation")
    env.pull_response = {"status_code": 500, "lines": [], "error_bytes": b"boom"}
    r = await client.post(
        "/api/modules/install", json={"module": "image_generation"}
    )
    events = sse_events(r.text)
    kinds = [ev["event"] for ev in events]
    assert kinds == ["pull_start", "pull_error", "install_complete"]
    assert events[1]["error"] == "boom"


@pytest.mark.asyncio
async def test_install_connect_error_yields_pull_error(client, env):
    env.enabled.add("image_generation")
    env.pull_response = {"exc": httpx.ConnectError("refused")}
    r = await client.post(
        "/api/modules/install", json={"module": "image_generation"}
    )
    events = sse_events(r.text)
    pull_error = [ev for ev in events if ev["event"] == "pull_error"][0]
    assert pull_error["error"] == "Cannot connect to Ollama. Is it running?"
    assert events[-1]["event"] == "install_complete"


@pytest.mark.asyncio
async def test_install_timeout_yields_pull_error(client, env):
    env.enabled.add("image_generation")
    env.pull_response = {"exc": httpx.TimeoutException("slow")}
    r = await client.post(
        "/api/modules/install", json={"module": "image_generation"}
    )
    events = sse_events(r.text)
    pull_error = [ev for ev in events if ev["event"] == "pull_error"][0]
    assert pull_error["error"] == "Model pull timed out"


@pytest.mark.asyncio
async def test_install_unexpected_error_yields_pull_error(client, env):
    env.enabled.add("image_generation")
    env.pull_response = {"exc": RuntimeError("ollama exploded")}
    r = await client.post(
        "/api/modules/install", json={"module": "image_generation"}
    )
    events = sse_events(r.text)
    pull_error = [ev for ev in events if ev["event"] == "pull_error"][0]
    assert "ollama exploded" in pull_error["error"]
    assert events[-1]["event"] == "install_complete"


@pytest.mark.asyncio
async def test_install_debug_mode_prints_pull_chunks(client, env, monkeypatch, capsys):
    env.enabled.add("image_generation")
    monkeypatch.setattr(modules_api, "is_debug", lambda: True)
    env.pull_response = {
        "status_code": 200,
        "lines": ['{"status": "pulling manifest"}'],
    }
    r = await client.post(
        "/api/modules/install", json={"module": "image_generation"}
    )
    assert [ev["event"] for ev in sse_events(r.text)] == [
        "pull_start",
        "pull_progress",
        "install_complete",
    ]
    # the raw Ollama chunk was echoed to stdout in debug mode
    assert "pulling manifest" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_install_cancelled_when_module_disabled_midstream(
    client, env, monkeypatch
):
    # two models: enabled check passes for the first, fails for the second
    env.modules["dual_module"] = ModuleConfig(
        "dual_module",
        {
            "label": "Dual",
            "tools": [],
            "models": {
                PROFILE: [
                    {"id": "m1:1b", "role": "chat", "size": "1 GB"},
                    {"id": "m2:1b", "role": "chat", "size": "1 GB"},
                ]
            },
        },
    )
    checks = iter([True, False])

    def flaky_is_enabled(name):
        return next(checks)

    monkeypatch.setattr(modules_api.settings, "is_module_enabled", flaky_is_enabled)
    env.pull_response = {
        "status_code": 200,
        "lines": ['{"status": "success"}'],
    }
    r = await client.post("/api/modules/install", json={"module": "dual_module"})
    events = sse_events(r.text)
    kinds = [ev["event"] for ev in events]
    assert kinds == ["pull_start", "pull_done", "install_cancelled"]
    assert events[2]["reason"] == "Module was disabled during install"


# ══════════════════════════════════════════════════════════════════════
# GET /modules/models/downloaded
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_models_downloaded_covers_enabled_optional_modules(client, env):
    env.enabled = {"assistant", "image_generation"}
    env.downloaded = {"sd:1b": True}
    r = await client.get("/api/modules/models/downloaded")
    assert r.status_code == 200
    body = r.json()
    assert body == {
        "image_generation": [{"id": "sd:1b", "downloaded": True}]
    }


@pytest.mark.asyncio
async def test_models_downloaded_skips_unavailable_enabled_module(client, env):
    env.enabled = {"assistant", "workspace_coder", "image_generation"}
    env.downloaded = {"sd:1b": True}
    r = await client.get("/api/modules/models/downloaded")
    # workspace_coder is enabled but has no models for cpu_small → skipped
    assert r.json() == {"image_generation": [{"id": "sd:1b", "downloaded": True}]}


@pytest.mark.asyncio
async def test_models_downloaded_skips_disabled_and_unavailable(client, env):
    env.enabled = {"assistant"}  # nothing optional enabled
    r = await client.get("/api/modules/models/downloaded")
    assert r.json() == {}
