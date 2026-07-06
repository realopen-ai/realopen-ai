"""
Dependency management service — detects, lists, installs, uninstalls, and
persists optional system-level dependencies using a volume-mounted overlay.

## Architecture

Optional dependencies are extracted to `/opt/optional/` (a Docker volume),
NOT installed to the system paths. This means:

  - No re-download on rebuild: .deb files are cached in `/var/cache/apt/`
    (also a volume).
  - No re-extract on rebuild: extracted files in `/opt/optional/` survive.
  - Zero time on rebuild: startup just creates symlinks (instant).

## Install flow

  1. `apt-get install -d`   -> downloads .debs to cache (uses cache if present)
  2. `dpkg-deb -x`          -> extracts each .deb to `/opt/optional/`
  3. Fix absolute symlinks  -> rewrite `/usr/...` -> `/opt/optional/usr/...`
  4. Create system symlinks -> for path-hardcoded binaries (e.g. LibreOffice's
     internal scripts look for `/usr/lib/libreoffice/`)
  5. Update manifest        -> record what was installed + file list

## Startup (after rebuild)

  1. Read manifest from `/opt/optional/.manifest.json`
  2. For each entry, check if binary exists in overlay
  3. If yes -> create system symlinks
  4. If no  -> re-extract from cached .debs (fast, no download)
  5. If no cache either -> user must re-install (only if volume was wiped)

## Uninstall flow

  1. Read file list from manifest
  2. Remove each file from `/opt/optional/`
  3. Remove system symlinks
  4. Clean up empty directories
  5. Update manifest
"""

from __future__ import annotations

import asyncio
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

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[deps] {formatted}", flush=True)


# ── Overlay paths ────────────────────────────────────────────────────

# The root of the volume-mounted overlay. All optional deps are extracted here.
OVERLAY_ROOT = Path("/opt/optional")

# apt cache directory (also volume-mounted). .deb files live in archives/.
APT_CACHE_DIR = Path("/var/cache/apt/archives")

# Manifest file tracks which deps are installed in the overlay + their files.
MANIFEST_PATH = OVERLAY_ROOT / ".manifest.json"


# ── Dependency catalog ───────────────────────────────────────────────


@dataclass
class SystemLink:
    """A symlink to create from a system path to an overlay path.

    Some packages (e.g. LibreOffice) have internal scripts that hardcode
    paths like `/usr/lib/libreoffice/`. We create symlinks from those
    system paths to the overlay so the hardcoded paths resolve correctly.
    """

    system_path: str  # e.g. "/usr/lib/libreoffice"
    overlay_path: str  # e.g. "/opt/optional/usr/lib/libreoffice"


@dataclass
class Dependency:
    name: str
    display_name: str
    description: str
    category: str  # "System", "LLM", etc.
    kind: str  # "system" (apt/dnf/brew) or "pip"
    # For system deps: the binary name to probe (shutil.which)
    binary_name: Optional[str] = None
    # For pip deps: the pip package name
    pip_name: Optional[str] = None
    # Approximate install size for UI display
    install_size: str = ""
    # What this dependency enables
    enables: list[str] = field(default_factory=list)
    # System symlinks to create (for path-hardcoded binaries)
    system_links: list[SystemLink] = field(default_factory=list)


# The catalog of known optional dependencies.
# To add a new dependency, just add a Dependency entry here.
CATALOG: list[Dependency] = [
    Dependency(
        name="libreoffice",
        display_name="LibreOffice",
        description="Headless document conversion and rendering. Required for high-quality PPTX template thumbnails, PPTX→PDF conversion, and proper template-based slide generation.",
        category="System",
        kind="system",
        binary_name="soffice",
        install_size="~500 MB",
        enables=[
            "High-quality template thumbnails",
            "PPTX → PDF conversion",
            "Proper slide generation from templates (preserves images & backgrounds)",
            "DOCX → PDF conversion",
        ],
        system_links=[
            # LibreOffice's internal scripts hardcode /usr/lib/libreoffice/
            SystemLink("/usr/lib/libreoffice", "/opt/optional/usr/lib/libreoffice"),
            SystemLink("/usr/share/libreoffice", "/opt/optional/usr/share/libreoffice"),
        ],
    ),
]


# ── Detection ────────────────────────────────────────────────────────


def _overlay_bin_path(dep: Dependency) -> Optional[Path]:
    """Get the path to the binary in the overlay, if it exists."""
    if not dep.binary_name:
        return None
    return OVERLAY_ROOT / "usr" / "bin" / dep.binary_name


def _is_in_overlay(dep: Dependency) -> bool:
    """Check if a dependency's binary exists in the overlay volume."""
    bin_path = _overlay_bin_path(dep)
    if bin_path and bin_path.exists():
        return True
    # For deps without a binary_name, check the manifest
    manifest = _read_manifest()
    return dep.name in manifest.get("installed", {})


def _is_system_dep_installed(dep: Dependency) -> bool:
    """Check if a system binary is installed (overlay or system PATH)."""
    # Check overlay first (fast, no subprocess)
    if _is_in_overlay(dep):
        return True
    # Fall back to system PATH
    if dep.binary_name:
        return shutil.which(dep.binary_name) is not None
    return False


def _is_pip_dep_installed(dep: Dependency) -> bool:
    """Check if a Python package is installed."""
    if not dep.pip_name:
        return False
    try:
        __import__(dep.pip_name.replace("-", "_").split("[")[0])
        return True
    except ImportError:
        return False


def is_installed(dep: Dependency) -> bool:
    """Check if a dependency is installed."""
    if dep.kind == "system":
        return _is_system_dep_installed(dep)
    elif dep.kind == "pip":
        return _is_pip_dep_installed(dep)
    return False


def get_version(dep: Dependency) -> Optional[str]:
    """Get the installed version of a dependency, if available."""
    if not is_installed(dep):
        return None
    try:
        if dep.kind == "system" and dep.binary_name:
            proc = subprocess.run(
                [dep.binary_name, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            if proc.returncode == 0:
                # Parse first line of output
                return proc.stdout.strip().split("\n")[0][:100]
        elif dep.kind == "pip" and dep.pip_name:
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


# ── Package name mapping per distro ──────────────────────────────────


# Maps our dependency names to distro-specific package names.
# Each entry: {dep_name: {distro: [package_names]}}
_PKG_MAP: dict[str, dict[str, list[str]]] = {
    "libreoffice": {
        "debian": ["libreoffice-core", "libreoffice-impress"],
        "fedora": ["libreoffice-core", "libreoffice-impress"],
        "arch": ["libreoffice-fresh"],
        "suse": ["libreoffice"],
        "alpine": [],
        "macos": ["libreoffice"],
    },
}


def get_install_command(dep: Dependency) -> Optional[str]:
    """Get the shell command that would install this dependency (for display)."""
    distro = _detect_distro()
    if dep.kind == "system":
        packages = _PKG_MAP.get(dep.name, {}).get(distro, [])
        if not packages:
            return None
        print(f"==== packages: {packages} ----")
        pkgs_str = " ".join(shlex.quote(p) for p in packages)
        return f"apt-get install -d -y --no-install-recommends {pkgs_str}  # → overlay"
    elif dep.kind == "pip" and dep.pip_name:
        return f"{sys.executable} -m pip install --target={OVERLAY_ROOT}/usr/lib/python3/dist-packages {dep.pip_name}"
    return None


def is_available_for_distro(dep: Dependency) -> bool:
    """Check if this dependency can be installed on the current distro."""
    distro = _detect_distro()
    if dep.kind == "system":
        packages = _PKG_MAP.get(dep.name, {}).get(distro, [])
        return len(packages) > 0
    return True


# ── Manifest (tracks what's in the overlay volume) ───────────────────


def _ensure_overlay_dir() -> None:
    """Ensure the overlay directory exists."""
    OVERLAY_ROOT.mkdir(parents=True, exist_ok=True)


def _read_manifest() -> dict:
    """Read the manifest file from the overlay volume."""
    try:
        if MANIFEST_PATH.exists():
            return json.loads(MANIFEST_PATH.read_text())
    except Exception as e:
        _log("failed to read manifest: %s", e)
    return {"installed": {}}


def _write_manifest(data: dict) -> None:
    """Write the manifest file to the overlay volume."""
    _ensure_overlay_dir()
    try:
        data["updated_at"] = datetime.utcnow().isoformat()
        MANIFEST_PATH.write_text(json.dumps(data, indent=2))
    except Exception as e:
        _log("failed to write manifest: %s", e)


def _record_install(
    dep: Dependency,
    deb_files: list[str],
    file_list: list[str],
) -> None:
    """Record an installation in the manifest."""
    manifest = _read_manifest()
    manifest.setdefault("installed", {})[dep.name] = {
        "deb_files": deb_files,
        "file_count": len(file_list),
        "installed_at": datetime.utcnow().isoformat(),
    }
    _write_manifest(manifest)
    _log(
        "recorded %s in manifest (%d files from %d debs)",
        dep.name,
        len(file_list),
        len(deb_files),
    )


def _record_uninstall(dep: Dependency) -> None:
    """Remove an installation from the manifest."""
    manifest = _read_manifest()
    if dep.name in manifest.get("installed", {}):
        del manifest["installed"][dep.name]
        _write_manifest(manifest)
        _log("removed %s from manifest", dep.name)


# ── Symlink fixing ───────────────────────────────────────────────────


def _fix_absolute_symlinks(root: Path) -> int:
    """Rewrite absolute symlinks that point to /usr/... to point to root/usr/....

    When packages are extracted to a non-standard root, absolute symlinks
    (e.g. /usr/bin/soffice -> /usr/lib/libreoffice/program/soffice) break
    because the target doesn't exist at /usr/lib/... — it's at
    root/usr/lib/... instead.

    This function walks the extracted tree and rewrites any absolute symlink
    starting with /usr/ to start with {root}/usr/ instead.
    """
    count = 0
    for item in root.rglob("*"):
        try:
            if not item.is_symlink():
                continue
            target = os.readlink(str(item))
            # Rewrite absolute /usr/... paths to {root}/usr/...
            if target.startswith("/usr/"):
                new_target = str(root) + target
                item.unlink()
                item.symlink_to(new_target)
                count += 1
            # Also handle /etc/ → {root}/etc/ (some packages symlink configs)
            elif target.startswith("/etc/"):
                new_target = str(root) + target
                if Path(new_target).exists():
                    item.unlink()
                    item.symlink_to(new_target)
                    count += 1
        except (OSError, PermissionError) as e:
            _log("warning: could not fix symlink %s: %s", item, e)
    return count


def _ensure_system_symlinks(dep: Dependency) -> None:
    """Create symlinks from system paths to overlay paths.

    This is needed for packages whose internal scripts hardcode system paths
    (e.g. LibreOffice looks for /usr/lib/libreoffice/program/soffice.bin).
    We create /usr/lib/libreoffice → /opt/optional/usr/lib/libreoffice so
    those hardcoded paths resolve.

    These symlinks live in the ephemeral container filesystem (not a volume),
    so they're recreated at startup and after each install.
    """
    for link in dep.system_links:
        sys_path = Path(link.system_path)
        overlay_path = Path(link.overlay_path)

        if not overlay_path.exists():
            _log("skip symlink %s → %s (overlay path missing)", sys_path, overlay_path)
            continue

        # If the system path already exists (real dir/file, not our symlink),
        # don't overwrite it — the package was installed in the base image.
        if sys_path.exists() and not sys_path.is_symlink():
            _log("skip symlink %s (already exists, not our link)", sys_path)
            continue

        # If it's our symlink already, make sure it points to the right place
        if sys_path.is_symlink():
            current = os.readlink(str(sys_path))
            if current == str(overlay_path):
                continue
            sys_path.unlink()

        try:
            sys_path.parent.mkdir(parents=True, exist_ok=True)
            sys_path.symlink_to(str(overlay_path))
            _log("created symlink %s → %s", sys_path, overlay_path)
        except (OSError, PermissionError) as e:
            _log("warning: could not create symlink %s: %s", sys_path, e)


def _remove_system_symlinks(dep: Dependency) -> None:
    """Remove the system symlinks created for this dependency."""
    for link in dep.system_links:
        sys_path = Path(link.system_path)
        if sys_path.is_symlink():
            try:
                current = os.readlink(str(sys_path))
                if current == link.overlay_path:
                    sys_path.unlink()
                    _log("removed symlink %s", sys_path)
            except OSError as e:
                _log("warning: could not remove symlink %s: %s", sys_path, e)


# ── apt-get download + dpkg-deb extract ──────────────────────────────


async def _run_command_streaming(
    cmd: str | list[str],
    stage: str,
    output_lines: list[str],
) -> AsyncGenerator[dict, None]:
    """Run a command and yield each line of output as an SSE event."""
    if isinstance(cmd, str):
        parts = shlex.split(cmd)
    else:
        parts = cmd

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


async def _apt_download(packages: list[str]) -> AsyncGenerator[dict, None]:
    """Download .deb files to the apt cache (uses cache if present).

    Yields progress events. The downloaded .debs end up in
    /var/cache/apt/archives/.
    """
    # First, update package lists (fast if /var/lib/apt/lists is a volume)
    yield {"stage": "updating", "output": "Updating package lists (apt-get update)..."}
    update_lines: list[str] = []
    async for event in _run_command_streaming(
        "env DEBIAN_FRONTEND=noninteractive apt-get update -qq",
        "updating",
        update_lines,
    ):
        if "output" in event:
            yield event

    # Download packages (does NOT install, just downloads to cache)
    pkgs_str = " ".join(shlex.quote(p) for p in packages)
    yield {"stage": "downloading", "output": f"Downloading {pkgs_str}..."}
    download_lines: list[str] = []
    rc = 0
    async for event in _run_command_streaming(
        f"env DEBIAN_FRONTEND=noninteractive apt-get install -d -y --no-install-recommends {pkgs_str}",
        "downloading",
        download_lines,
    ):
        if "_returncode" in event:
            rc = event["_returncode"]
        elif "output" in event:
            yield event

    if rc != 0:
        recent = "\n".join(download_lines[-20:])
        yield {
            "stage": "error",
            "error": f"apt-get download failed (exit code {rc}).",
            "output": recent,
        }


def _find_debs_for_packages(packages: list[str]) -> list[Path]:
    """Find .deb files in the cache that correspond to the given packages.

    Uses apt-cache depends --recurse to get the full transitive dependency
    list, then matches .deb filenames in the cache.
    """
    # Get full dependency list (package names only)
    try:
        result = subprocess.run(
            [
                "apt-cache",
                "depends",
                "--recurse",
                "--no-recommends",
                "--no-suggests",
                "--no-conflicts",
                "--no-breaks",
                "--no-replaces",
                "--no-enhances",
            ]
            + packages,
            capture_output=True,
            text=True,
            timeout=60,
        )
        # Parse: lines starting with a word (no leading whitespace) are package names
        needed = set()
        for line in result.stdout.split("\n"):
            line = line.strip()
            if line and not line.startswith(
                ("|", "Depends:", "Recommends:", "Suggests:")
            ):
                # Package names contain only [a-z0-9.+-]
                if all(c.isalnum() or c in ".+-" for c in line):
                    needed.add(line.split(":")[0])  # strip :amd64 etc.
        needed.update(packages)
    except Exception as e:
        _log("apt-cache depends failed: %s — will extract all cached debs", e)
        needed = set(packages)

    # Find matching .deb files in cache
    # .deb filename format: package_version_arch.deb
    debs = []
    if APT_CACHE_DIR.exists():
        for deb in APT_CACHE_DIR.glob("*.deb"):
            # Extract package name (before first _)
            deb_pkg = deb.name.split("_")[0]
            # Handle library package names with :arch suffix
            if deb_pkg in needed:
                debs.append(deb)

    _log(
        "found %d .deb files in cache for packages %s (needed: %d packages)",
        len(debs),
        packages,
        len(needed),
    )
    return debs


async def _extract_deb(deb_path: Path, root: Path) -> list[str]:
    """Extract a single .deb to the overlay root. Returns list of extracted files."""
    proc = await asyncio.create_subprocess_exec(
        "dpkg-deb",
        "--contents",
        str(deb_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    contents_stdout, _ = await proc.communicate()

    # Parse dpkg-deb --contents output:
    #   drwxr-xr-x root/root         0 2024-01-01 00:00 ./usr/bin/
    #   -rwxr-xr-x root/root    123456 2024-01-01 00:00 ./usr/bin/soffice
    files: list[str] = []
    for line in contents_stdout.decode(errors="replace").split("\n"):
        # Each line: permissions owner size date time path
        parts = line.split(None, 5)
        if len(parts) < 6:
            continue
        path = parts[5].strip()
        if path.startswith("./"):
            path = path[2:]  # Remove leading ./
        if path:
            files.append(path)

    # Actually extract
    proc = await asyncio.create_subprocess_exec(
        "dpkg-deb",
        "-x",
        str(deb_path),
        str(root),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    await proc.wait()

    return files


def _remove_files_from_overlay(files: list[str], root: Path) -> int:
    """Remove a list of files from the overlay. Returns count removed."""
    removed = 0
    for f in files:
        full = root / f
        try:
            if full.is_file() or full.is_symlink():
                full.unlink()
                removed += 1
            elif full.is_dir():
                # Only remove if empty (don't nuke shared dirs)
                try:
                    full.rmdir()
                    removed += 1
                except OSError:
                    pass  # Not empty — other packages use it
        except (OSError, PermissionError) as e:
            _log("warning: could not remove %s: %s", full, e)

    # Clean up empty parent directories
    _cleanup_empty_dirs(root)
    return removed


def _cleanup_empty_dirs(root: Path) -> None:
    """Remove empty directories from the overlay (bottom-up)."""
    # Walk the tree bottom-up
    all_dirs = sorted(
        [p for p in root.rglob("*") if p.is_dir()],
        key=lambda p: len(p.parts),
        reverse=True,
    )
    for d in all_dirs:
        try:
            if d != root:
                d.rmdir()  # Only succeeds if empty
        except OSError:
            pass  # Not empty — leave it


# ── Installation with SSE progress ───────────────────────────────────


async def install_dependency(
    dep: Dependency,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> AsyncGenerator[dict, None]:
    """Install a dependency to the overlay volume.

    Flow:
      1. apt-get install -d  (download .debs to cache, uses cache)
      2. dpkg-deb -x          (extract each .deb to /opt/optional/)
      3. Fix absolute symlinks
      4. Create system symlinks
      5. Update manifest

    Yields SSE events: downloading → extracting → linking → done.
    """
    distro = _detect_distro()

    if distro == "unknown":
        yield {
            "stage": "error",
            "error": "Could not detect OS distribution. Please install manually.",
        }
        return

    # ── Already in overlay? Skip everything. ──
    if _is_in_overlay(dep):
        _log("%s already in overlay — skipping install", dep.name)
        _ensure_system_symlinks(dep)
        yield {
            "stage": "done",
            "exit_code": 0,
            "output": f"{dep.display_name} is already installed in the overlay.",
            "version": get_version(dep),
        }
        return

    if dep.kind == "system":
        packages = _PKG_MAP.get(dep.name, {}).get(distro, [])
        if not packages:
            yield {
                "stage": "error",
                "error": f"{dep.display_name} is not available for {distro}.",
            }
            return

        # ── Step 1: Download .debs to cache ──
        yield {
            "stage": "downloading",
            "output": f"Downloading {dep.display_name} packages...",
        }
        download_ok = True
        async for event in _apt_download(packages):
            if event.get("stage") == "error":
                yield event
                download_ok = False
                break
            if "output" in event and progress_callback:
                progress_callback(event)
            if "output" in event:
                yield event

        if not download_ok:
            return

        # ── Step 2: Find and extract .debs ──
        deb_files = _find_debs_for_packages(packages)
        if not deb_files:
            yield {
                "stage": "error",
                "error": "Download succeeded but no .deb files found in cache.",
            }
            return

        _ensure_overlay_dir()
        all_files: list[str] = []
        total = len(deb_files)
        for i, deb in enumerate(deb_files, 1):
            yield {
                "stage": "extracting",
                "output": f"Extracting {deb.name} ({i}/{total})...",
            }
            try:
                files = await _extract_deb(deb, OVERLAY_ROOT)
                all_files.extend(files)
            except Exception as e:
                _log("failed to extract %s: %s", deb, e)

        yield {
            "stage": "extracting",
            "output": f"Extracted {len(all_files)} files from {total} packages.",
        }

        # ── Step 3: Fix absolute symlinks ──
        yield {"stage": "linking", "output": "Fixing symlinks..."}
        fixed = _fix_absolute_symlinks(OVERLAY_ROOT)
        yield {
            "stage": "linking",
            "output": f"Fixed {fixed} absolute symlinks.",
        }

        # ── Step 4: Create system symlinks ──
        if dep.system_links:
            yield {"stage": "linking", "output": "Creating system symlinks..."}
            _ensure_system_symlinks(dep)
            yield {
                "stage": "linking",
                "output": f"Created {len(dep.system_links)} system symlinks.",
            }

        # ── Step 5: Update manifest ──
        deb_names = [d.name for d in deb_files]
        _record_install(dep, deb_names, all_files)

        yield {
            "stage": "done",
            "exit_code": 0,
            "output": f"{dep.display_name} installed to overlay ({len(all_files)} files).",
            "version": get_version(dep),
        }

    elif dep.kind == "pip" and dep.pip_name:
        _ensure_overlay_dir()
        target = OVERLAY_ROOT / "usr" / "lib" / "python3" / "dist-packages"
        target.mkdir(parents=True, exist_ok=True)

        yield {
            "stage": "installing",
            "output": f"Installing {dep.display_name} via pip to {target}...",
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pip",
            "install",
            "--target",
            str(target),
            dep.pip_name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        output_lines: list[str] = []
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text:
                output_lines.append(text)
                yield {"stage": "installing", "output": text}

        await proc.wait()

        if proc.returncode == 0:
            _record_install(dep, [], [])  # pip doesn't give us a file list easily
            yield {
                "stage": "done",
                "exit_code": 0,
                "output": f"{dep.display_name} installed successfully.",
            }
        else:
            yield {
                "stage": "error",
                "exit_code": proc.returncode,
                "error": f"pip install failed (exit code {proc.returncode}).",
                "output": "\n".join(output_lines[-30:]),
            }


# ── Uninstallation ───────────────────────────────────────────────────


async def uninstall_dependency(
    dep: Dependency,
) -> AsyncGenerator[dict, None]:
    """Uninstall a dependency from the overlay.

    Removes all files that were extracted for this dependency, then
    removes the system symlinks and updates the manifest.
    """
    manifest = _read_manifest()
    entry = manifest.get("installed", {}).get(dep.name)

    if not entry and not _is_in_overlay(dep):
        yield {
            "stage": "error",
            "error": f"{dep.display_name} is not installed.",
        }
        return

    yield {
        "stage": "uninstalling",
        "output": f"Removing {dep.display_name} from overlay...",
    }

    # ── Step 1: Remove system symlinks ──
    _remove_system_symlinks(dep)
    yield {"stage": "uninstalling", "output": "Removed system symlinks."}

    # ── Step 2: Remove files from overlay ──
    # We need the file list. Try to reconstruct from cached .debs.
    deb_files = entry.get("deb_files", []) if entry else []
    removed_count = 0

    if deb_files:
        # Re-read file lists from cached .debs
        for deb_name in deb_files:
            deb_path = APT_CACHE_DIR / deb_name
            if deb_path.exists():
                try:
                    proc = await asyncio.create_subprocess_exec(
                        "dpkg-deb",
                        "--contents",
                        str(deb_path),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                    contents, _ = await proc.communicate()
                    files: list[str] = []
                    for line in contents.decode(errors="replace").split("\n"):
                        parts = line.split(None, 5)
                        if len(parts) >= 6:
                            path = parts[5].strip().lstrip("./")
                            if path:
                                files.append(path)
                    removed = _remove_files_from_overlay(files, OVERLAY_ROOT)
                    removed_count += removed
                except Exception as e:
                    _log("could not read deb contents for %s: %s", deb_name, e)
            else:
                _log(
                    "deb file not in cache: %s — cannot cleanly remove files", deb_name
                )
        yield {
            "stage": "uninstalling",
            "output": f"Removed {removed_count} files from overlay.",
        }
    else:
        # No deb file list — try to remove based on binary name
        bin_path = _overlay_bin_path(dep)
        if bin_path and bin_path.exists():
            try:
                bin_path.unlink()
                removed_count += 1
            except OSError:
                pass
        yield {
            "stage": "uninstalling",
            "output": f"Removed binary (no deb list; {removed_count} file(s)).",
        }

    # ── Step 3: Clean up empty directories ──
    _cleanup_empty_dirs(OVERLAY_ROOT)

    # ── Step 4: Update manifest ──
    _record_uninstall(dep)

    yield {
        "stage": "done",
        "exit_code": 0,
        "output": f"{dep.display_name} uninstalled ({removed_count} files removed).",
    }


# ── Startup: verify overlay + create symlinks (NO reinstall) ─────────


async def verify_overlay_on_startup() -> None:
    """Verify the overlay volume and create system symlinks.

    Called on backend startup. This is FAST (sub-second) because:
      - The overlay volume already has the extracted files
      - We just need to create ephemeral symlinks in /usr/...

    If the overlay is missing but the cache survives, we re-extract
    (fast, no download). If both are missing, we log a warning.
    """
    manifest = _read_manifest()
    installed = manifest.get("installed", {})

    if not installed:
        return

    for name, entry in installed.items():
        dep = get_dependency(name)
        if not dep:
            _log("manifest references unknown dep '%s' — skipping", name)
            continue

        if _is_in_overlay(dep):
            # Overlay intact — just create symlinks (instant!)
            _log("overlay OK for '%s' — creating system symlinks", name)
            _ensure_system_symlinks(dep)
            continue

        # Overlay missing — try to re-extract from cache
        deb_files = entry.get("deb_files", [])
        if deb_files:
            cached = [
                APT_CACHE_DIR / d for d in deb_files if (APT_CACHE_DIR / d).exists()
            ]
            if cached:
                _log(
                    "overlay missing for '%s' — re-extracting from cache (%d debs)",
                    name,
                    len(cached),
                )
                _ensure_overlay_dir()
                all_files: list[str] = []
                for deb in cached:
                    try:
                        files = await _extract_deb(deb, OVERLAY_ROOT)
                        all_files.extend(files)
                    except Exception as e:
                        _log("re-extract failed for %s: %s", deb, e)
                _fix_absolute_symlinks(OVERLAY_ROOT)
                _ensure_system_symlinks(dep)
                _log(
                    "re-extracted '%s' (%d files) — no download needed",
                    name,
                    len(all_files),
                )
                continue

        # Both overlay and cache missing — user must reinstall
        _log("WARNING: '%s' is in manifest but overlay + cache are both missing.", name)
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

        # Check if it's in the overlay vs system
        in_overlay = _is_in_overlay(dep)

        result.append(
            {
                "name": dep.name,
                "display_name": dep.display_name,
                "description": dep.description,
                "category": dep.category,
                "kind": dep.kind,
                "installed": installed,
                "in_overlay": in_overlay,
                "version": version,
                "install_size": dep.install_size,
                "enables": dep.enables,
                "available": available,
                "install_cmd": install_cmd,
                "distro": distro,
                "persistence": (
                    "overlay-volume"
                    if in_overlay
                    else ("system" if installed else "none")
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
