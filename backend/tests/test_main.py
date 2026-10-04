"""
Tests for the FastAPI application factory (app/main.py).

Scope:
- _get_data_dir(): happy path, fallback when /app/data is unwritable, and
  the OSError propagation when every candidate fails.
- _detect_setup_mode(): SETUP_MODE env override, marker-file present,
  marker-file missing.
- _apply_module_config(): unregisters tools of disabled modules, no-op
  when everything is enabled, debug-logging branch.
- _seed_default_templates(): direct tests with the session factory mocked —
  early return when the table already has rows, skip-when-pptx-missing,
  seeding of all defaults when files exist, per-slug file filtering, and
  thumbnail-generation failure tolerance.
- lifespan(): startup/shutdown with EVERY external dependency mocked —
  alembic migrations, template seeding, tool-config seeding, dependency
  verification — plus the setup-mode skip, the all-failures-survived path,
  and the debug-logging branch.
- idle sandbox monitor: driven through lifespan with ONLY the 30 s tick
  sleep replaced — verifies idle+taskless sandboxes are stopped while busy
  or fresh ones are kept, and that DB errors are swallowed (loop survives).
- App wiring: FastAPI config (title/docs/openapi), CORS middleware kwargs,
  DebugLoggingMiddleware presence, routers mounted under /api, /metrics
  mount, WebSocket routes, and live GET /api/health + /metrics/ + CORS
  preflight through the real app object.

Mocking: app.main's import-time `pip_persistence.activate_persistent_
site_packages()` is redirected to a tmp dir via the module's test hook
BEFORE the import. No DB, Redis, Ollama, Docker, or network access ever
happens: every lifespan dependency is monkeypatched, and requests are
driven in-process via httpx.ASGITransport (which does not run lifespan).
"""

import asyncio
import importlib
import logging
import sys
import uuid
from pathlib import Path, PosixPath
from types import SimpleNamespace
from unittest import mock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import settings
from app.core.middleware import DebugLoggingMiddleware


@pytest.fixture(scope="module")
def main(tmp_path_factory):
    """Import app.main with pip_persistence pinned to a temp dir.

    The activation runs at import time and would otherwise create (and
    sys.path-register) a persistent site-packages target under the repo's
    data/ directory. The test hook keeps the side effect inside tmp.
    """
    import app.services.pip_persistence as pip_persistence

    persist_root = tmp_path_factory.mktemp("pip_persistence")
    site_dir = persist_root / "site"
    pip_persistence.set_dirs_for_testing(
        persist_root / "wheels", persist_root / "cache", site_dir
    )
    already_imported = "app.main" in sys.modules
    try:
        module = importlib.import_module("app.main")
        if not already_imported:
            # activate_persistent_site_packages() registered our tmp target
            assert str(site_dir) in sys.path
    finally:
        pip_persistence.set_dirs_for_testing(None, None, None)
    return module


# ── _get_data_dir ──────────────────────────────────────────────────────


def test_get_data_dir_returns_existing_directory(main):
    data_dir = main._get_data_dir()
    assert data_dir.is_dir()
    assert data_dir.exists()


def test_get_data_dir_falls_back_when_app_data_unwritable(main, monkeypatch):
    """When /app/data cannot be created, the repo-level data/ dir is used."""

    class _NoAppDataPath(PosixPath):
        def mkdir(self, *args, **kwargs):
            if str(self) == "/app/data":
                raise OSError("simulated: /app/data not writable")
            return super().mkdir(*args, **kwargs)

    monkeypatch.setattr(main, "Path", _NoAppDataPath)
    result = main._get_data_dir()

    expected = Path(main.__file__).parent.parent.parent / "data"
    assert str(result) == str(expected)
    assert result.is_dir()


def test_get_data_dir_raises_when_all_candidates_fail(main, monkeypatch):
    """If no candidate (nor the fallback) can be created, OSError escapes."""

    class _FailingPath(PosixPath):
        def mkdir(self, *args, **kwargs):
            raise OSError("simulated: everything is read-only")

    monkeypatch.setattr(main, "Path", _FailingPath)
    with pytest.raises(OSError, match="read-only"):
        main._get_data_dir()


# ── _detect_setup_mode ─────────────────────────────────────────────────


def test_detect_setup_mode_env_var_forces_setup(main, monkeypatch, tmp_path):
    monkeypatch.setattr(main.settings, "SETUP_MODE", True)
    # Marker file exists — but the explicit env override wins
    (tmp_path / ".setup-complete").touch()
    monkeypatch.setattr(main, "_get_data_dir", lambda: tmp_path)
    assert main._detect_setup_mode() is True


def test_detect_setup_mode_marker_present_means_setup_complete(
    main, monkeypatch, tmp_path
):
    monkeypatch.setattr(main.settings, "SETUP_MODE", False)
    (tmp_path / ".setup-complete").touch()
    monkeypatch.setattr(main, "_get_data_dir", lambda: tmp_path)
    assert main._detect_setup_mode() is False


def test_detect_setup_mode_missing_marker_enters_setup(main, monkeypatch, tmp_path):
    monkeypatch.setattr(main.settings, "SETUP_MODE", False)
    # tmp_path exists but has no .setup-complete marker
    monkeypatch.setattr(main, "_get_data_dir", lambda: tmp_path)
    assert main._detect_setup_mode() is True


# ── _apply_module_config ───────────────────────────────────────────────


class _FakeRegistry:
    def __init__(self, tools):
        self._tools = set(tools)
        self.unregister_calls = []

    def unregister_many(self, names):
        removed = [n for n in names if n in self._tools]
        self._tools -= set(removed)
        self.unregister_calls.append(list(names))
        return len(removed)

    def has_tool(self, name):
        return name in self._tools


def _patch_module_env(monkeypatch, main, modules, enabled_names):
    stub = SimpleNamespace(
        get_modules=lambda: modules,
        is_module_enabled=lambda name: name in enabled_names,
    )
    monkeypatch.setattr(main, "settings", stub)


def test_apply_module_config_unregisters_disabled_module_tools(main, monkeypatch):
    registry = _FakeRegistry({"use_websearch", "use_vision", "use_image_gen"})
    monkeypatch.setattr("app.agent.base.get_tool_registry", lambda: registry)
    _patch_module_env(
        monkeypatch,
        main,
        modules={
            "assistant": SimpleNamespace(tools=["use_websearch", "use_vision"]),
            "image_generation": SimpleNamespace(tools=["use_image_gen"]),
        },
        enabled_names={"assistant"},
    )

    main._apply_module_config()

    # Only the disabled module's tools were unregistered
    assert registry.unregister_calls == [["use_image_gen"]]
    assert not registry.has_tool("use_image_gen")
    assert registry.has_tool("use_websearch")
    assert registry.has_tool("use_vision")


def test_apply_module_config_noop_when_all_modules_enabled(main, monkeypatch):
    registry = _FakeRegistry({"use_websearch", "use_image_gen"})
    monkeypatch.setattr("app.agent.base.get_tool_registry", lambda: registry)
    _patch_module_env(
        monkeypatch,
        main,
        modules={
            "assistant": SimpleNamespace(tools=["use_websearch"]),
            "image_generation": SimpleNamespace(tools=["use_image_gen"]),
        },
        enabled_names={"assistant", "image_generation"},
    )

    main._apply_module_config()

    assert registry.unregister_calls == []
    assert registry.has_tool("use_websearch")
    assert registry.has_tool("use_image_gen")


def test_apply_module_config_unknown_tool_names_ignored(main, monkeypatch):
    """unregister_many tolerates tool names the registry never saw."""
    registry = _FakeRegistry({"use_websearch"})
    monkeypatch.setattr("app.agent.base.get_tool_registry", lambda: registry)
    _patch_module_env(
        monkeypatch,
        main,
        modules={"ghost": SimpleNamespace(tools=["nonexistent_tool"])},
        enabled_names=set(),
    )

    main._apply_module_config()

    assert registry.unregister_calls == [["nonexistent_tool"]]
    assert registry.has_tool("use_websearch")


def test_apply_module_config_debug_logging_branch(main, monkeypatch, caplog):
    registry = _FakeRegistry({"use_websearch", "use_image_gen"})
    monkeypatch.setattr("app.agent.base.get_tool_registry", lambda: registry)
    _patch_module_env(
        monkeypatch,
        main,
        modules={
            "assistant": SimpleNamespace(tools=["use_websearch"]),
            "image_generation": SimpleNamespace(tools=["use_image_gen"]),
        },
        enabled_names={"assistant"},
    )
    monkeypatch.setattr(main, "is_debug", lambda: True)

    with caplog.at_level(logging.DEBUG, logger="app.main"):
        main._apply_module_config()

    messages = [r.getMessage() for r in caplog.records if r.name == "app.main"]
    assert any("Module config: unregistered 1 tools" in m for m in messages)
    assert any("use_image_gen" in m for m in messages)
    assert any("active tools" in m for m in messages)
    assert any("use_websearch" in m for m in messages)


# ── _seed_default_templates (session factory fully mocked) ─────────────


class _FakeTemplateResult:
    """Stand-in for the (result of) ``select(Template).limit(1)`` query."""

    def __init__(self, existing):
        self._existing = existing

    def scalar_one_or_none(self):
        return self._existing


class _FakeSeedingSession:
    """Async session double for _seed_default_templates — no real DB."""

    def __init__(self, existing_template=None):
        self.existing = existing_template
        self.execute_calls = 0
        self.added = []
        self.commit_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query):
        self.execute_calls += 1
        return _FakeTemplateResult(self.existing)

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commit_calls += 1


def _pretend_pptx_files_exist(monkeypatch, only_slugs=None):
    """Make ``<templates>/pptx/<slug>.pptx`` paths report existing on disk.

    Only the .pptx lookups are faked, independently of bundled templates;
    every other ``Path.exists`` call delegates to the real implementation.
    """

    real_exists = Path.exists

    def fake_exists(self, *args, **kwargs):
        if str(self).endswith(".pptx"):
            if only_slugs is None:
                return True
            return any(slug in str(self) for slug in only_slugs)
        return real_exists(self, *args, **kwargs)

    monkeypatch.setattr(Path, "exists", fake_exists)


@pytest.mark.asyncio
async def test_seed_default_templates_noop_when_table_already_has_rows(main, monkeypatch):
    """One existing row is enough: no Template objects added, no commit."""
    fake = _FakeSeedingSession(existing_template=object())
    monkeypatch.setattr("app.db.session.async_session_factory", lambda: fake)

    await main._seed_default_templates()

    assert fake.execute_calls == 1  # only the emptiness check ran
    assert fake.added == []
    assert fake.commit_calls == 0


@pytest.mark.asyncio
async def test_seed_default_templates_skips_missing_pptx_files(main, monkeypatch):
    """Empty table but no .pptx files on disk: nothing seeded, yet the
    transaction is still committed (the existence check opened one)."""
    fake = _FakeSeedingSession(existing_template=None)
    monkeypatch.setattr("app.db.session.async_session_factory", lambda: fake)

    _pretend_pptx_files_exist(monkeypatch, only_slugs=[])

    await main._seed_default_templates()

    assert fake.added == []
    assert fake.commit_calls == 1


@pytest.mark.asyncio
async def test_seed_default_templates_seeds_all_defaults_when_files_exist(
    main, monkeypatch, caplog
):
    fake = _FakeSeedingSession(existing_template=None)
    monkeypatch.setattr("app.db.session.async_session_factory", lambda: fake)
    _pretend_pptx_files_exist(monkeypatch)

    thumb = mock.MagicMock(return_value="thumb-b64")
    monkeypatch.setattr("app.services.thumbnail_gen.generate_schematic_thumbnail", thumb)

    with caplog.at_level(logging.INFO, logger="app.main"):
        await main._seed_default_templates()

    assert [t.slug for t in fake.added] == ["corporate", "modern", "elegant"]
    for t in fake.added:
        assert t.path == f"{t.slug}.pptx"
        assert t.thumbnail == "thumb-b64"
        assert isinstance(t.id, uuid.UUID)
        assert t.created_at is not None and t.updated_at is not None
    assert {t.display_name for t in fake.added} == {"Corporate", "Modern", "Elegant"}
    assert {tuple(t.tags) for t in fake.added} >= {("corporate", "professional", "navy")}

    assert thumb.call_count == 3
    assert {c.args[1] for c in thumb.call_args_list} == {"Corporate", "Modern", "Elegant"}
    assert all(str(c.args[0]).endswith(".pptx") for c in thumb.call_args_list)

    assert fake.commit_calls == 1
    messages = [r.getMessage() for r in caplog.records if r.name == "app.main"]
    assert sum("Seeded template" in m for m in messages) == 3


@pytest.mark.asyncio
async def test_seed_default_templates_seeds_only_slugs_with_files(main, monkeypatch):
    """Only the slugs whose .pptx file exists get a row."""
    fake = _FakeSeedingSession(existing_template=None)
    monkeypatch.setattr("app.db.session.async_session_factory", lambda: fake)
    _pretend_pptx_files_exist(monkeypatch, only_slugs={"modern"})
    monkeypatch.setattr(
        "app.services.thumbnail_gen.generate_schematic_thumbnail",
        mock.MagicMock(return_value="t"),
    )

    await main._seed_default_templates()

    assert [t.slug for t in fake.added] == ["modern"]
    assert fake.commit_calls == 1


@pytest.mark.asyncio
async def test_seed_default_templates_thumbnail_failure_still_seeds(
    main, monkeypatch, caplog
):
    """A crashing thumbnail generator must not abort seeding (warning only)."""
    fake = _FakeSeedingSession(existing_template=None)
    monkeypatch.setattr("app.db.session.async_session_factory", lambda: fake)
    _pretend_pptx_files_exist(monkeypatch)

    def broken_thumb(pptx_path, display_name):
        raise RuntimeError("thumbnail blew up")

    monkeypatch.setattr(
        "app.services.thumbnail_gen.generate_schematic_thumbnail", broken_thumb
    )

    with caplog.at_level(logging.WARNING, logger="app.main"):
        await main._seed_default_templates()

    assert len(fake.added) == 3
    assert all(t.thumbnail is None for t in fake.added)
    assert fake.commit_calls == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert sum("Thumbnail generation failed" in m for m in warnings) == 3


# ── lifespan (all external dependencies mocked) ────────────────────────


@pytest.fixture
def mocked_lifespan_deps(main, monkeypatch):
    """Mock every lifespan dependency that touches DB, apt, or the tool store."""
    upgrade = mock.Mock()
    monkeypatch.setattr("alembic.command.upgrade", upgrade)
    seed_templates = mock.AsyncMock()
    monkeypatch.setattr(main, "_seed_default_templates", seed_templates)
    seed_and_load = mock.AsyncMock(return_value={"seeded": 2, "loaded": 5})
    monkeypatch.setattr("app.agent.tools.config_store.seed_and_load", seed_and_load)
    verify_deps = mock.AsyncMock()
    monkeypatch.setattr("app.services.deps_manager.verify_deps_on_startup", verify_deps)
    apply_config = mock.Mock()
    monkeypatch.setattr(main, "_apply_module_config", apply_config)
    return SimpleNamespace(
        upgrade=upgrade,
        seed_templates=seed_templates,
        seed_and_load=seed_and_load,
        verify_deps=verify_deps,
        apply_config=apply_config,
    )


@pytest.mark.asyncio
async def test_lifespan_normal_mode_runs_all_startup_steps(main, monkeypatch, mocked_lifespan_deps):
    monkeypatch.setattr(main, "_detect_setup_mode", lambda: False)

    async with main.lifespan(main.app):
        pass

    deps = mocked_lifespan_deps
    deps.apply_config.assert_called_once()

    deps.upgrade.assert_called_once()
    cfg, revision = deps.upgrade.call_args.args
    assert revision == "head"
    assert cfg.get_main_option("sqlalchemy.url") == settings.DATABASE_URL
    assert cfg.get_main_option("script_location").endswith("alembic")

    deps.seed_templates.assert_awaited_once()
    deps.seed_and_load.assert_awaited_once()
    deps.verify_deps.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_setup_mode_skips_module_config(main, monkeypatch, mocked_lifespan_deps):
    monkeypatch.setattr(main, "_detect_setup_mode", lambda: True)

    async with main.lifespan(main.app):
        pass

    mocked_lifespan_deps.apply_config.assert_not_called()
    # Everything else still runs in setup mode
    mocked_lifespan_deps.upgrade.assert_called_once()
    mocked_lifespan_deps.seed_templates.assert_awaited_once()
    mocked_lifespan_deps.seed_and_load.assert_awaited_once()
    mocked_lifespan_deps.verify_deps.assert_awaited_once()


@pytest.mark.asyncio
async def test_lifespan_survives_all_startup_failures(main, monkeypatch, mocked_lifespan_deps):
    """Every startup step is wrapped in try/except — the app must still boot."""
    monkeypatch.setattr(main, "_detect_setup_mode", lambda: False)
    mocked_lifespan_deps.upgrade.side_effect = RuntimeError("db unreachable")
    mocked_lifespan_deps.seed_templates.side_effect = RuntimeError("seed failed")
    mocked_lifespan_deps.seed_and_load.side_effect = RuntimeError("config store down")
    mocked_lifespan_deps.verify_deps.side_effect = RuntimeError("apt broken")

    # Must not raise — and must shut the sandbox monitor down cleanly
    async with main.lifespan(main.app):
        pass

    mocked_lifespan_deps.apply_config.assert_called_once()


@pytest.mark.asyncio
async def test_lifespan_debug_branch_logs_details(main, monkeypatch, mocked_lifespan_deps, caplog):
    monkeypatch.setattr(main, "_detect_setup_mode", lambda: False)
    monkeypatch.setattr(main, "is_debug", lambda: True)

    with caplog.at_level(logging.DEBUG, logger="app.main"):
        async with main.lifespan(main.app):
            pass

    messages = [r.getMessage() for r in caplog.records if r.name == "app.main"]
    assert any("DEBUG mode is ON" in m for m in messages)
    assert any("Tools registered (before module filter)" in m for m in messages)
    assert any("Tools active (after module filter)" in m for m in messages)
    assert any("Alembic migrations applied successfully" in m for m in messages)
    assert any("Tool configurations: 2 seeded, 5 loaded" in m for m in messages)


@pytest.mark.asyncio
async def test_lifespan_shutdown_cancels_sandbox_monitor(main, monkeypatch, mocked_lifespan_deps):
    """The monitor task must not outlive the lifespan (loop stays clean)."""
    monkeypatch.setattr(main, "_detect_setup_mode", lambda: False)

    async with main.lifespan(main.app):
        # While the app is "up" the monitor task exists and is sleeping
        pending = [t for t in asyncio.all_tasks() if not t.done()]
        assert all(not t.cancelled() for t in pending)

    # After shutdown no monitor coroutine is left running on this loop
    leftover = [
        t
        for t in asyncio.all_tasks()
        if not t.done() and "idle_sandbox_monitor" in str(t.get_coro())
    ]
    assert leftover == []


# ── idle sandbox monitor (exercised through lifespan) ──────────────────


class _MonitorFakeResult:
    """Chainable stand-in for ``(await db.execute(...)).scalars().all()``."""

    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _MonitorFakeSession:
    """Async session double for the idle sandbox monitor — no real DB."""

    def __init__(self, rows, scalar_answers):
        self.rows = rows
        self.scalar_answers = list(scalar_answers)
        self.commit_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, query):
        return _MonitorFakeResult(self.rows)

    async def scalar(self, query):
        return self.scalar_answers.pop(0)

    async def commit(self):
        self.commit_calls += 1


@pytest.mark.asyncio
async def test_lifespan_idle_sandbox_monitor_stops_only_idle_sandboxes(
    main, monkeypatch, mocked_lifespan_deps
):
    """One monitor tick: a sandbox past its idle timeout with no queued or
    running tasks is stopped; a busy one and a recently-active one are kept."""
    from datetime import datetime, timedelta

    monkeypatch.setattr(main, "_detect_setup_mode", lambda: False)

    now = datetime.utcnow()
    idle_sandbox = SimpleNamespace(
        id="idle-1",
        last_active_at=now - timedelta(hours=2),
        idle_timeout_seconds=600,
        status="running",
        desired_running=True,
    )
    busy_sandbox = SimpleNamespace(
        id="busy-1",
        last_active_at=now - timedelta(hours=2),
        idle_timeout_seconds=600,
        status="running",
        desired_running=True,
    )
    fresh_sandbox = SimpleNamespace(
        id="fresh-1",
        last_active_at=now,
        idle_timeout_seconds=600,
        status="running",
        desired_running=True,
    )
    fake_session = _MonitorFakeSession(
        rows=[idle_sandbox, busy_sandbox, fresh_sandbox],
        # db.scalar() answers in row order: no task, active task, no task
        scalar_answers=[None, "task-9", None],
    )
    monkeypatch.setattr("app.db.session.async_session_factory", lambda: fake_session)

    stop_calls = mock.AsyncMock()
    monkeypatch.setattr("app.services.sandbox_host.call", stop_calls)

    # The monitor sleeps 30 s between ticks. Replace ONLY that sleep: the
    # first tick returns immediately (one DB pass), the second cancels the
    # monitor so the loop ends deterministically inside the test.
    real_sleep = asyncio.sleep
    monitor_ticks = 0

    async def fake_sleep(delay, *args, **kwargs):
        nonlocal monitor_ticks
        if delay == 30:
            monitor_ticks += 1
            if monitor_ticks >= 2:
                raise asyncio.CancelledError()
            return None
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    async with main.lifespan(main.app):
        # Give the monitor enough event-loop turns to finish its single pass
        for _ in range(20):
            await real_sleep(0)

    assert monitor_ticks == 2  # exactly one completed DB pass, then cancel
    assert fake_session.commit_calls == 1
    # Only the idle sandbox was reaped
    assert idle_sandbox.status == "stopped"
    assert busy_sandbox.status == "running"
    assert fresh_sandbox.status == "running"
    stop_calls.assert_awaited_once()
    assert stop_calls.await_args.args == ("stop", idle_sandbox)


@pytest.mark.asyncio
async def test_lifespan_idle_sandbox_monitor_survives_db_errors(
    main, monkeypatch, mocked_lifespan_deps, caplog
):
    """A failing DB round is swallowed (debug log) and the loop keeps going."""
    monkeypatch.setattr(main, "_detect_setup_mode", lambda: False)

    class _ExplodingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def execute(self, query):
            raise RuntimeError("monitor db down")

    monkeypatch.setattr("app.db.session.async_session_factory", _ExplodingSession)

    real_sleep = asyncio.sleep
    monitor_ticks = 0

    async def fake_sleep(delay, *args, **kwargs):
        nonlocal monitor_ticks
        if delay == 30:
            monitor_ticks += 1
            if monitor_ticks >= 3:  # let TWO failing rounds happen
                raise asyncio.CancelledError()
            return None
        return await real_sleep(delay, *args, **kwargs)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)

    with caplog.at_level(logging.DEBUG, logger="app.main"):
        async with main.lifespan(main.app):
            for _ in range(20):
                await real_sleep(0)

    assert monitor_ticks == 3  # two DB rounds ran despite the failures
    messages = [r.getMessage() for r in caplog.records if r.name == "app.main"]
    assert sum("Sandbox idle monitor" in m and "monitor db down" in m for m in messages) >= 2


# ── App wiring ─────────────────────────────────────────────────────────


def test_app_object_configuration(main):
    app = main.app
    assert isinstance(app, FastAPI)
    assert app.title == settings.APP_NAME == "RealOpen-AI"
    assert app.version == "0.1.0"
    assert app.docs_url == "/api/docs"
    assert app.redoc_url == "/api/redoc"
    assert app.openapi_url == "/api/openapi.json"


def test_cors_middleware_configured(main):
    cors = next(
        m for m in main.app.user_middleware if m.cls is CORSMiddleware
    )
    assert cors.kwargs["allow_origins"] == settings.CORS_ORIGINS
    assert cors.kwargs["allow_credentials"] is True
    assert cors.kwargs["allow_methods"] == ["*"]
    assert cors.kwargs["allow_headers"] == ["*"]


def test_debug_logging_middleware_added(main):
    assert any(m.cls is DebugLoggingMiddleware for m in main.app.user_middleware)


def test_all_routers_mounted_under_api_prefix(main):
    paths = set(main.app.openapi()["paths"])
    expected = {
        "/api/health",
        "/api/chat",
        "/api/chat/stream",
        "/api/conversations",
        "/api/models",
        "/api/modules",
        "/api/modules/toggle",
        "/api/setup/status",
        "/api/setup/complete",
        "/api/memory",
        "/api/memory/search",
        "/api/documents",
        "/api/documents/upload",
        "/api/reports/{report_id}/download",
        "/api/workspace/templates",
        "/api/deps",
        "/api/providers",
        "/api/tools",
        "/api/skills",
        "/api/voice/settings",
        "/api/sandboxes",
        "/api/profiles",
    }
    missing = expected - paths
    assert not missing, f"missing mounted routes: {missing}"
    assert len(paths) >= 80  # all routers contributed their endpoints


def test_metrics_mounted(main):
    mounts = {
        getattr(r, "path", None)
        for r in main.app.routes
        if type(r).__name__ == "Mount"
    }
    assert "/metrics" in mounts


def test_websocket_routes_registered(main):
    paths = {getattr(r, "path", None) for r in main.app.routes}
    assert "/api/sandboxes/{sandbox_id}/terminal" in paths
    # /ws/voice is registered iff the (optional) voice manager imported
    if main.voice_manager is not None:
        assert "/ws/voice" in paths
    else:
        assert "/ws/voice" not in paths


def test_lifespan_attached_to_app(main):
    # FastAPI stores the user lifespan on the router's lifespan_context
    assert main.app.router.lifespan_context is not None


# ── Live requests through the real app (no lifespan: ASGITransport) ────


@pytest.mark.asyncio
async def test_real_app_serves_health(main):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json() == {
        "status": "healthy",
        "service": "RealOpen-AI",
        "version": "0.1.0",
    }


@pytest.mark.asyncio
async def test_real_app_serves_openapi(main):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/api/openapi.json")
    assert resp.status_code == 200
    spec = resp.json()
    assert "/api/health" in spec["paths"]
    assert spec["info"]["title"] == "RealOpen-AI"


@pytest.mark.asyncio
async def test_real_app_serves_docs(main):
    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/api/docs")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


@pytest.mark.asyncio
async def test_real_app_metrics_endpoint(main):
    from app.core import metrics as app_metrics

    app_metrics.record_chat_tokens("pytest-main-mount", 1)

    transport = httpx.ASGITransport(app=main.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        bare = await client.get("/metrics")
        slashed = await client.get("/metrics/")

    # The ASGI metrics app redirects bare /metrics to /metrics/
    assert bare.status_code == 307
    assert bare.headers["location"].endswith("/metrics/")

    assert slashed.status_code == 200
    assert "text/plain" in slashed.headers["content-type"]
    content = slashed.content
    assert b"realopen_chat_tokens_total" in content
    assert b"pytest-main-mount" in content


@pytest.mark.asyncio
async def test_real_app_cors_preflight(main):
    transport = httpx.ASGITransport(app=main.app)
    origin = settings.CORS_ORIGINS[0]
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.options(
            "/api/health",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "GET",
            },
        )
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin
    assert "GET" in resp.headers.get("access-control-allow-methods", "")


@pytest.mark.asyncio
async def test_real_app_cors_headers_on_simple_request(main):
    transport = httpx.ASGITransport(app=main.app)
    origin = settings.CORS_ORIGINS[0]
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        resp = await client.get("/api/health", headers={"Origin": origin})
    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == origin
