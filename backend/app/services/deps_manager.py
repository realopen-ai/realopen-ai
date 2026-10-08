"""
Dependency management service — installs, uninstalls, and persists optional
system-level dependencies using apt-get or pip.

## Architecture

Optional dependencies (like LibreOffice) are installed via `apt-get install`
to their default system locations. We persist:

  - `/var/cache/apt` (cached .deb files) — Docker volume
  - `/var/lib/apt/lists` (apt package index) — Docker volume
  - `/app/data/.installed-deps.json` (manifest of installed deps) — Docker volume

We do NOT persist the installed files themselves. After a container rebuild,
the dpkg database is fresh and all apt-installed packages are gone. On startup,
we replay `apt-get install --no-download` for each dep in the manifest — this
reinstalls from cached .debs with no network access needed. Debian handles
dependency resolution, triggers, ldconfig, and filesystem layout exactly as
intended.

Python (pip) dependencies (kind "pip", e.g. the voice runtime packages) are
installed into the interpreter's site-packages with `python -m pip install`.
Those live in the image, not in the data volume, so after an image rebuild
they are gone — the voice setup wizard (provider-aware, idempotent,
find_spec-based) re-installs them on the next setup run. The manifest is
still updated so the Dependencies UI reflects the user's choices.

## Install flow (user clicks Install)

  1. apt-get update                       (kind "system-direct")
     python -m pip install <pip_name>     (kind "pip", streamed output)
  2. apt-get install -y <packages>        (kind "system-direct")
  3. Update manifest

## Uninstall flow

  1. apt-get remove -y --auto-remove <packages>  (kind "system-direct")
     python -m pip uninstall -y <pip_name>        (kind "pip")
  2. Delete cached .debs for these packages (so it stays uninstalled after
     rebuild) — apt deps only
  3. Update manifest

This scales to any Debian package or pip package with zero
package-specific logic.
"""

from __future__ import annotations

import asyncio
from app.services import ocr
import importlib.metadata
import importlib.util
import json
import logging
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Callable, Optional

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
MANIFEST_PATH = Path("/app/data/.installed-deps.json")


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
    },
}


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
        return shutil.which(dep.binary_name) is not None
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
                [dep.binary_name, "--version"],
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
        pkgs_str = " ".join(shlex.quote(p) for p in packages)
        return f"apt-get install -y --no-install-recommends {pkgs_str}"
    if dep.kind == "pip" and dep.pip_name:
        cmd = f"pip install {dep.pip_name}"
        if dep.pip_extra_args:
            cmd += " " + " ".join(shlex.quote(a) for a in dep.pip_extra_args)
        return cmd
    return None


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
    proc = await asyncio.create_subprocess_exec(
        *parts,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

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
    """Install a system-direct dep via apt-get install to the default location.

    apt-get install handles everything: downloads .debs (cached for future
    reinstalls), installs to the correct locations, runs triggers, ldconfig, etc.
    """
    packages = _PKG_MAP.get(dep.name, {}).get(distro, [])
    if not packages:
        yield {
            "stage": "error",
            "error": f"{dep.display_name} is not available for {distro}.",
        }
        return

    pkgs_str = " ".join(shlex.quote(p) for p in packages)

    # Step 1: apt-get update
    yield {"stage": "updating", "output": "Updating package lists..."}
    async for event in _run_command_streaming(
        "env DEBIAN_FRONTEND=noninteractive apt-get update -qq",
        "updating",
        [],
    ):
        if "output" in event:
            if progress_callback:
                progress_callback(event)
            yield event

    # Step 2: apt-get install
    yield {"stage": "installing", "output": f"Installing {pkgs_str}..."}
    install_cmd = f"env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {pkgs_str}"
    install_lines: list[str] = []
    rc = 0
    async for event in _run_command_streaming(install_cmd, "installing", install_lines):
        if "_returncode" in event:
            rc = event["_returncode"]
        elif "output" in event:
            if progress_callback:
                progress_callback(event)
            yield event

    if rc != 0:
        yield {
            "stage": "error",
            "exit_code": rc,
            "error": f"apt-get install failed (exit code {rc}).",
            "output": "\n".join(install_lines[-30:]),
        }
        return

    # Step 3: Update manifest
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
    """Uninstall a system-direct dep via apt-get remove.

    apt-get remove handles everything: removes files, cleans dpkg db, runs
    triggers. We also clean cached .debs so it stays uninstalled after rebuild.
    """
    packages = _PKG_MAP.get(dep.name, {}).get(distro, [])
    if not packages:
        yield {
            "stage": "error",
            "error": f"Cannot uninstall — no packages for {distro}.",
        }
        return

    pkgs_str = " ".join(shlex.quote(p) for p in packages)

    # Step 1: apt-get remove
    yield {"stage": "uninstalling", "output": f"Running apt-get remove {pkgs_str}..."}
    remove_cmd = (
        f"env DEBIAN_FRONTEND=noninteractive apt-get remove -y --auto-remove {pkgs_str}"
    )
    remove_lines: list[str] = []
    rc = 0
    async for event in _run_command_streaming(remove_cmd, "uninstalling", remove_lines):
        if "_returncode" in event:
            rc = event["_returncode"]
        elif "output" in event:
            yield event

    # apt-get remove may return non-zero if the package isn't in the dpkg db
    # (e.g. after a rebuild where the startup reinstall failed). That's OK —
    # we still clean the cache + manifest.
    if rc != 0:
        yield {
            "stage": "uninstalling",
            "output": f"apt-get remove returned {rc} (package may not be in dpkg db)",
        }

    # Step 2: Clean cached .debs so it stays uninstalled after rebuild
    debs_removed = _remove_debs_from_cache_for_packages(packages)
    if debs_removed > 0:
        yield {
            "stage": "uninstalling",
            "output": f"Deleted {debs_removed} cached .deb file(s).",
        }

    # Step 3: Update manifest
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
                    "apt-cache"
                    if installed and dep.kind == "system-direct"
                    else ("image" if installed else "none")
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
