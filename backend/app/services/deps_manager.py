"""
Dependency management service — detects, lists, and installs optional
system-level dependencies.

- Catalog of known dependencies with metadata
- Detection via shutil.which / subprocess probes
- Installation via subprocess (apt-get / dnf / pacman / brew)
- SSE progress streaming during installation
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncGenerator, Callable, Optional

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[deps] {formatted}", flush=True)


# ── Dependency catalog ───────────────────────────────────────────────


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
    ),
]


# ── Detection ────────────────────────────────────────────────────────


def _is_system_dep_installed(dep: Dependency) -> bool:
    """Check if a system binary is installed."""
    if not dep.binary_name:
        return False
    return shutil.which(dep.binary_name) is not None


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
                    elif distro_id in ("arch", "manjaro", " Endeavouros"):
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
        "alpine": [],  # No official package — user must install manually
        "macos": ["libreoffice"],  # brew install --cask libreoffice
    },
}

# Package manager commands per distro
_PKG_MGR: dict[str, str] = {
    "debian": "env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {pkgs}",
    "fedora": "sudo -n dnf install -y {pkgs}",
    "arch": "sudo -n pacman -S --needed --noconfirm {pkgs}",
    "suse": "sudo -n zypper install -n {pkgs}",
    "alpine": "sudo -n apk add {pkgs}",
    "macos": "brew install --cask {pkgs}",
}

# Package manager update commands (run before install)
_PKG_UPDATE: dict[str, str] = {
    "debian": "env DEBIAN_FRONTEND=noninteractive apt-get update -qq",
    "fedora": "sudo -n dnf check-update",
    "arch": "sudo -n pacman -Sy",
    "suse": "sudo -n zypper refresh",
    "alpine": "sudo -n apk update",
    "macos": "brew update",
}


def get_install_command(dep: Dependency) -> Optional[str]:
    """Get the shell command that would install this dependency."""
    distro = _detect_distro()
    if dep.kind == "system":
        packages = _PKG_MAP.get(dep.name, {}).get(distro, [])
        if not packages:
            return None
        print(f"==== packages: {packages} ----")
        pkgs_str = " ".join(shlex.quote(p) for p in packages)
        mgr = _PKG_MGR.get(distro)
        print(f"==== mgr: {mgr} ----")
        if mgr:
            return mgr.format(pkgs=pkgs_str)
    elif dep.kind == "pip" and dep.pip_name:
        return f"{sys.executable} -m pip install {dep.pip_name}"
    return None


def is_available_for_distro(dep: Dependency) -> bool:
    """Check if this dependency can be installed on the current distro."""
    distro = _detect_distro()
    if dep.kind == "system":
        packages = _PKG_MAP.get(dep.name, {}).get(distro, [])
        return len(packages) > 0
    return True  # pip deps are always available


# ── Installation with SSE progress ───────────────────────────────────


async def install_dependency(
    dep: Dependency,
    progress_callback: Optional[Callable[[dict], None]] = None,
) -> AsyncGenerator[dict, None]:
    """Install a dependency, yielding progress events.

    Yields dicts with:
        {stage: "checking_sudo" | "updating" | "installing" | "done" | "error",
         output: str (last few lines of output),
         exit_code: int (for done/error)}

    The caller should forward these as SSE events.
    """
    distro = _detect_distro()

    print(f"==== distro: {distro} ----")
    print(f"==== dep: {dep} ----")

    if distro == "unknown":
        yield {
            "stage": "error",
            "error": "Could not detect OS distribution. Please install manually.",
        }
        return

    if dep.kind == "system":
        packages = _PKG_MAP.get(dep.name, {}).get(distro, [])
        if not packages:
            yield {
                "stage": "error",
                "error": f"LibreOffice is not available for {distro}. Please install manually.",
            }
            return

        # Build the install script
        update_cmd = _PKG_UPDATE.get(distro, "")
        mgr = _PKG_MGR.get(distro)
        if not mgr:
            yield {"stage": "error", "error": f"No package manager found for {distro}."}
            return

        pkgs_str = " ".join(shlex.quote(p) for p in packages)
        install_cmd = mgr.format(pkgs=pkgs_str)

        print(f"==== install_cmd: {install_cmd} ----")

        needs_sudo = False

        if needs_sudo:
            print("=== checking sudo access...")
            # Check if passwordless sudo is available
            yield {"stage": "checking_sudo", "output": "Checking sudo access..."}
            sudo_check = await asyncio.create_subprocess_exec(
                "sudo",
                "-n",
                "true",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await sudo_check.communicate()
            print(f"sudo check == {sudo_check.returncode}")
            if sudo_check.returncode != 0:
                install_cmd_manual = install_cmd
                yield {
                    "stage": "error",
                    "error": f"Passwordless sudo is not available. Please run this command manually in a terminal:\n\n{install_cmd_manual}",
                    "manual_command": install_cmd_manual,
                }
                return

        # Run package manager update
        if update_cmd:
            yield {"stage": "updating", "output": f"Updating package lists..."}
            update_parts = shlex.split(update_cmd)
            update_proc = await asyncio.create_subprocess_exec(
                *update_parts,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            # Stream output
            while True:
                line = await update_proc.stdout.readline()
                if not line:
                    break
                text = line.decode(errors="replace").strip()
                if text and progress_callback:
                    progress_callback({"stage": "updating", "output": text})
                if text:
                    yield {"stage": "updating", "output": text}
            await update_proc.wait()

        # Run install
        yield {"stage": "installing", "output": f"Installing {dep.display_name}..."}
        install_parts = shlex.split(install_cmd)
        install_proc = await asyncio.create_subprocess_exec(
            *install_parts,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

        # Stream install output line by line
        output_lines: list[str] = []
        while True:
            line = await install_proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text:
                output_lines.append(text)
                # Keep only last 20 lines for the event payload
                recent = "\n".join(output_lines[-20:])
                if progress_callback:
                    progress_callback({"stage": "installing", "output": text})
                yield {"stage": "installing", "output": text, "recent_output": recent}

        await install_proc.wait()

        if install_proc.returncode == 0:
            yield {
                "stage": "done",
                "exit_code": 0,
                "output": f"{dep.display_name} installed successfully.",
                "version": get_version(dep),
            }
        else:
            recent = "\n".join(output_lines[-30:])
            yield {
                "stage": "error",
                "exit_code": install_proc.returncode,
                "error": f"Installation failed (exit code {install_proc.returncode}).",
                "output": recent,
            }

    elif dep.kind == "pip" and dep.pip_name:
        yield {
            "stage": "installing",
            "output": f"Installing {dep.display_name} via pip...",
        }
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "pip",
            "install",
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
                "version": version,
                "install_size": dep.install_size,
                "enables": dep.enables,
                "available": available,
                "install_cmd": install_cmd,
                "distro": distro,
            }
        )
    return result


def get_dependency(name: str) -> Optional[Dependency]:
    """Get a Dependency by name from the catalog."""
    for dep in CATALOG:
        if dep.name == name:
            return dep
    return None
