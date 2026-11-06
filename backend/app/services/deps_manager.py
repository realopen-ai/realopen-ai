"""Optional dependency installation with streamed package-manager progress.

System packages use the detected OS manager (APT in Debian containers,
Homebrew casks on macOS, and the supported native Linux/Windows managers).
Native commands never prompt for passwords over HTTP: privileged Linux
commands use noninteractive sudo and return a manual terminal command if
permissions are unavailable. macOS app bundles live under data/native/apps.

The shared data/.installed-deps.json manifest records runtime and platform
ownership. Native removal does not adopt pre-existing system installations,
and failed operations keep the manifest unchanged. Cached APT recovery is
restricted to Debian Docker startup; native system packages persist in place.
Python dependencies retain their provider-aware persistent pip install path.
"""

from __future__ import annotations

import asyncio
from app.services import ocr
import importlib.metadata
import importlib.util
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Callable, Optional
from app.config import settings
from app.services.integrations.libreoffice import _find_soffice

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[deps] {formatted}", flush=True)


# ── Paths ────────────────────────────────────────────────────────────

# apt cache directory (Docker volume). .deb files live in archives/.
APT_CACHE_DIR = Path("/var/cache/apt/archives")

# Manifest file tracks which deps the user has explicitly installed.
# Lives in /app/data which is mounted as a Docker volume (./data:/app/data).
MANIFEST_PATH = settings.get_data_dir() / ".installed-deps.json"


# ── Dependency catalog ───────────────────────────────────────────────


@dataclass
class Dependency:
    name: str
    display_name: str
    description: str
    category: str
    kind: str  # "system-direct" (apt-get) | "pip" (python package)
    binary_name: Optional[str] = None  # for is_installed check
    pip_name: Optional[str] = None  # for pip-installed deps
    import_name: Optional[str] = None  # module name for find_spec checks
    pip_extra_args: list[str] = field(default_factory=list)  # e.g. cpu index URL
    install_size: str = ""
    enables: list[str] = field(default_factory=list)
    ocr_language: Optional[str] = None


def _voice_dep(
    name: str,
    display: str,
    description: str,
    pip_name: str,
    import_name: Optional[str] = None,
    pip_extra_args: Optional[list[str]] = None,
    install_size: str = "",
    enables: Optional[list[str]] = None,
) -> Dependency:
    """Build a voice-runtime pip Dependency (kind "pip").

    NOTE: which packages the voice feature *needs* is resolved from
    profiles.yml via app.services.voice_model_installer — these catalog
    entries only describe HOW each package is installed so the existing
    Settings ▸ Dependencies UI can show/install them.
    """
    return Dependency(
        name=name,
        display_name=display,
        description=description,
        category="Voice",
        kind="pip",
        pip_name=pip_name,
        import_name=import_name,
        pip_extra_args=pip_extra_args or [],
        install_size=install_size,
        enables=enables or [],
    )


CATALOG: list[Dependency] = [
    Dependency(
        name="tesseract-ocr-fra",
        display_name="French OCR language",
        description="Recognize French text in scanned documents with local Tesseract.",
        category="System",
        kind="system-direct",
        ocr_language="fra",
        install_size="~1 MB",
    ),
    Dependency(
        name="tesseract-ocr-ara",
        display_name="Arabic OCR language",
        description="Recognize Arabic text in scanned documents with local Tesseract.",
        category="System",
        kind="system-direct",
        ocr_language="ara",
        install_size="~2 MB",
    ),
    Dependency(
        name="libreoffice",
        display_name="LibreOffice",
        description="Headless document conversion and rendering.",
        category="System",
        kind="system-direct",
        binary_name="soffice",
        install_size="~600 MB",
        enables=[
            "High-quality template thumbnails",
            "MS-formats conversions",
        ],
    ),
    # ── Voice runtime (pip) ──────────────────────────────────────────
    # Mirrors RUNTIME_PACKAGES in app/services/voice_model_installer.py
    # (kept consistent by backend/tests).
    _voice_dep(
        "mlx-qwen3-asr",
        "Qwen3-ASR MLX runtime (Apple Silicon)",
        "Native MLX speech-recognition runtime for Apple-Silicon hosts (no PyTorch needed).",
        "mlx-qwen3-asr",
        import_name="mlx_qwen3_asr",
        install_size="~60 MB",
        enables=["Voice input (ASR model runtime)"],
    ),
    _voice_dep(
        "torch-cpu",
        "PyTorch (CPU)",
        "CPU-only PyTorch build used by the speech recognition model.",
        "torch",
        pip_extra_args=["--index-url", "https://download.pytorch.org/whl/cpu"],
        install_size="~200 MB",
        enables=["Voice input (ASR model runtime)"],
    ),
    _voice_dep(
        "transformers",
        "Transformers",
        "Hugging Face Transformers — runs the ASR model.",
        "transformers",
        install_size="~50 MB",
        enables=["Voice input (ASR model runtime)"],
    ),
    _voice_dep(
        "soundfile",
        "SoundFile",
        "libsndfile bindings for reading/writing audio (ASR/TTS I/O).",
        "soundfile",
        install_size="~3 MB",
        enables=["Voice audio input/output"],
    ),
    _voice_dep(
        "webrtcvad-wheels",
        "WebRTC VAD",
        "Voice activity detection used for barge-in / pre-roll gating.",
        "webrtcvad-wheels",
        import_name="webrtcvad",
        install_size="~1 MB",
        enables=["Barge-in interruption detection"],
    ),
    _voice_dep(
        "huggingface-hub",
        "Hugging Face Hub client",
        "Client used by the setup wizard to download ASR model files.",
        "huggingface-hub",
        import_name="huggingface_hub",
        install_size="~2 MB",
        enables=["Voice model downloads during setup"],
    ),
    _voice_dep(
        "pocket-tts",
        "Pocket TTS runtime",
        "Streaming neural speech synthesis runtime.",
        "pocket-tts",
        import_name="pocket_tts",
        install_size="~600 MB",
        enables=["Voice output (TTS)"],
    ),
]


# ── Package name mapping per distro ──────────────────────────────────


_PKG_MAP: dict[str, dict[str, list[str]]] = {
    "tesseract-ocr-fra": {"debian": ["tesseract-ocr-fra"]},
    "tesseract-ocr-ara": {"debian": ["tesseract-ocr-ara"]},
    "libreoffice": {
        "debian": [
            "libreoffice-core",
            "libreoffice-impress",
            "libreoffice-writer",
            "libreoffice-calc",
        ],
        "fedora": [
            "libreoffice-core",
            "libreoffice-impress",
            "libreoffice-writer",
            "libreoffice-calc",
        ],
        "arch": ["libreoffice-fresh"],
        "suse": ["libreoffice"],
        "alpine": [],
        "macos": ["libreoffice"],
        "windows": ["TheDocumentFoundation.LibreOffice"],
    },
}

# Formulae are the Homebrew default; GUI applications declare cask packaging.
_PKG_KINDS = {"libreoffice": {"macos": "cask"}}


# ── Detection ────────────────────────────────────────────────────────


def is_installed(dep: Dependency) -> bool:
    """Check if a dependency is installed.

    - binary deps: binary available on PATH (e.g. soffice)
    - pip deps (binary_name is None, pip_name set): module importable —
      checked with importlib.util.find_spec ONLY (never import heavy libs
      like torch in the backend loop), with an importlib.metadata fallback
      for packages whose import name differs from the distribution name.
    """
    if dep.ocr_language:
        return dep.ocr_language in ocr.installed_languages()
    if dep.binary_name:
        return (
            _find_soffice()
            if dep.binary_name == "soffice"
            else shutil.which(dep.binary_name)
        ) is not None
    if dep.pip_name:
        module_name = dep.import_name or dep.pip_name.replace("-", "_")
        try:
            if importlib.util.find_spec(module_name) is not None:
                return True
        except Exception:
            pass
        try:
            importlib.metadata.version(dep.pip_name)
            return True
        except Exception:
            return False
    return False


def get_version(dep: Dependency) -> Optional[str]:
    """Get the installed version of a dependency, if available."""
    if not is_installed(dep):
        return None
    try:
        if dep.binary_name:
            proc = subprocess.run(
                [
                    (
                        _find_soffice()
                        if dep.binary_name == "soffice"
                        else dep.binary_name
                    ),
                    "--version",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0:
                # Parse first line of output
                return proc.stdout.strip().split("\n")[0][:100]
        elif dep.pip_name:
            import importlib.metadata

            pkg_name = (
                dep.pip_name.replace("-", "_")
                .split("[")[0]
                .split(">")[0]
                .split("<")[0]
                .split("=")[0]
            )
            return importlib.metadata.version(pkg_name)
    except Exception:
        pass
    return None


# ── Distro detection ─────────────────────────────────────────────────


def _detect_distro() -> str:
    """Detect the Linux distribution (or 'macos' / 'unknown')."""
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"

    # Check /etc/os-release
    try:
        release_file = Path("/etc/os-release")
        if release_file.exists():
            content = release_file.read_text()
            for line in content.split("\n"):
                if line.startswith("ID="):
                    distro_id = line.split("=", 1)[1].strip().strip('"').lower()
                    if distro_id in ("debian", "ubuntu", "linuxmint", "raspbian"):
                        return "debian"
                    elif distro_id in ("fedora", "rhel", "centos", "rocky", "alma"):
                        return "fedora"
                    elif distro_id in ("arch", "manjaro", "endeavouros"):
                        return "arch"
                    elif distro_id in ("alpine",):
                        return "alpine"
                    elif distro_id in ("opensuse", "suse", "sles"):
                        return "suse"
                    return distro_id
    except Exception:
        pass

    # Fallback: check for package managers
    if shutil.which("apt-get"):
        return "debian"
    if shutil.which("dnf") or shutil.which("yum"):
        return "fedora"
    if shutil.which("pacman"):
        return "arch"
    if shutil.which("apk"):
        return "alpine"
    if shutil.which("zypper"):
        return "suse"
    if shutil.which("brew"):
        return "macos"

    return "unknown"


def is_available_for_distro(dep: Dependency) -> bool:
    """Check if this dependency can be installed on the current distro."""
    distro = _detect_distro()
    if dep.kind == "system-direct":
        packages = _PKG_MAP.get(dep.name, {}).get(distro, [])
        return len(packages) > 0
    return True


def get_install_command(dep: Dependency) -> Optional[str]:
    """Get the shell command that would install this dependency (for display)."""
    distro = _detect_distro()
    if dep.kind == "system-direct":
        packages = _PKG_MAP.get(dep.name, {}).get(distro, [])
        if not packages:
            return None
        return shlex.join(_system_commands(dep, distro)[-1])
    if dep.kind == "pip" and dep.pip_name:
        cmd = f"pip install {dep.pip_name}"
        if dep.pip_extra_args:
            cmd += " " + " ".join(shlex.quote(a) for a in dep.pip_extra_args)
        return cmd
    return None


def _system_commands(dep: Dependency, distro: str, *, remove=False) -> list[list[str]]:
    """Package-manager commands shared by UI previews and actual execution."""
    packages = _PKG_MAP.get(dep.name, {}).get(distro, [])
    if not packages:
        raise ValueError(
            f"{dep.display_name} has no supported package mapping for {distro}."
        )
    if distro == "macos":
        cask = _PKG_KINDS.get(dep.name, {}).get(distro) == "cask"
        command = [
            "brew",
            "uninstall" if remove else "install",
            *(["--cask"] if cask else []),
            *packages,
        ]
        if cask and not remove:
            command.append(f"--appdir={settings.get_data_dir() / 'native/apps'}")
        return [command]
    if distro == "windows":
        return [
            [
                "winget",
                "uninstall" if remove else "install",
                "--id",
                packages[0],
                "--exact",
                "--silent",
                "--disable-interactivity",
                *(
                    []
                    if remove
                    else ["--accept-package-agreements", "--accept-source-agreements"]
                ),
            ]
        ]
    if distro == "debian":
        return (
            [["apt-get", "remove", "-y", "--auto-remove", *packages]]
            if remove
            else [
                ["apt-get", "update", "-qq"],
                ["apt-get", "install", "-y", "--no-install-recommends", *packages],
            ]
        )
    managers = {
        "fedora": [
            "yum" if not shutil.which("dnf") and shutil.which("yum") else "dnf",
            "remove" if remove else "install",
            "-y",
        ],
        "arch": ["pacman", "-R" if remove else "-S", "--noconfirm"],
        "suse": ["zypper", "--non-interactive", "remove" if remove else "install"],
        "alpine": ["apk", "del" if remove else "add"],
    }
    if distro not in managers:
        raise ValueError(f"No package-manager integration for {distro}.")
    return [[*managers[distro], *packages]]


def _execution_command(command: list[str]) -> str:
    prefix = []
    if (
        settings.REALOPEN_RUNTIME == "native"
        and command[0] not in {"brew", "winget"}
        and hasattr(os, "geteuid")
        and os.geteuid() != 0
    ):
        prefix = ["sudo", "-n"]
    if command[0] == "apt-get":
        prefix = ["env", "DEBIAN_FRONTEND=noninteractive", *prefix]
    return shlex.join([*prefix, *command])


def _manual_command(command: list[str]) -> str:
    if command[0] == "winget":
        command = [
            part
            for part in command
            if part not in {"--silent", "--disable-interactivity"}
        ]
    prefix = (
        ["sudo"]
        if settings.REALOPEN_RUNTIME == "native"
        and command[0] not in {"brew", "winget"}
        and hasattr(os, "geteuid")
        and os.geteuid() != 0
        else []
    )
    return shlex.join([*prefix, *command])


# ── Manifest ─────────────────────────────────────────────────────────


def _read_manifest() -> dict:
    """Read the manifest file."""
    try:
        if MANIFEST_PATH.exists():
            return json.loads(MANIFEST_PATH.read_text())
    except Exception as e:
        _log("failed to read manifest: %s", e)
    return {"installed": {}}


def _write_manifest(data: dict) -> None:
    """Write the manifest file."""
    try:
        MANIFEST_PATH.parent.mkdir(parents=True, exist_ok=True)
        data["updated_at"] = datetime.utcnow().isoformat()
        MANIFEST_PATH.write_text(json.dumps(data, indent=2))
    except Exception as e:
        _log("failed to write manifest: %s", e)


def _record_install(dep: Dependency) -> None:
    """Record an installation in the manifest."""
    manifest = _read_manifest()
    manifest.setdefault("installed", {})[dep.name] = {
        "installed_at": datetime.utcnow().isoformat(),
        "runtime": settings.REALOPEN_RUNTIME,
        "platform": _detect_distro(),
    }
    _write_manifest(manifest)
    _log("recorded %s in manifest", dep.name)


def _record_uninstall(dep: Dependency) -> None:
    """Remove an installation from the manifest."""
    manifest = _read_manifest()
    if dep.name in manifest.get("installed", {}):
        del manifest["installed"][dep.name]
        _write_manifest(manifest)
        _log("removed %s from manifest", dep.name)


# ── Async command helpers ────────────────────────────────────────────


async def _run_apt_command_async(
    cmd: list[str],
    timeout: int = 300,
) -> tuple[int, str, str]:
    """Run an apt/dpkg command asynchronously.

    Returns (returncode, stdout, stderr).
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "", f"Timeout after {timeout}s"

    return (
        proc.returncode,
        stdout.decode(errors="replace") if stdout else "",
        stderr.decode(errors="replace") if stderr else "",
    )


async def _run_command_streaming(
    cmd: str,
    stage: str,
    output_lines: list[str],
) -> AsyncGenerator[dict, None]:
    """Run a command and yield each line of output as an SSE event."""
    parts = shlex.split(cmd)
    env = {
        **os.environ,
        "HOMEBREW_NO_AUTO_UPDATE": "1",
        "HOMEBREW_NO_INSTALL_CLEANUP": "1",
        "HOMEBREW_NO_SUDO": "1",
    }
    try:
        proc = await asyncio.create_subprocess_exec(
            *parts,
            env=env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError:
        message = f"Cannot start {parts[0]}. Install the package manager or run the suggested command in a terminal."
        output_lines.append(message)
        yield {"stage": stage, "output": message}
        yield {"_returncode": 127}
        return

    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        text = line.decode(errors="replace").strip()
        if text:
            output_lines.append(text)
            yield {"stage": stage, "output": text}

    await proc.wait()
    yield {"_returncode": proc.returncode}


# ── Installation ─────────────────────────────────────────────────────


async def _install_system_direct(
    dep: Dependency,
    distro: str,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> AsyncGenerator[dict, None]:
    """Install with the detected package manager; never request a web password."""
    try:
        commands = _system_commands(dep, distro)
    except ValueError:
        logger.exception(
            "Cannot prepare dependency installation for %s on %s", dep.name, distro
        )
        yield {
            "stage": "error",
            "error": "Cannot prepare dependency installation for this platform. Check server logs for details.",
        }
        return
    manager = commands[-1][0]
    manual = " && ".join(_manual_command(command) for command in commands)
    if not shutil.which(manager):
        yield {
            "stage": "error",
            "error": f"{manager} is not installed or not on PATH. Install it or run this command in a terminal.",
            "manual_command": manual,
        }
        return
    if distro == "macos":
        (settings.get_data_dir() / "native/apps").mkdir(parents=True, exist_ok=True)
    for command in commands:
        stage = "updating" if command is not commands[-1] else "installing"
        yield {"stage": stage, "output": f"Running {shlex.join(command)}..."}
        output_lines = []
        rc = 0
        async for event in _run_command_streaming(
            _execution_command(command), stage, output_lines
        ):
            if "_returncode" in event:
                rc = event["_returncode"]
            else:
                if progress_callback:
                    progress_callback(event)
                yield event
        if rc != 0:
            yield {
                "stage": "error",
                "exit_code": rc,
                "error": f"{manager} {stage} failed (exit code {rc}). If administrator access is required, run the command below in a terminal; the app cannot request your password.",
                "manual_command": manual,
                "output": "\n".join(output_lines[-30:]),
            }
            return
    if not is_installed(dep):
        yield {
            "stage": "error",
            "error": f"{manager} finished, but {dep.display_name} is not detectable. Check its installation location and server logs.",
            "manual_command": manual,
        }
        return
    _record_install(dep)
    yield {
        "stage": "done",
        "exit_code": 0,
        "output": f"{dep.display_name} installed successfully.",
        "version": get_version(dep),
    }


async def _install_pip(
    dep: Dependency,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> AsyncGenerator[dict, None]:
    """Install a pip dependency through the PERSISTENT wheelhouse.

    Uses :mod:`app.services.pip_persistence` (the LibreOffice-style pip
    strategy): the wheel lands in ``data/pip-wheels`` and the install runs
    offline-first from there — so the package survives container rebuilds
    via the startup replay instead of being re-downloaded each time.
    Streams every real output line as an SSE ``stage`` event — pip gives
    no byte-accurate progress, so the output is the honest progress
    indicator (no invented percentages)."""
    if not dep.pip_name:
        yield {
            "stage": "error",
            "error": f"{dep.display_name} has no pip package name.",
        }
        return

    yield {
        "stage": "installing",
        "output": (
            f"Installing {dep.pip_name} (persistent wheelhouse — survives container rebuilds)"
        ),
    }

    try:
        from app.services import pip_persistence
    except Exception:  # pragma: no cover — module lives in the same package
        pip_persistence = None  # type: ignore[assignment]

    failed = False
    if pip_persistence is not None:
        try:
            async for line in pip_persistence.pip_install_persistent(
                dep.pip_name, tuple(dep.pip_extra_args)
            ):
                if line:
                    event = {"stage": "installing", "output": line[:400]}
                    if progress_callback:
                        progress_callback(event)
                    yield event
        except Exception:  # noqa: BLE001 — fall back to plain pip
            logger.exception("Persistent dependency installation failed")
            yield {
                "stage": "installing",
                "output": "Persistent install failed — plain pip fallback. Check server logs.",
            }
            failed = True
        if not failed:
            rc = 0
    if failed or pip_persistence is None:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--progress-bar",
            "off",
            dep.pip_name,
            *dep.pip_extra_args,
        ]
        yield {
            "stage": "installing",
            "output": f"Running: {' '.join(shlex.quote(c) for c in cmd)}",
        }

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                event = {"stage": "installing", "output": text[:400]}
                if progress_callback:
                    progress_callback(event)
                yield event
            rc = await proc.wait()
        finally:
            if proc.returncode is None:
                proc.kill()
                try:
                    await proc.wait()
                except Exception:
                    pass

    if rc != 0:
        yield {
            "stage": "error",
            "exit_code": rc,
            "error": f"pip install failed (exit code {rc}).",
        }
        return

    # Refresh import caches so find_spec sees the new package in THIS process.
    importlib.invalidate_caches()

    # Step: Update manifest
    _record_install(dep)

    yield {
        "stage": "done",
        "exit_code": 0,
        "output": f"{dep.display_name} installed successfully.",
        "version": get_version(dep),
    }


async def install_dependency(
    dep: Dependency,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> AsyncGenerator[dict, None]:
    """Install a dependency. Yields SSE progress events."""
    distro = _detect_distro()

    if dep.kind == "pip":
        # pip works regardless of the distro (even "unknown").
        if is_installed(dep):
            _log("%s already installed — skipping", dep.name)
            yield {
                "stage": "done",
                "exit_code": 0,
                "output": f"{dep.display_name} is already installed.",
                "version": get_version(dep),
            }
            return
        async for event in _install_pip(dep, progress_callback):
            yield event
        return

    if distro == "unknown":
        yield {
            "stage": "error",
            "error": "Could not detect OS distribution.",
        }
        return

    if is_installed(dep):
        _log("%s already installed — skipping", dep.name)
        yield {
            "stage": "done",
            "exit_code": 0,
            "output": f"{dep.display_name} is already installed.",
            "version": get_version(dep),
        }
        return

    if dep.kind == "system-direct":
        async for event in _install_system_direct(dep, distro, progress_callback):
            yield event
        return

    yield {
        "stage": "error",
        "error": f"Unknown dependency kind: {dep.kind}",
    }


# ── Uninstallation ───────────────────────────────────────────────────


def _remove_debs_from_cache_for_packages(packages: list[str]) -> int:
    """Delete .deb files from cache that match the given package names."""
    removed = 0
    if not APT_CACHE_DIR.exists():
        return 0
    for pkg in packages:
        for deb in APT_CACHE_DIR.glob(f"{pkg}_*.deb"):
            try:
                deb.unlink()
                removed += 1
                _log("deleted cached .deb: %s", deb.name)
            except OSError as e:
                _log("warning: could not delete %s: %s", deb.name, e)
    return removed


async def _uninstall_system_direct(
    dep: Dependency,
    distro: str,
) -> AsyncGenerator[dict, None]:
    """Remove only an app-managed native installation; retain state on failure."""
    try:
        command = _system_commands(dep, distro, remove=True)[-1]
    except ValueError:
        logger.exception(
            "Cannot prepare dependency removal for %s on %s", dep.name, distro
        )
        yield {
            "stage": "error",
            "error": "Cannot prepare dependency removal for this platform. Check server logs for details.",
        }
        return
    manual = _manual_command(command)
    if settings.REALOPEN_RUNTIME == "native":
        record = _read_manifest().get("installed", {}).get(dep.name, {})
        if record.get("runtime") != "native" or record.get("platform") != distro:
            yield {
                "stage": "error",
                "error": f"{dep.display_name} was not installed by this native runtime. Manage this existing installation with your OS package manager.",
                "manual_command": manual,
            }
            return
    yield {"stage": "uninstalling", "output": f"Running {shlex.join(command)}..."}
    output_lines = []
    rc = 0
    async for event in _run_command_streaming(
        _execution_command(command), "uninstalling", output_lines
    ):
        if "_returncode" in event:
            rc = event["_returncode"]
        else:
            yield event
    if rc != 0:
        yield {
            "stage": "error",
            "exit_code": rc,
            "error": f"{command[0]} removal failed (exit code {rc}). Run the command in a terminal if administrator access is required.",
            "manual_command": manual,
            "output": "\n".join(output_lines[-30:]),
        }
        return
    if distro == "debian":
        _remove_debs_from_cache_for_packages(_PKG_MAP[dep.name][distro])
    _record_uninstall(dep)
    yield {
        "stage": "done",
        "exit_code": 0,
        "output": f"{dep.display_name} uninstalled successfully.",
    }


async def _uninstall_pip(dep: Dependency) -> AsyncGenerator[dict, None]:
    """Uninstall a pip dependency with `python -m pip uninstall -y`."""
    if not dep.pip_name:
        yield {
            "stage": "error",
            "error": f"{dep.display_name} has no pip package name.",
        }
        return

    cmd = [sys.executable, "-m", "pip", "uninstall", "-y", dep.pip_name]
    yield {
        "stage": "uninstalling",
        "output": f"Running: {' '.join(shlex.quote(c) for c in cmd)}",
    }
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert proc.stdout is not None
    try:
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text:
                yield {"stage": "uninstalling", "output": text[:400]}
        rc = await proc.wait()
    finally:
        if proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass

    # pip uninstall returns non-zero when the package isn't installed —
    # that's fine, we still clean the manifest.
    if rc != 0:
        yield {
            "stage": "uninstalling",
            "output": f"pip uninstall returned {rc} (package may not be installed)",
        }

    importlib.invalidate_caches()
    _record_uninstall(dep)

    yield {
        "stage": "done",
        "exit_code": 0,
        "output": f"{dep.display_name} uninstalled successfully.",
    }


async def uninstall_dependency(
    dep: Dependency,
) -> AsyncGenerator[dict, None]:
    """Uninstall a dependency. Yields SSE progress events."""
    distro = _detect_distro()

    if dep.kind == "system-direct":
        async for event in _uninstall_system_direct(dep, distro):
            yield event
        return

    if dep.kind == "pip":
        async for event in _uninstall_pip(dep):
            yield event
        return

    yield {
        "stage": "error",
        "error": f"Unknown dependency kind: {dep.kind}",
    }


# ── Startup: reinstall from cache ────────────────────────────────────


def _apt_lists_present() -> bool:
    """Check if apt package lists are present in /var/lib/apt/lists/.

    apt-get install --no-download needs the package index to resolve
    dependencies. If the lists are empty (e.g. the volume was recreated
    from an image that had `rm -rf /var/lib/apt/lists/*`), the offline
    reinstall will fail with "Unable to locate package" (rc=100).
    """
    lists = Path("/var/lib/apt/lists")

    if not lists.exists():
        return False

    for p in lists.iterdir():
        if p.name in ("lock", "partial"):
            continue
        return True

    return False


async def verify_deps_on_startup() -> None:
    """Reinstall any deps that are in the manifest but missing from the system.

    After a container rebuild, the dpkg database is fresh and all apt-installed
    packages are gone. This reinstalls them:

      1. If apt package lists are present (from the apt-lists volume):
         → apt-get install --no-download (offline, no wifi needed)
      2. If that fails or lists are missing:
         → apt-get update + apt-get install -y (with network, uses cache
           for .debs that match, downloads only what's missing)

    Debian handles everything: dependency resolution, triggers, ldconfig,
    symlinks, filesystem layout. No package-specific recovery code needed.
    """
    if settings.REALOPEN_RUNTIME != "docker" or _detect_distro() != "debian":
        # Native installs persist in their OS locations. Never replay a shared
        # Docker manifest with a host package manager (or during app startup).
        return
    manifest = _read_manifest()
    installed = manifest.get("installed", {})

    if not installed:
        return

    distro = _detect_distro()

    for name in installed:
        dep = get_dependency(name)
        if not dep:
            _log("manifest references unknown dep '%s' — skipping", name)
            continue

        if dep.kind != "system-direct":
            # pip deps live in the image, not in the data volume — they are
            # re-installed by the (idempotent) voice setup wizard when the
            # user re-runs setup, never silently at startup.
            continue

        packages = _PKG_MAP.get(name, {}).get(distro, [])
        if not packages:
            continue

        if is_installed(dep):
            _log("'%s' already installed — skipping", name)
            continue

        _log("'%s' missing (post-rebuild) — reinstalling...", name)
        pkgs_str = " ".join(shlex.quote(p) for p in packages)

        # ── Strategy 1: offline reinstall (--no-download) ──
        # Only try this if apt package lists are present. If the lists are
        # empty, --no-download will fail with "Unable to locate package".
        lists_ok = _apt_lists_present()
        if lists_ok:
            _log("apt lists present — trying offline reinstall (--no-download)...")
            rc, stdout, stderr = await _run_apt_command_async(
                [
                    "sh",
                    "-c",
                    "env DEBIAN_FRONTEND=noninteractive apt-get install -y "
                    f"--no-download --no-install-recommends {pkgs_str}",
                ],
                timeout=300,
            )

            if rc == 0 and is_installed(dep):
                _log("'%s' reinstalled from cache (no wifi needed)", name)
                continue

            _log("offline reinstall failed (rc=%d): %s", rc, stderr.strip()[:300])
        else:
            _log("apt lists missing — skipping offline reinstall")

        # ── Strategy 2: online reinstall (apt-get update + install) ──
        # This requires network for apt-get update (~20-50MB), but apt will
        # use cached .debs from /var/cache/apt/archives/ when they match,
        # only downloading what's missing.
        _log("trying with network (apt-get update + install)...")
        rc, _, stderr = await _run_apt_command_async(
            [
                "sh",
                "-c",
                "env DEBIAN_FRONTEND=noninteractive apt-get update -qq",
            ],
            timeout=120,
        )
        if rc != 0:
            _log("apt-get update returned %d: %s", rc, stderr.strip()[:300])

        rc, _, stderr = await _run_apt_command_async(
            [
                "sh",
                "-c",
                "env DEBIAN_FRONTEND=noninteractive apt-get install -y "
                f"--no-install-recommends {pkgs_str}",
            ],
            timeout=300,
        )

        if rc == 0 and is_installed(dep):
            _log("'%s' reinstalled (with network)", name)
        else:
            _log(
                "ERROR: Failed to reinstall '%s' (rc=%d): %s",
                name,
                rc,
                stderr.strip()[:500],
            )
            _log("User must re-install %s via the Dependencies UI.", name)


# ── Catalog serialization ────────────────────────────────────────────


def get_catalog() -> list[dict]:
    """Return the dependency catalog with installation status."""
    distro = _detect_distro()
    result = []
    for dep in CATALOG:
        installed = is_installed(dep)
        version = get_version(dep) if installed else None
        install_cmd = get_install_command(dep)
        available = is_available_for_distro(dep)

        result.append(
            {
                "name": dep.name,
                "display_name": dep.display_name,
                "description": dep.description,
                "category": dep.category,
                "kind": dep.kind,
                "installed": installed,
                # in_overlay = installed (in the new architecture, being installed
                # means it's persistent — the apt cache allows offline reinstalls).
                # Kept for frontend compatibility (the "Persistent volume" badge).
                "in_overlay": installed,
                "version": version,
                "install_size": dep.install_size,
                "enables": dep.enables,
                "available": available,
                "install_cmd": install_cmd,
                "distro": distro,
                "persistence": (
                    "native"
                    if installed and settings.REALOPEN_RUNTIME == "native"
                    else (
                        "apt-cache"
                        if installed and dep.kind == "system-direct"
                        else ("image" if installed else "none")
                    )
                ),
            }
        )
    return result


def get_dependency(name: str) -> Optional[Dependency]:
    """Get a Dependency by name from the catalog."""
    for dep in CATALOG:
        if dep.name == name:
            return dep
    return None
