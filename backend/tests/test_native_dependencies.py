"""Optional native packages use the right manager and keep failures actionable."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import settings
from app.services import deps_manager as deps
from app.services.integrations import libreoffice


@pytest.fixture
def native(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "REALOPEN_RUNTIME", "native")
    monkeypatch.setattr(settings, "REALOPEN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(deps, "MANIFEST_PATH", tmp_path / "manifest.json")
    monkeypatch.setattr(deps, "get_version", lambda _: "test-version")
    return deps.get_dependency("libreoffice")


@pytest.mark.parametrize(
    "distro,manager",
    [
        ("macos", "brew"),
        ("debian", "apt-get"),
        ("fedora", "dnf"),
        ("arch", "pacman"),
        ("suse", "zypper"),
        ("windows", "winget"),
    ],
)
def test_command_preview_matches_platform_execution(
    native, monkeypatch, distro, manager
):
    monkeypatch.setattr(deps, "_detect_distro", lambda: distro)
    command = deps._system_commands(native, distro)[-1]
    assert command[0] == manager
    assert deps.get_install_command(native) == deps.shlex.join(command)
    if distro == "macos":
        assert "--cask" in command
        assert any(arg.startswith("--appdir=") for arg in command)
        assert "apt-get" not in command


@pytest.mark.asyncio
async def test_macos_install_records_success_only_after_detection(native, monkeypatch):
    monkeypatch.setattr(deps, "_detect_distro", lambda: "macos")
    monkeypatch.setattr(deps.shutil, "which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(deps, "is_installed", lambda _: True)
    commands = []

    async def run(command, stage, output):
        commands.append(command)
        yield {"stage": stage, "output": "Application installed"}
        yield {"_returncode": 0}

    monkeypatch.setattr(deps, "_run_command_streaming", run)
    events = [event async for event in deps._install_system_direct(native, "macos")]
    assert events[-1]["stage"] == "done"
    assert commands[0].startswith("brew install --cask libreoffice")
    assert deps._read_manifest()["installed"][native.name]["platform"] == "macos"
    assert deps._read_manifest()["installed"][native.name]["runtime"] == "native"


@pytest.mark.asyncio
async def test_failed_package_update_aborts_without_installing(native, monkeypatch):
    monkeypatch.setattr(deps.shutil, "which", lambda _: "/usr/bin/apt-get")
    calls = []

    async def run(command, stage, output):
        calls.append(command)
        output.append("sudo: a password is required")
        yield {"stage": stage, "output": output[-1]}
        yield {"_returncode": 1}

    monkeypatch.setattr(deps, "_run_command_streaming", run)
    events = [event async for event in deps._install_system_direct(native, "debian")]
    assert len(calls) == 1
    assert events[-1]["stage"] == "error"
    assert "terminal" in events[-1]["error"]
    assert "apt-get update" in events[-1]["manual_command"]
    assert "apt-get install" in events[-1]["manual_command"]
    assert not deps.MANIFEST_PATH.exists()


@pytest.mark.asyncio
async def test_missing_manager_returns_clear_error_and_manual_command(
    native, monkeypatch
):
    monkeypatch.setattr(deps.shutil, "which", lambda _: None)
    events = [event async for event in deps._install_system_direct(native, "macos")]
    assert events[-1]["stage"] == "error"
    assert "brew is not installed" in events[-1]["error"]
    assert "brew install --cask" in events[-1]["manual_command"]


@pytest.mark.asyncio
async def test_zero_exit_without_detectable_binary_is_not_success(native, monkeypatch):
    monkeypatch.setattr(deps.shutil, "which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(deps, "is_installed", lambda _: False)

    async def run(*_args):
        yield {"_returncode": 0}

    monkeypatch.setattr(deps, "_run_command_streaming", run)
    events = [event async for event in deps._install_system_direct(native, "macos")]
    assert events[-1]["stage"] == "error"
    assert "not detectable" in events[-1]["error"]
    assert not deps.MANIFEST_PATH.exists()


@pytest.mark.asyncio
async def test_existing_installation_is_not_adopted_or_reinstalled(native, monkeypatch):
    monkeypatch.setattr(deps, "_detect_distro", lambda: "macos")
    monkeypatch.setattr(deps, "is_installed", lambda _: True)
    events = [event async for event in deps.install_dependency(native)]
    assert events[-1]["stage"] == "done"
    assert "already installed" in events[-1]["output"]
    assert not deps.MANIFEST_PATH.exists()


@pytest.mark.asyncio
async def test_native_removal_does_not_use_docker_ownership_record(native):
    deps._write_manifest(
        {"installed": {native.name: {"runtime": "docker", "platform": "debian"}}}
    )
    events = [event async for event in deps._uninstall_system_direct(native, "macos")]
    assert events[-1]["stage"] == "error"
    assert "not installed by this native runtime" in events[-1]["error"]
    assert native.name in deps._read_manifest()["installed"]


@pytest.mark.asyncio
async def test_failed_native_removal_keeps_manifest(native, monkeypatch):
    monkeypatch.setattr(deps, "_detect_distro", lambda: "macos")
    deps._record_install(native)

    async def run(*_args):
        yield {"_returncode": 1}

    monkeypatch.setattr(deps, "_run_command_streaming", run)
    events = [event async for event in deps._uninstall_system_direct(native, "macos")]
    assert events[-1]["stage"] == "error"
    assert native.name in deps._read_manifest()["installed"]


@pytest.mark.asyncio
async def test_native_startup_never_replays_docker_apt_manifest(native, monkeypatch):
    monkeypatch.setattr(
        deps,
        "_read_manifest",
        lambda: pytest.fail("Native startup replayed Docker manifest"),
    )
    await deps.verify_deps_on_startup()


@pytest.mark.asyncio
async def test_missing_executable_is_a_streamed_failure(monkeypatch):
    monkeypatch.setattr(
        deps.asyncio,
        "create_subprocess_exec",
        AsyncMock(side_effect=FileNotFoundError()),
    )
    output = []
    events = [
        event
        async for event in deps._run_command_streaming(
            "missing-package-manager install example", "installing", output
        )
    ]
    assert events[-1] == {"_returncode": 127}
    assert "Cannot start missing-package-manager" in output[0]


def test_macos_bundle_is_detected_without_path_entry(native, monkeypatch, tmp_path):
    binary = tmp_path / "native/apps/LibreOffice.app/Contents/MacOS/soffice"
    binary.parent.mkdir(parents=True)
    binary.write_text("test")
    monkeypatch.setattr(libreoffice.sys, "platform", "darwin")
    monkeypatch.setattr(libreoffice, "_SOFFICE_PATH", str(tmp_path / "missing"))
    monkeypatch.setattr(libreoffice.shutil, "which", lambda _: None)
    monkeypatch.setattr(Path, "is_file", lambda path: str(path) == str(binary))
    assert libreoffice._find_soffice() == str(binary)
    assert deps.is_installed(native)


def test_docker_does_not_detect_a_host_macos_bundle(native, monkeypatch):
    monkeypatch.setattr(libreoffice.sys, "platform", "linux")
    monkeypatch.setattr(libreoffice, "_SOFFICE_PATH", "/missing-soffice")
    monkeypatch.setattr(libreoffice.shutil, "which", lambda _: None)
    assert libreoffice._find_soffice() is None
