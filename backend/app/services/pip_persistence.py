"""Persistent pip installs — the pip twin of the LibreOffice apt strategy.

LibreOffice persists via the ``/var/cache/apt`` volume + a startup replay
(``apt-get install --no-download`` — offline from cached .debs). This module
gives PIP packages the same treatment:

* **Wheelhouse** — ``data/pip-wheels`` (the ``./data:/app/data`` bind mount
  persists it). Every voice-runtime pip install first populates the
  wheelhouse (``pip download -d``), then installs FROM it
  (``pip install --no-index --find-links``) — one download, cached forever.
* **Startup replay** — after a container rebuild the interpreter's
  site-packages are fresh but the wheelhouse survived. On startup we replay
  each manifest-recorded package offline (``--no-index --find-links``) and,
  only when that fails, with network + the persistent HTTP cache
  (``data/pip-cache``). Zero re-downloads for unchanged versions.
* **HTTP cache** — ``data/pip-cache`` (``--cache-dir``) keeps wheels warm
  for the rare online path.

The manifest of installed voice runtime packages lives in the SAME voice
manifest as the models (``data/models/voice/.manifest.json`` → ``runtime``
entry) so one file describes the whole persisted voice stack.
"""

from __future__ import annotations

import asyncio
import importlib.metadata
import importlib.util
import logging
import platform
import re
import shutil
import site
import sys
from pathlib import Path
from typing import AsyncGenerator, Dict, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)

_WHEELHOUSE_OVERRIDE: Optional[Path] = None
_PIP_CACHE_OVERRIDE: Optional[Path] = None
_SITE_PACKAGES_OVERRIDE: Optional[Path] = None

# Bump this when the pinned voice runtime set or its binary ABI assumptions
# change. Old targets remain harmless and can be removed by the user later.
VOICE_RUNTIME_LAYOUT_VERSION = "v2"


def _data_dir() -> Path:
    candidates = [
        Path("/app/data"),
        Path(__file__).resolve().parent.parent.parent.parent / "data",
    ]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    return Path("/tmp/realopen-data")


def set_dirs_for_testing(
    wheelhouse: Optional[Path],
    pip_cache: Optional[Path],
    site_packages: Optional[Path] = None,
) -> None:
    """Test hook: pin the wheelhouse + HTTP cache dirs (hermetic tests)."""
    global _WHEELHOUSE_OVERRIDE, _PIP_CACHE_OVERRIDE, _SITE_PACKAGES_OVERRIDE
    _WHEELHOUSE_OVERRIDE = Path(wheelhouse) if wheelhouse is not None else None
    _PIP_CACHE_OVERRIDE = Path(pip_cache) if pip_cache is not None else None
    _SITE_PACKAGES_OVERRIDE = Path(site_packages) if site_packages is not None else None


def runtime_tag() -> str:
    """Stable interpreter/platform tag used to isolate native wheels."""
    implementation = getattr(sys.implementation, "name", "python")
    pyver = f"{sys.version_info.major}{sys.version_info.minor}"
    system = platform.system().lower() or sys.platform
    machine = (platform.machine() or "unknown").lower().replace(" ", "_")
    return f"{implementation}-{pyver}-{system}-{machine}"


def persistent_site_packages_dir() -> Path:
    """Durable import target for the current Python/platform ABI."""
    if _SITE_PACKAGES_OVERRIDE is not None:
        return _SITE_PACKAGES_OVERRIDE
    return _data_dir() / "python" / VOICE_RUNTIME_LAYOUT_VERSION / runtime_tag()


def activate_persistent_site_packages() -> Path:
    """Make the durable target importable in this process and its children.

    This runs before the optional voice packages are imported. ``addsitedir``
    also processes any ``.pth`` files installed by dependencies. The target
    stays after the backend's locked site-packages so optional voice wheels
    cannot shadow FastAPI, Starlette, or other application dependencies.
    """
    target = persistent_site_packages_dir()
    target.mkdir(parents=True, exist_ok=True)
    target_str = str(target)
    if target_str not in sys.path:
        site.addsitedir(target_str)
    return target


def wheelhouse_dir() -> Path:
    """``data/pip-wheels`` — flat, persisted, offline-installable artifacts."""
    if _WHEELHOUSE_OVERRIDE is not None:
        return _WHEELHOUSE_OVERRIDE
    return _data_dir() / "pip-wheels" / runtime_tag()


def pip_cache_dir() -> Path:
    """``data/pip-cache`` — persisted pip HTTP cache (online fallback)."""
    if _PIP_CACHE_OVERRIDE is not None:
        return _PIP_CACHE_OVERRIDE
    return _data_dir() / "pip-cache"


def _canonical(name: str) -> str:
    """PEP 503 normalized project name (``Foo_Bar.baz`` → ``foo-bar-baz``)."""
    return re.sub(r"[-_.]+", "-", str(name).strip()).lower()


def wheelhouse_has(package: str) -> bool:
    """Whether the wheelhouse already holds an artifact for ``package``.

    pip artifacts spell project names with either ``-`` or ``_`` (PyPI
    stores both spellings), so the prefix is matched leniently across the
    PEP 503 separator characters.
    """
    house = wheelhouse_dir()
    if not house.is_dir():
        return False
    canon = _canonical(package)
    prefix = canon.replace("-", "[-_.]")
    pattern = re.compile(rf"^{prefix}[-_.]", re.IGNORECASE)
    try:
        return any(pattern.match(p.name) for p in house.iterdir())
    except OSError:
        return False


def _base_cmd() -> List[str]:
    return [sys.executable, "-m", "pip"]


def _constraint_args() -> List[str]:
    constraints = Path(__file__).with_name("voice-runtime-constraints.txt")
    return ["--constraint", str(constraints)] if constraints.is_file() else []


def _target_distributions(project: str) -> List[importlib.metadata.Distribution]:
    """Return matching distributions installed in the durable target only."""
    target = persistent_site_packages_dir()
    if not target.is_dir():
        return []
    expected = _canonical(project.split("[", 1)[0])
    return [
        dist
        for dist in importlib.metadata.distributions(path=[str(target)])
        if _canonical(dist.metadata.get("Name", "")) == expected
    ]


def _prepare_exact_reinstall(pip_name: str) -> None:
    """Remove stale metadata before replacing an exact-pinned target package.

    ``pip --target --upgrade`` overwrites package code but can leave older
    ``.dist-info`` directories behind. That makes version checks ambiguous
    and caused setup to keep reporting the superseded release. Removing only
    the matching metadata is safe; the following install replaces the code.
    """
    if "==" not in pip_name:
        return
    project, expected = (part.strip() for part in pip_name.split("==", 1))
    distributions = _target_distributions(project)
    versions = {dist.version for dist in distributions}
    if not distributions or versions == {expected}:
        return
    for dist in distributions:
        metadata_path = Path(getattr(dist, "_path", ""))
        try:
            if metadata_path.is_dir():
                shutil.rmtree(metadata_path)
            elif metadata_path.exists():
                metadata_path.unlink()
        except OSError as exc:
            logger.warning("could not remove stale metadata %s: %s", metadata_path, exc)


async def _stream_subprocess(
    cmd: Sequence[str], timeout: float
) -> AsyncGenerator[Tuple[str, Optional[int]], None]:
    """Run a command, yielding (line, None) per output line and (last_line,
    exit_code) at the end. Output lines are real pip output — never invented."""
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                break
            text = raw.decode(errors="replace").strip()
            if text:
                yield text, None
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
            yield f"timed out after {timeout:.0f}s", -1
            return
        yield f"exit code {rc}", rc
    except FileNotFoundError:
        yield "pip is not available in this environment", -1
        return
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass


async def pip_install_persistent(
    pip_name: str,
    extra_args: Sequence[str] = (),
    download_timeout_s: float = 900.0,
    install_timeout_s: float = 600.0,
) -> AsyncGenerator[str, None]:
    """Install ``pip_name`` so the artifacts PERSIST in the data volume.

    Strategy (mirrors deps_manager's apt strategy 1/2):
      1. Wheelhouse not populated → ``pip download -d <wheelhouse>`` (with
         any extra args, e.g. the torch CPU index).
      2. Offline install from the wheelhouse (``--no-index --find-links``)
         → return on success (works for both fresh and hit paths).
      3. On failure → online install with the persisted HTTP cache.

    Yields real pip output lines (for SSE ``pull_status`` / CLI echo).
    Raises RuntimeError when every strategy failed.
    """
    target = activate_persistent_site_packages()
    house = wheelhouse_dir()
    cache = pip_cache_dir()
    try:
        house.mkdir(parents=True, exist_ok=True)
        cache.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        logger.warning("pip persistence dirs unavailable: %s", e)

    _prepare_exact_reinstall(pip_name)

    def _offline_install() -> AsyncGenerator[str, None]:
        return _stream_subprocess(
            [
                *_base_cmd(),
                "install",
                "--no-index",
                "--find-links",
                str(house),
                "--progress-bar",
                "off",
                "--target",
                str(target),
                "--upgrade",
                pip_name,
                *_constraint_args(),
                *extra_args,
            ],
            install_timeout_s,
        )

    if wheelhouse_has(pip_name):
        yield f"wheelhouse hit — installing {pip_name} offline from {house}"
    else:
        yield (
            f"wheelhouse miss — downloading {pip_name} into {house} "
            "(one-time; persisted)"
        )
        dl_cmd = [
            *_base_cmd(),
            "download",
            "-d",
            str(house),
            "--no-cache-dir",
            "--progress-bar",
            "off",
            pip_name,
            *_constraint_args(),
            *extra_args,
        ]
        rc = None
        async for line, code in _stream_subprocess(dl_cmd, download_timeout_s):
            if code is not None:
                rc = code
            elif line:
                yield line[:400]
        if rc not in (0, None):
            yield f"pip download failed ({rc}) — trying a direct online install"
        elif wheelhouse_has(pip_name):
            yield "wheel artifacts saved — installing offline"

    # Offline install from the wheelhouse (hit, or after a fresh download).
    if wheelhouse_has(pip_name):
        rc2 = None
        async for line, code in _offline_install():
            if code is not None:
                rc2 = code
            elif line:
                yield line[:400]
        if rc2 == 0:
            yield f"✓ {pip_name} installed from the persistent wheelhouse"
            return
        yield (
            f"offline install failed ({rc2}) — retrying with network "
            "(persistent HTTP cache)"
        )

    # Online fallback (also the path when the wheelhouse stayed empty).
    online_cmd = [
        *_base_cmd(),
        "install",
        "--cache-dir",
        str(cache),
        "--progress-bar",
        "off",
        "--target",
        str(target),
        "--upgrade",
        pip_name,
        *_constraint_args(),
        *extra_args,
    ]
    rc = None
    async for line, code in _stream_subprocess(online_cmd, install_timeout_s):
        if code is not None:
            rc = code
        elif line:
            yield line[:400]
    if rc != 0:
        raise RuntimeError(f"pip install failed for {pip_name} (exit {rc})")
    yield f"✓ {pip_name} installed (network, cache at {cache})"


def module_importable(module_name: str) -> bool:
    """find_spec-only presence check — never imports heavy libs."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:  # noqa: BLE001 — ImportError for broken parents
        return False


def package_satisfied(pip_name: str, module_name: str) -> bool:
    """Return whether the module exists and an exact pin, when present, matches."""
    if not module_importable(module_name):
        return False
    if "==" not in pip_name:
        return True
    project, expected = (part.strip() for part in pip_name.split("==", 1))
    project = project.split("[", 1)[0]
    target_versions = {dist.version for dist in _target_distributions(project)}
    if target_versions:
        return target_versions == {expected}
    try:
        return importlib.metadata.version(project) == expected
    except importlib.metadata.PackageNotFoundError:
        return False


async def replay_voice_runtime_on_startup(
    packages: Sequence[Dict[str, object]],
    per_install_timeout_s: float = 600.0,
) -> List[str]:
    """Reinstall manifest-recorded pip packages missing after a rebuild.

    LibreOffice parity: offline-first (wheelhouse), network only as the
    fallback. Returns human-readable log lines; NEVER raises — a failed
    replay logs loudly and the setup wizard remains the explicit recovery
    path.
    """
    activate_persistent_site_packages()
    log: List[str] = []
    if not packages:
        return log
    for pkg in packages:
        pip_name = str(pkg.get("pip_name") or "").strip()
        module = str(pkg.get("module") or "").strip()
        display = str(pkg.get("display") or pip_name)
        extra = tuple(str(a) for a in (pkg.get("pip_extra_args") or []))
        if not pip_name:
            continue
        if module and package_satisfied(pip_name, module):
            log.append(f"[pip-replay] {display} already importable — skip")
            continue
        log.append(f"[pip-replay] {display} missing after rebuild — reinstalling")
        try:
            async for line in pip_install_persistent(
                pip_name, extra, install_timeout_s=per_install_timeout_s
            ):
                if line:
                    log.append(f"[pip-replay]   {line[:200]}")
            if module and not package_satisfied(pip_name, module):
                log.append(
                    f"[pip-replay] ERROR: {display} still not importable — "
                    "re-run the setup wizard"
                )
        except Exception as e:  # noqa: BLE001 — startup must never die
            log.append(f"[pip-replay] ERROR: {display}: {e}")
    for line in log:
        logger.info("%s", line)
    return log
