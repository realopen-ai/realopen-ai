"""Hugging Face cache ownership — the hub cache lives in the data volume.

Design (user directive, 2026-09-19): let HuggingFace handle model downloads
naturally — ``mlx_qwen3_asr`` (``Session(model="Qwen/…")``), ``pocket_tts``
(``TTSModel.load_model(language=…)``) and ``transformers`` all resolve their
weights through the ``huggingface_hub`` cache — and MOUNT that cache inside
the persisted data directory so models survive container rebuilds::

    Docker:  HF_HOME=/app/data/huggingface   (the ./data:/app/data bind mount)
    Dev:     <repo>/data/huggingface         (same directory the bind mount maps)

The setup wizard warms this cache ONCE (natural downloads, real progress);
afterwards every engine load is a local cache hit — no network, no
"weights being pulled" mid-conversation, no app hang.

Public surface (stdlib only — importable before huggingface_hub):

    ensure_hf_env()      → set HF_HOME (+patch hub constants if already imported)
    hf_home()            → <data>/huggingface
    hub_cache_dir()      → <data>/huggingface/hub  (models--<org>--<name>/…)
    repo_cache_dir(id)   → hub_cache_dir()/"models--<org>--<name>"
    snapshot_dir(repo, revision) → the cached snapshot path for a repo+rev
    offline_hub()        → context manager: engine loads NEVER hit the network
    run_monitored(...)   → run a blocking download while watching dir sizes
    dir_size_bytes(p)    → honest byte size of a directory tree

Offline policy (task spec §21 — no first-use downloads): RUNTIME engine
loads run inside ``offline_hub()``. A warm cache loads instantly; a cold
cache raises immediately with a clear "re-run the setup wizard" error
instead of silently downloading gigabytes mid-voice-turn (the original
"app hangs while weights are pulled" bug). SETUP-side warm-ups never use
``offline_hub()`` — that is exactly where the natural downloads belong.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence

logger = logging.getLogger(__name__)

# Env var pinning for tests / unusual deployments (never a selection source).
_HF_HOME_OVERRIDE: Optional[str] = os.environ.get("REALOPEN_HF_HOME")


def _data_dir() -> Path:
    """The persisted data directory (Docker ``/app/data`` first, dev repo
    ``data/`` second) — same resolution order as models_store / the
    installer, so every module agrees on the location by construction."""
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


def set_hf_home_for_testing(path: Optional[Path]) -> None:
    """Test hook: pin the HF home (keeps tests hermetic)."""
    global _HF_HOME_OVERRIDE
    _HF_HOME_OVERRIDE = str(path) if path is not None else None


def hf_home() -> Path:
    """``<data>/huggingface`` — the persisted HF cache root."""
    if _HF_HOME_OVERRIDE is not None:
        return Path(_HF_HOME_OVERRIDE)
    # An explicitly-set HF_HOME (docker-compose / operator) wins.
    env_home = os.environ.get("HF_HOME")
    if env_home:
        return Path(env_home)
    return _data_dir() / "huggingface"


def hub_cache_dir() -> Path:
    """``<hf_home>/hub`` — where models--<org>--<name> repos live."""
    return hf_home() / "hub"


def repo_cache_dir(repo_id: str) -> Path:
    """Hub cache directory for one repo (``models--<org>--<name>``)."""
    return hub_cache_dir() / ("models--" + str(repo_id).replace("/", "--"))


def ensure_hf_env() -> Path:
    """Point the HF ecosystem at the persisted cache — BEFORE first use.

    * ``HF_HOME`` is set (setdefault — an explicit operator value wins).
    * If ``huggingface_hub`` was ALREADY imported in this process (constants
      are resolved at import time), its ``constants`` are patched so cache
      resolution honors our HF_HOME anyway.
    """
    home = hf_home()
    # `hf auth login` writes to the CLI's standard user cache. RealOpen pins
    # HF_HOME to the project data directory so model weights survive Docker
    # rebuilds, which otherwise makes huggingface_hub stop seeing that login.
    # Bridge the active CLI token into this process only; do not copy secrets
    # into the repository/persisted model directory and never overwrite an
    # operator-provided HF_TOKEN.
    if not os.environ.get("HF_TOKEN"):
        cli_token = Path.home() / ".cache" / "huggingface" / "token"
        try:
            token = cli_token.read_text(encoding="utf-8").strip()
            if token:
                os.environ["HF_TOKEN"] = token
        except OSError:
            pass
    try:
        home.mkdir(parents=True, exist_ok=True)
    except OSError as e:  # read-only data dir → hub falls back gracefully
        logger.warning("cannot create HF cache dir %s: %s", home, e)
    os.environ.setdefault("HF_HOME", str(home))
    hub = sys.modules.get("huggingface_hub")
    if hub is not None:
        consts = getattr(hub, "constants", None)
        if consts is not None:
            for attr in ("HF_HUB_CACHE", "HUGGINGFACE_HUB_CACHE"):
                try:
                    setattr(consts, attr, str(hub_cache_dir()))
                except Exception:  # noqa: BLE001 — best-effort patch
                    pass
    return home


def snapshot_dir(repo_id: str, revision: str = "main") -> Optional[Path]:
    """The cached snapshot directory for ``repo_id@revision``, when complete.

    Resolves the hub layout directly: ``refs/<rev>`` holds the commit sha,
    ``snapshots/<sha>/`` the materialized files. Returns None when the repo
    is not cached at that revision (caller falls back to natural loading).
    """
    root = repo_cache_dir(repo_id)
    ref = root / "refs" / (revision or "main")
    try:
        if not ref.is_file():
            return None
        sha = ref.read_text(encoding="utf-8").strip()
        if not sha:
            return None
        snap = root / "snapshots" / sha
        if snap.is_dir() and any(snap.iterdir()):
            return snap
    except OSError:
        return None
    return None


def snapshot_files(repo_id: str, revision: str = "main") -> dict:
    """{relative_path: size} of every file in the cached snapshot
    (symlinks followed — the sizes are the real blob sizes)."""
    snap = snapshot_dir(repo_id, revision)
    out: dict = {}
    if snap is None:
        return out
    for p in snap.rglob("*"):
        try:
            if p.is_file():
                out[p.relative_to(snap).as_posix()] = p.stat().st_size
        except OSError:
            continue
    return out


def dir_size_bytes(path: Path) -> int:
    """Honest byte size of a directory tree (symlinks NOT followed — blob
    directories hold the real downloads; snapshot links stay ~0)."""
    total = 0
    try:
        with os.scandir(path) as it:
            stack = list(it)
        while stack:
            entry = stack.pop()
            try:
                if entry.is_dir(follow_symlinks=False):
                    with os.scandir(entry.path) as sub:
                        stack.extend(sub)
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue
    except (OSError, NotADirectoryError):
        return 0
    return total


@contextlib.contextmanager
def offline_hub():
    """Run engine model loads with the hub OFFLINE (no network).

    A warm cache is an instant local load; a cold cache raises a clear
    ``LocalEntryNotFoundError`` / ``OfflineModeIsEnabled`` immediately —
    mapped by the engines to a recoverable "re-run the setup wizard" error.
    Never used for setup-side warm-ups (downloads belong there).
    """
    saved_env = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "1"
    hub = sys.modules.get("huggingface_hub")
    consts = getattr(hub, "constants", None) if hub is not None else None
    saved_const = getattr(consts, "HF_HUB_OFFLINE", None) if consts else None
    if consts is not None:
        try:
            consts.HF_HUB_OFFLINE = True
        except Exception:  # noqa: BLE001 — best-effort patch
            pass
    try:
        yield
    finally:
        if saved_env is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = saved_env
        if consts is not None and saved_const is not None:
            try:
                consts.HF_HUB_OFFLINE = saved_const
            except Exception:  # noqa: BLE001
                pass


def suppress_hub_progress_bars() -> None:
    """Keep hub download progress bars off our stderr (we emit our own
    byte-accurate SSE progress instead)."""
    os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
    hub = sys.modules.get("huggingface_hub")
    consts = getattr(hub, "constants", None) if hub is not None else None
    if consts is not None:
        try:
            setattr(consts, "HF_HUB_DISABLE_PROGRESS_BARS", True)
        except Exception:  # noqa: BLE001
            pass


async def run_monitored(
    fn: Callable[[], Any],
    watch_dirs: Sequence[Path],
    on_progress: Callable[[int], None],
    interval_s: float = 0.3,
) -> Any:
    """Run blocking ``fn`` in a thread while watching ``watch_dirs`` sizes.

    ``on_progress(total_bytes)`` fires (from the event loop) whenever the
    watched directories grow — REAL bytes on disk (including in-flight
    ``*.incomplete`` blobs), never invented percentages. Re-raises whatever
    ``fn`` raised. Used by the setup wizard to give the natural HF
    downloads honest progress reporting.
    """
    stop = threading.Event()
    watch: List[Path] = [Path(d) for d in watch_dirs]

    def _watch_loop() -> None:
        last = -1
        while not stop.wait(interval_s):
            total = sum(dir_size_bytes(d) for d in watch)
            if total != last:
                last = total

                def _emit(value: int) -> None:
                    try:
                        on_progress(value)
                    except Exception:  # noqa: BLE001 — progress is best-effort
                        pass

                _emit(total)

    watcher = threading.Thread(target=_watch_loop, daemon=True, name="hf-size-watch")
    watcher.start()
    try:
        return await asyncio.to_thread(fn)
    finally:
        stop.set()
        watcher.join(timeout=2.0)


def is_offline() -> bool:
    """Whether the hub is currently pinned offline (tests/logging)."""
    return os.environ.get("HF_HUB_OFFLINE", "").lower() in ("1", "true", "yes")


def human_mb(nbytes: int) -> str:
    """Byte count → ``"123.4 MB"`` (status lines only)."""
    return f"{nbytes / (1024 * 1024):.1f} MB"
