"""Native startup safety without installing software or touching real app data."""

import hashlib
import io
import importlib
import importlib.util
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tarfile
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
from native.config import Config, DEFAULTS, atomic_json, dotenv
from native import migration
from native import dependencies
from native.processes import Processes, RuntimeLock, wait_http
from native.services import NativeSearchRuntime, Services
from app.api.frontend import install_frontend
from app.services import search_services
from app.config import settings


@pytest.mark.parametrize("key", DEFAULTS)
def test_default_docker_endpoints_become_managed_local_endpoints(tmp_path, key):
    cfg = Config(tmp_path, {key: DEFAULTS[key]})
    assert cfg.managed[key]
    assert "127.0.0.1" in cfg.env[key]
    assert cfg.data == tmp_path / "data"
    assert (
        Config(tmp_path, {}).env["DATABASE_URL"]
        == Config(tmp_path, {}).env["DATABASE_URL"]
    )


@pytest.mark.parametrize("key", DEFAULTS)
def test_custom_endpoints_are_external_and_preserved(tmp_path, key):
    custom = (
        "postgresql+asyncpg://user:secret@server:5432/db"
        if key == "DATABASE_URL"
        else "http://server:9000"
    )
    cfg = Config(tmp_path, {key: custom})
    assert not cfg.managed[key]
    assert cfg.env[key] == custom


def test_config_precedence_and_path_translation(tmp_path):
    (tmp_path / ".env").write_text("NATIVE_API_PORT=8001\nHARDWARE_PROFILE=cpu_small\n")
    (tmp_path / ".env.native").write_text(
        "NATIVE_API_PORT=8002\nAGENT_TRACE_DIR=/app/data/agent_traces\nOTHER=/app/database\n"
    )
    cfg = Config(tmp_path, {"NATIVE_API_PORT": "8003"})
    assert cfg.env["VITE_PROXY_TARGET"] == "http://127.0.0.1:8003"
    assert cfg.env["HARDWARE_PROFILE"] == "cpu_small"
    assert cfg.env["AGENT_TRACE_DIR"] == str(tmp_path / "data/agent_traces")
    assert cfg.env["OTHER"] == "/app/database"
    assert cfg.env["REALOPEN_STATE_DIR"] == str(tmp_path / "data/state")


@pytest.mark.parametrize(
    "module,function,suffix",
    [
        ("app.services.rag", "get_data_dir", ""),
        ("app.api.workspace", "_get_data_dir", ""),
        ("app.api.reports", "_get_data_dir", ""),
        ("app.api.setup", "_get_data_dir", ""),
        ("app.services.voice_settings", "_path", "voice-settings.json"),
        ("app.api.workspace", "_get_templates_dir", "templates/pptx/custom"),
        ("app.services.pip_persistence", "_data_dir", ""),
        ("app.services.voice_model_installer", "get_data_dir", ""),
        ("app.voice.hf_cache", "_data_dir", ""),
        ("app.voice.models_store", "_default_models_dir", "models/voice"),
    ],
)
def test_native_services_honor_central_data_directory(
    tmp_path, monkeypatch, module, function, suffix
):
    directory = tmp_path / "central-data"
    monkeypatch.setattr(settings, "REALOPEN_DATA_DIR", str(directory))
    assert getattr(importlib.import_module(module), function)() == directory / suffix


def test_backend_native_settings_are_loaded_and_parsed_centrally(tmp_path, monkeypatch):
    monkeypatch.setenv("REALOPEN_DATA_DIR", str(tmp_path / "central"))
    monkeypatch.setenv("NATIVE_MANAGED_SEARXNG", "1")
    monkeypatch.setenv("NATIVE_MANAGED_DEGOOG", "0")
    configured = type(settings)(_env_file=None)
    assert configured.get_data_dir() == tmp_path / "central"
    assert configured.NATIVE_MANAGED_SEARXNG is True
    assert configured.NATIVE_MANAGED_DEGOOG is False


def test_managed_remote_service_rejected_and_external_override_supported(tmp_path):
    with pytest.raises(ValueError, match="loopback"):
        Config(
            tmp_path,
            {
                "DATABASE_URL": "postgresql://u:p@remote/db",
                "NATIVE_DATABASE_MODE": "managed",
            },
        )
    cfg = Config(tmp_path, {"NATIVE_SEARXNG_MODE": "external"})
    assert not cfg.managed["SEARXNG_BASE_URL"]
    assert cfg.env["NATIVE_MANAGED_SEARXNG"] == "0"


def test_dotenv_never_executes_shell(tmp_path):
    path = tmp_path / "config"
    path.write_text("export VALUE='$(touch never)'\n# comment\nA=hello # comment\n")
    assert dotenv(path) == {"VALUE": "$(touch never)", "A": "hello"}


def test_lock_prevents_two_supervisors_and_releases(tmp_path):
    path = tmp_path / "lock"
    with RuntimeLock(path):
        with pytest.raises(RuntimeError, match="already running"):
            with RuntimeLock(path):
                pytest.fail("Second supervisor acquired lock")
    with RuntimeLock(path):
        pass


def test_process_supervisor_runs_and_stops_only_owned_service(tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    manager = Processes(tmp_path)
    process = manager.start(
        "test",
        [sys.executable, "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        tmp_path,
        dict(os.environ),
        port,
    )
    try:
        wait_http(f"http://127.0.0.1:{port}", process, timeout=10)
        assert manager.start("test", [], tmp_path, {}, port) is process
        other = Processes(tmp_path / "other")
        with pytest.raises(RuntimeError, match="occupied"):
            other.start("other", [], tmp_path, {}, port)
        other.close()
        assert process.poll() is None
    finally:
        manager.close()
    assert process.poll() is not None


def test_external_search_services_never_start_or_stop_local_processes(tmp_path):
    runtime = NativeSearchRuntime(tmp_path)
    runtime.cfg = Config(
        tmp_path,
        {
            "SEARXNG_BASE_URL": "http://remote:8080",
            "DEGOOG_BASE_URL": "http://remote:4444",
        },
    )
    assert runtime.reconcile({"searxng": True, "degoog": False}) == {
        "searxng": {"status": "external"},
        "degoog": {"status": "stopped"},
    }
    assert not runtime.processes.owned
    with pytest.raises(ValueError):
        runtime.reconcile({"searxng": "true", "degoog": False})


@pytest.mark.parametrize("mode", ["native", "docker"])
def test_host_runtime_import_health_and_shutdown_lifespan(tmp_path, monkeypatch, mode):
    from app.services import pip_persistence
    from app.voice import asr, tts
    from native import services
    import search_runtime

    closed = []
    native = SimpleNamespace(close=lambda: closed.append("native"))
    monkeypatch.setenv("REALOPEN_RUNTIME", mode)
    monkeypatch.setattr(settings, "REALOPEN_RUNTIME", mode)
    monkeypatch.setattr(services, "NativeSearchRuntime", lambda: native)
    monkeypatch.setattr(search_runtime, "SearchRuntime", lambda: SimpleNamespace())
    monkeypatch.setattr(
        pip_persistence, "activate_persistent_site_packages", lambda: None
    )
    monkeypatch.setattr(settings, "VOICE_RUNTIME_URL", settings.VOICE_RUNTIME_URL)
    monkeypatch.setattr(
        type(settings),
        "get_voice_config",
        lambda _self: SimpleNamespace(asr=None, tts=SimpleNamespace(voice="mary")),
    )
    engine = SimpleNamespace(status=lambda: {"ready": False})
    monkeypatch.setattr(asr, "create_asr_engine", lambda _: engine)
    monkeypatch.setattr(tts, "create_tts_engine", lambda _: engine)
    script = tmp_path / "scripts/host-runtime-server.py"
    script.parent.mkdir()
    script.write_text(
        (
            Path(__file__).resolve().parents[2] / "scripts/host-runtime-server.py"
        ).read_text()
    )
    spec = importlib.util.spec_from_file_location("native_host_lifespan_test", script)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module, "_sandboxes", lambda: SimpleNamespace(ping=lambda: False)
    )
    with TestClient(module.app) as client:
        assert client.get("/health").json()["runtime"] == mode
        assert not closed
    assert closed == (["native"] if mode == "native" else [])


def test_external_database_and_valkey_are_never_provisioned(tmp_path):
    cfg = Config(
        tmp_path,
        {"DATABASE_URL": "postgresql://u:p@remote/db", "REDIS_URL": "redis://remote"},
    )
    services = Services(
        cfg, SimpleNamespace(start=lambda *_: pytest.fail("External service started"))
    )
    services.database()
    services.valkey()
    assert not (cfg.data / "postgres").exists()


def test_apt_install_preserves_installed_packages(tmp_path, monkeypatch):
    cfg = Config(tmp_path, {})
    calls = []
    monkeypatch.setattr(
        dependencies.subprocess,
        "run",
        lambda command, **_: SimpleNamespace(
            stdout=(
                "install ok installed" if command[-1] == "existing" else "not installed"
            )
        ),
    )
    monkeypatch.setattr(dependencies, "run", lambda command, *_: calls.append(command))
    dependencies.Installer(cfg).apt(["existing", "missing"])
    install = next(command for command in calls if "install" in command)
    assert install[-1] == "missing" and "existing" not in install
    assert "--no-upgrade" in install


def test_external_services_skip_linux_service_packages(tmp_path, monkeypatch):
    cfg = Config(
        tmp_path,
        {
            "NATIVE_DATABASE_MODE": "external",
            "NATIVE_REDIS_MODE": "external",
            "NATIVE_OLLAMA_MODE": "external",
        },
    )
    installer = dependencies.Installer(cfg)
    packages = []
    monkeypatch.setattr(dependencies.sys, "platform", "linux")
    monkeypatch.setattr(dependencies.shutil, "which", lambda *_a, **_kw: "/bin/apt-get")
    monkeypatch.setattr(installer, "apt", lambda values: packages.extend(values))
    monkeypatch.setattr(installer, "node", lambda: None)
    installer.system()
    assert "tesseract-ocr-eng" in packages
    assert not any(
        name.startswith(("postgresql", "redis", "valkey", "ollama"))
        for name in packages
    )


def test_native_frontend_preserves_api_and_spa_routes(tmp_path):
    (tmp_path / "index.html").write_text("<html>App</html>")
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/app.js").write_text("const ready = true;")
    app = FastAPI()

    @app.get("/api/health")
    def health():
        return {"ok": True}

    install_frontend(app, tmp_path)
    with TestClient(app) as client:
        assert client.get("/learn/notes/123").text == "<html>App</html>"
        assert client.get("/assets/app.js").status_code == 200
        assert client.get("/assets/missing.js").status_code == 404
        assert client.get("/api/health").json() == {"ok": True}
        assert client.get("/api/missing").status_code == 404
        assert client.get("/ws/missing").status_code == 404
        assert client.get("/%2e%2e/secret").status_code == 404


def test_migration_refuses_nonempty_target_before_stopping_containers(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        migration,
        "legacy_mounts",
        lambda _: [{"volume": "old", "destination": "uploads"}],
    )
    monkeypatch.setattr(migration, "containers", lambda _: [])
    target = tmp_path / "data/uploads"
    target.mkdir(parents=True)
    (target / "precious").write_text("retain")
    monkeypatch.setattr(
        migration,
        "docker",
        lambda *_a, **_k: pytest.fail("Docker mutation before safety check"),
    )
    with pytest.raises(RuntimeError, match="overwrite"):
        migration.migrate_storage(tmp_path)
    assert (target / "precious").read_text() == "retain"


def test_legacy_guard_requires_migration_and_respects_completed_marker(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(migration, "legacy_mounts", lambda _: [{"volume": "old"}])
    with pytest.raises(RuntimeError, match="migrate-storage"):
        migration.check_legacy(tmp_path)
    atomic_json(
        tmp_path / "data/migrations/volumes.json", {"old": {"destination": "uploads"}}
    )
    migration.check_legacy(tmp_path)


def test_native_restore_verifies_checksum_and_refuses_existing_tables(
    tmp_path, monkeypatch
):
    cfg = Config(tmp_path, {})
    dump = cfg.data / "migrations/export.dump"
    dump.parent.mkdir(parents=True)
    dump.write_bytes(b"database export")
    metadata = {
        "dump": "migrations/export.dump",
        "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
    }
    atomic_json(dump.parent / "database.json", metadata)
    calls = []
    services = SimpleNamespace(
        database=lambda: calls.append("database"), psql=lambda _: "conversations"
    )
    with pytest.raises(RuntimeError, match="not empty"):
        migration.restore_native(cfg, services)
    assert calls == ["database"]
    atomic_json(cfg.runtime / "migration.json", metadata)
    migration.restore_native(cfg, services)
    dump.write_bytes(b"corrupted")
    with pytest.raises(RuntimeError, match="checksum"):
        migration.restore_native(cfg, services)


def test_native_restore_never_writes_to_external_database(tmp_path):
    cfg = Config(tmp_path, {"DATABASE_URL": "postgresql://u:p@remote/db"})
    dump = cfg.data / "migrations/export.dump"
    dump.parent.mkdir(parents=True)
    dump.write_bytes(b"export")
    atomic_json(
        dump.parent / "database.json",
        {
            "dump": "migrations/export.dump",
            "sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        },
    )
    with pytest.raises(RuntimeError, match="external"):
        migration.restore_native(
            cfg,
            SimpleNamespace(database=lambda: pytest.fail("Touched external database")),
        )


def test_native_restore_rejects_paths_outside_data(tmp_path):
    cfg = Config(tmp_path, {})
    atomic_json(
        cfg.data / "migrations/database.json",
        {"dump": "../../outside", "sha256": "unused"},
    )
    with pytest.raises(RuntimeError, match="path"):
        migration.restore_native(cfg, None)


def test_cli_help_does_not_install_or_start_anything():
    root = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, root / "scripts/run-native.py", "--help"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert "migrate" in result.stdout and "--no-install" in result.stdout


def test_native_search_aliases_preserve_custom_urls(monkeypatch):
    monkeypatch.setattr(search_services.settings, "REALOPEN_RUNTIME", "native")
    monkeypatch.setattr(
        search_services.settings, "SEARXNG_BASE_URL", "http://127.0.0.1:8080"
    )
    assert (
        search_services.provider_url("searxng", "http://searxng:8080")
        == "http://127.0.0.1:8080"
    )
    assert (
        search_services.provider_url("searxng", "https://custom.example/search")
        == "https://custom.example/search"
    )


@pytest.mark.parametrize("unsafe", [False, True])
def test_volume_copy_preserves_original_and_rejects_unsafe_archive(
    tmp_path, monkeypatch, unsafe
):
    entry = {
        "volume": "old-uploads",
        "destination": "uploads",
        "container": "old-backend",
        "image": "helper",
        "service": "backend",
    }
    monkeypatch.setattr(migration, "legacy_mounts", lambda _: [entry])
    monkeypatch.setattr(migration, "containers", lambda _: [])
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if args[0] == "run":
            with tarfile.open(fileobj=kwargs["stdout"], mode="w") as archive:
                contents = b"saved upload"
                member = tarfile.TarInfo("../../outside" if unsafe else "report.txt")
                member.size = len(contents)
                archive.addfile(member, io.BytesIO(contents))
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(migration, "docker", docker)
    if unsafe:
        with pytest.raises(tarfile.TarError):
            migration.migrate_storage(tmp_path)
        assert not (tmp_path / "data/uploads").exists()
        assert not (tmp_path / "outside").exists()
    else:
        migration.migrate_storage(tmp_path)
        assert (tmp_path / "data/uploads/report.txt").read_text() == "saved upload"
        marker = json.loads((tmp_path / "data/migrations/volumes.json").read_text())
        assert "old-uploads" in marker
        migration.migrate_storage(tmp_path)
        assert len([call for call in calls if call[0] == "run"]) == 1
    assert not any(call[:2] == ("volume", "rm") for call in calls)


def test_bind_mounted_database_is_exported_even_without_legacy_volumes(
    tmp_path, monkeypatch
):
    pg = {
        "Id": "test-pg",
        "Image": "test-image",
        "State": {"Running": True},
        "Config": {"Labels": {"com.docker.compose.service": "postgres"}},
    }
    monkeypatch.setattr(migration, "legacy_mounts", lambda _: [])
    monkeypatch.setattr(migration, "containers", lambda _: [pg])
    calls = []

    def docker(*args, **kwargs):
        calls.append(args)
        if "stdout" in kwargs:
            kwargs["stdout"].write(b"portable-db-export")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(migration, "docker", docker)
    migration.migrate_storage(tmp_path)
    metadata = json.loads((tmp_path / "data/migrations/database.json").read_text())
    assert (tmp_path / "data" / metadata["dump"]).read_bytes() == b"portable-db-export"
    assert ("stop", "--time", "60", "test-pg") in calls


def test_workspace_archive_preserves_absolute_and_relative_virtualenv_links(tmp_path):
    archive = tmp_path / "workspace.tar"
    with tarfile.open(archive, "w") as output:
        for name, target in [
            ("./.venv/bin/python", "/usr/local/bin/python3.12"),
            ("./.venv/bin/python3", "python"),
        ]:
            member = tarfile.TarInfo(name)
            member.type, member.linkname = tarfile.SYMTYPE, target
            output.addfile(member)
        member = tarfile.TarInfo("./app.py")
        member.size = 5
        output.addfile(member, io.BytesIO(b"hello"))
        member = tarfile.TarInfo("./copy.py")
        member.type, member.linkname = tarfile.LNKTYPE, "./app.py"
        output.addfile(member)
    staging = tmp_path / "staging"
    staging.mkdir()
    migration.extract_volume_archive(archive, staging, workspace=True)
    assert os.readlink(staging / ".venv/bin/python") == "/usr/local/bin/python3.12"
    assert os.readlink(staging / ".venv/bin/python3") == "python"
    assert (staging / "app.py").read_bytes() == b"hello"
    assert os.path.samefile(staging / "app.py", staging / "copy.py")
    with pytest.raises(tarfile.AbsoluteLinkError):
        migration.extract_volume_archive(archive, tmp_path / "non-workspace")


@pytest.mark.parametrize("nested_link", [False, True])
def test_workspace_archive_cannot_write_through_escaping_links(tmp_path, nested_link):
    outside = tmp_path / "outside"
    outside.mkdir()
    archive = tmp_path / "unsafe.tar"
    with tarfile.open(archive, "w") as output:
        member = tarfile.TarInfo("escape")
        member.type, member.linkname = tarfile.SYMTYPE, str(outside)
        output.addfile(member)
        member = tarfile.TarInfo("escape/payload")
        if nested_link:
            member.type, member.linkname = tarfile.SYMTYPE, "/usr/local/bin/python"
            output.addfile(member)
        else:
            member.size = 4
            output.addfile(member, io.BytesIO(b"evil"))
    staging = tmp_path / "staging"
    staging.mkdir()
    with pytest.raises(tarfile.FilterError):
        migration.extract_volume_archive(archive, staging, workspace=True)
    assert not list(outside.iterdir())
