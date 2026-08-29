"""
Dependency management service — installs, uninstalls, and persists optional
system-level dependencies using apt-get.

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

## Install flow (user clicks Install)

  1. apt-get update
  2. apt-get install -y <packages>     (downloads .debs to cache + installs)
  3. Update manifest

## Startup (after rebuild)

  1. Read manifest
  2. For each dep: apt-get install --no-download -y <packages>
     → reinstalls from cache, no wifi needed
     → Debian restores: binaries, libraries, symlinks, ldconfig, dpkg db
  3. Done

## Uninstall flow

  1. apt-get remove -y --auto-remove <packages>
  2. Delete cached .debs for these packages (so it stays uninstalled after rebuild)
  3. Update manifest

This scales to any Debian package with zero package-specific logic.
"""

from __future__ import annotations

import asyncio
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
    kind: str  # "system-direct" (apt-get install to default location)
    binary_name: Optional[str] = None  # for is_installed check
    pip_name: Optional[str] = None  # for pip-installed deps
    install_size: str = ""
    enables: list[str] = field(default_factory=list)


CATALOG: list[Dependency] = [
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
]


# ── Package name mapping per distro ──────────────────────────────────


_PKG_MAP: dict[str, dict[str, list[str]]] = {
    "libreoffice": {
        "debian": ["libreoffice-core", "libreoffice-impress", "libreoffice-writer"],
        "fedora": ["libreoffice-core", "libreoffice-impress", "libreoffice-writer"],
        "arch": ["libreoffice-fresh"],
        "suse": ["libreoffice"],
        "alpine": [],
        "macos": ["libreoffice"],
    },
}


# ── Detection ────────────────────────────────────────────────────────


def is_installed(dep: Dependency) -> bool:
    """Check if a dependency is installed (binary available on PATH)."""
    if dep.binary_name:
        return shutil.which(dep.binary_name) is not None
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


async def install_dependency(
    dep: Dependency,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> AsyncGenerator[dict, None]:
    """Install a dependency. Yields SSE progress events."""
    distro = _detect_distro()

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


async def uninstall_dependency(
    dep: Dependency,
) -> AsyncGenerator[dict, None]:
    """Uninstall a dependency. Yields SSE progress events."""
    distro = _detect_distro()

    if dep.kind == "system-direct":
        async for event in _uninstall_system_direct(dep, distro):
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
                    f"env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-download --no-install-recommends {pkgs_str}",
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
                f"env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {pkgs_str}",
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
                "persistence": "apt-cache" if installed else "none",
            }
        )
    return result


def get_dependency(name: str) -> Optional[Dependency]:
    """Get a Dependency by name from the catalog."""
    for dep in CATALOG:
        if dep.name == name:
            return dep
    return None
