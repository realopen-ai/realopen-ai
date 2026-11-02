"""Saved provider choices drive only allowlisted, owned local containers."""

import asyncio
import copy
import importlib.util
import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import httpx
import pytest
import yaml

from app.agent.tools import config_store
from app.agent.tools.web_search import WEB_SEARCH_CONFIG
from app.services import search_services

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location(
    "search_runtime", ROOT / "scripts/search_runtime.py"
)
host = importlib.util.module_from_spec(spec)
spec.loader.exec_module(host)


@pytest.fixture
def preferences(monkeypatch):
    config = WEB_SEARCH_CONFIG.default_config()
    monkeypatch.setattr(config_store, "get_tool_config", lambda _: config)
    monkeypatch.setattr(search_services, "_status", {})
    monkeypatch.setattr(search_services, "_wake", None)
    return config


def container(provider, root=ROOT, running=True, policy="no", health=None):
    state = {"Running": running, "Status": "running" if running else "exited"}
    if health:
        state["Health"] = {"Status": health}
    return {
        "Config": {
            "Labels": {
                "com.docker.compose.service": provider,
                "com.docker.compose.project.working_dir": str(root),
            }
        },
        "HostConfig": {"RestartPolicy": {"Name": policy}},
        "State": state,
    }


def fake_runtime(monkeypatch, containers):
    runtime = host.SearchRuntime(ROOT)
    calls = []

    def run(args, timeout=90):
        calls.append(args)
        if args[:2] == ["container", "inspect"]:
            provider = args[-1].removeprefix("realopen-")
            if provider not in containers:
                raise subprocess.CalledProcessError(1, args, stderr="No such container")
            return SimpleNamespace(stdout=json.dumps([containers[provider]]))
        if args[0] == "compose":
            containers[args[-1]] = container(args[-1])
        if args[0] == "stop":
            containers[args[-1].removeprefix("realopen-")]["State"]["Running"] = False
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(runtime, "_run", run)
    return runtime, calls


def test_enabled_service_starts_but_disabled_is_not_created(monkeypatch):
    runtime, calls = fake_runtime(monkeypatch, {})
    states = runtime.reconcile({"searxng": True, "degoog": False})
    assert states == {"searxng": {"status": "ready"}, "degoog": {"status": "stopped"}}
    up = next(c for c in calls if c[0] == "compose")
    assert up[-1] == "searxng" and up[up.index("--pull") + 1] == "never"
    assert "--no-deps" in up and "--no-build" in up
    assert not any(c[0] == "pull" for c in calls)


def test_running_services_are_idempotent_and_health_is_reported(monkeypatch):
    runtime, calls = fake_runtime(
        monkeypatch,
        {
            "searxng": container("searxng"),
            "degoog": container("degoog", health="starting"),
        },
    )
    states = runtime.reconcile({"searxng": True, "degoog": True})
    assert states["degoog"]["status"] == "starting"
    assert all(c[0] == "container" for c in calls)


def test_disabling_stops_owned_service_and_removes_legacy_restart_policy(monkeypatch):
    runtime, calls = fake_runtime(
        monkeypatch, {"degoog": container("degoog", policy="unless-stopped")}
    )
    assert (
        runtime.reconcile({"searxng": False, "degoog": False})["degoog"]["status"]
        == "stopped"
    )
    assert ["update", "--restart=no", "realopen-degoog"] in calls
    assert ["stop", "--time", "10", "realopen-degoog"] in calls
    assert not any(c[0] in {"rm", "volume"} for c in calls)


@pytest.mark.parametrize(
    "desired",
    [
        {"other": True},
        {"searxng": "yes", "degoog": False},
        {"searxng": True, "degoog": False, "postgres": False},
    ],
)
def test_host_rejects_arbitrary_services(desired):
    with pytest.raises(ValueError):
        host.SearchRuntime().reconcile(desired)


def test_foreign_containers_are_never_stopped(monkeypatch):
    runtime, calls = fake_runtime(
        monkeypatch, {"searxng": container("searxng", root=Path("/another/project"))}
    )
    assert (
        runtime.reconcile({"searxng": False, "degoog": False})["searxng"]["status"]
        == "failed"
    )
    assert not any(c[0] in {"stop", "update", "compose"} for c in calls)


def test_docker_failures_do_not_leak_command_output(monkeypatch):
    runtime = host.SearchRuntime()

    def fail(*args, **kwargs):
        raise subprocess.CalledProcessError(1, ["docker"], stderr="password=secret")

    monkeypatch.setattr(runtime, "_run", fail)
    states = runtime.reconcile({"searxng": True, "degoog": False})
    assert states["searxng"]["status"] == "failed"
    assert "secret" not in json.dumps(states)


def test_prepare_pulls_images_without_starting_containers(monkeypatch):
    runtime = host.SearchRuntime()
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        if args[:2] == ["image", "inspect"]:
            raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(runtime, "_run", run)
    runtime.prepare()
    assert len([c for c in calls if c[0] == "pull"]) == 2
    assert not any(c[0] in {"compose", "run", "start"} for c in calls)


def test_preferences_and_external_instances(preferences):
    assert search_services.desired_services()["searxng"] == {
        "enabled": True,
        "managed": True,
    }
    preferences["custom"]["providers"]["searxng"]["enabled"] = False
    preferences["custom"]["providers"]["degoog"].update(
        enabled=True, base_url="https://search.example.org"
    )
    assert search_services.desired_services()["degoog"] == {
        "enabled": True,
        "managed": False,
    }
    assert search_services.status()["degoog"]["status"] == "external"
    preferences["enabled"] = False
    assert not any(s["enabled"] for s in search_services.desired_services().values())


@pytest.mark.asyncio
async def test_unreachable_runtime_retains_preferences_and_recovers(
    preferences, monkeypatch
):
    original = httpx.AsyncClient
    requests = []
    failing = True

    def respond(request):
        requests.append(request)
        if failing:
            raise httpx.ConnectError("unreachable")
        return httpx.Response(
            200, json={"searxng": {"status": "ready"}, "degoog": {"status": "stopped"}}
        )

    monkeypatch.setattr(
        search_services.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(respond), **kw),
    )
    before = copy.deepcopy(preferences)
    await search_services.reconcile_once()
    assert preferences == before
    assert search_services.status()["searxng"]["status"] == "failed"
    failing = False
    await search_services.reconcile_once()
    assert search_services.status()["searxng"]["status"] == "ready"
    assert requests[-1].url.path == "/healthz"


@pytest.mark.asyncio
async def test_monitor_reconciles_latest_toggle_without_reloading_preferences(
    preferences, monkeypatch
):
    desired = []
    changed = asyncio.Event()
    loads = []

    async def load(**kwargs):
        loads.append(kwargs)

    async def reconcile():
        desired.append(search_services.desired_services())
        changed.set()

    monkeypatch.setattr(config_store, "seed_and_load", load)
    monkeypatch.setattr(search_services, "reconcile_once", reconcile)
    task = asyncio.create_task(search_services.monitor())
    try:
        await asyncio.wait_for(changed.wait(), 1)
        changed.clear()
        preferences["custom"]["providers"]["searxng"]["enabled"] = False
        preferences["custom"]["providers"]["degoog"]["enabled"] = True
        search_services.request_reconcile()
        await asyncio.wait_for(changed.wait(), 1)
        assert (
            desired[-1]["degoog"]["enabled"] and not desired[-1]["searxng"]["enabled"]
        )
        assert loads == [{"require_database": True}]
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert search_services._wake is None


def test_compose_never_autostarts_search_services():
    compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text())["services"]
    for name in ["searxng", "degoog"]:
        assert "search" in compose[name]["profiles"]
        assert compose[name]["restart"] == "no"
    assert "docker.sock" not in json.dumps(compose["backend"])


@pytest.mark.asyncio
async def test_monitor_never_acts_on_fallback_defaults(preferences, monkeypatch):
    attempted = asyncio.Event()

    async def unavailable(**kwargs):
        assert kwargs["require_database"]
        attempted.set()
        raise RuntimeError("DB offline")

    async def must_not_act():
        pytest.fail("Fallback defaults must not change containers")

    monkeypatch.setattr(config_store, "seed_and_load", unavailable)
    monkeypatch.setattr(search_services, "reconcile_once", must_not_act)
    task = asyncio.create_task(search_services.monitor())
    try:
        await asyncio.wait_for(attempted.wait(), 1)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
