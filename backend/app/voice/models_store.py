"""Runtime voice-model readiness + manifest validation (read side).

The setup wizard (``app/services/voice_model_installer.py``) warms the
PERSISTED HuggingFace hub cache (``data/huggingface/hub`` — natural
downloads, exactly like running the packages on a laptop) and writes the
manifest; this module OWNS the read side used at runtime by the voice
session (and re-used by the installer for idempotency checks):

* :data:`MODELS_DIR` — ``<repo>/data/models/voice`` (the manifest home;
  same resolution as the installer: Docker ``/app/data`` first, then the
  repo-root ``data/``). The ``./data:/app/data`` bind mount persists it.
* The model WEIGHTS themselves live in the HF hub cache pinned by
  :mod:`app.voice.hf_cache` to ``<data>/huggingface/hub`` — the manifest
  records which repos/snapshots/files were warmed.
* :func:`voice_dependency_status` — the summary the setup wizard status
  endpoint prefers: ``{"enabled", "configured", "asr", "tts", "ready"}``
  with per-side ``provider`` / ``model`` / ``installed`` / ``valid``.
* :func:`asr_ready` / :func:`tts_ready` — the gates the voice WebSocket
  session checks before accepting a ``start`` frame.

Manifest — ``data/models/voice/.manifest.json``. Schema v3 ("hf" store —
the current one written by the natural installer)::

    {
      "asr": {
        "provider": "qwen3-asr", "model": "Qwen/Qwen3-ASR-0.6B",
        "revision": "main", "type": "asr", "role": "default_asr",
        "store": "hf",
        "hf_repos": {"Qwen/Qwen3-ASR-0.6B": "snapshots/<sha>"},
        "files": {"Qwen/Qwen3-ASR-0.6B::model.safetensors": 1876091704, ...},
        "total_bytes": ..., "complete": true, "installed_at": "..."
      },
      "tts": {
        "provider": "pocket-tts", "model": "pocket-tts",
        "language": "english_2026-04", "voice": "mary",
        "type": "tts", "role": "default_tts",
        "store": "hf",
        "hf_repos": {"kyutai/pocket-tts-without-voice-cloning": "snapshots/<sha>"},
        "files": {"kyutai/...::model.safetensors": 209709196, ...},
        "complete": true, "package_installed": true, "installed_at": "..."
      },
      "runtime": {  # pip packages — replayed at startup (pip persistence)
        "packages": [
          {"id": "pocket-tts", "module": "pocket_tts",
           "pip_name": "pocket-tts", "pip_extra_args": [],
           "display": "Pocket TTS runtime"}, ...
        ],
        "installed_at": "..."
      }
    }

Legacy schema (v2 / v1 — local files under ``data/models/voice/…``) stays
readable and valid where the runtime can still use it: ASR local snapshots
load fine (and get seeded into the HF cache on the next setup run); TTS
v2 layouts (``local-config.yaml`` + local weights) load through the
engine's legacy path. v1 TTS entries (weights only) are INVALID — the
engine cannot load offline from them.

Model selection ALWAYS comes from profiles.yml via
``settings.get_voice_config()`` — no hardcoded ids here.
"""

from __future__ import annotations

import importlib.util
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

# Manifest file name (schema shared with the setup-side installer).
MANIFEST_FILENAME = ".manifest.json"


def _default_models_dir() -> Path:
    """``<repo>/data/models/voice`` — robust to the process cwd.

    Same candidate order as ``voice_model_installer.get_data_dir()``:
    the Docker bind mount (``/app/data``) when writable/present, else the
    repository root's ``data/`` directory located relative to this file.
    """
    candidates = [
        Path("/app/data"),  # inside the Docker container (bind mount)
        Path(__file__).resolve().parent.parent.parent.parent / "data",
    ]
    for data_dir in candidates:
        # mkdir doubles as the writability probe (same as the installer).
        try:
            data_dir.mkdir(parents=True, exist_ok=True)
            return data_dir / "models" / "voice"
        except OSError:
            continue
    return Path("/tmp/realopen-data") / "models" / "voice"


# Voice model storage root. Module-level attribute so tests (and the
# installer's get_models_dir() probe) can pin it; reassigning it is safe
# because every accessor reads it at call time.
MODELS_DIR: Path = _default_models_dir()


def manifest_path() -> Path:
    """Path of the voice models manifest file."""
    return Path(MODELS_DIR) / MANIFEST_FILENAME


def load_manifest() -> dict:
    """Read the voice models manifest ({} when missing or corrupt)."""
    path = manifest_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read voice manifest %s: %s", path, e)
    return {}


def save_manifest(data: dict) -> None:
    """Write the manifest atomically (same format as the installer)."""
    path = manifest_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as e:
        logger.error("Failed to write voice manifest %s: %s", path, e)


# ── HF cache helpers (the current store) ─────────────────────────────


def _hf_repo_snapshot_root(repo_id: str, snapshot_name: str) -> Optional[Path]:
    """The snapshot directory ``<hub>/models--<org>--<name>/<snapshot_name>``,
    when it exists and is non-empty."""
    try:
        from app.voice.hf_cache import repo_cache_dir

        root = repo_cache_dir(repo_id) / snapshot_name
        if root.is_dir() and any(root.iterdir()):
            return root
    except Exception as e:  # noqa: BLE001 — cache probing never breaks gates
        logger.debug("hf snapshot probe failed for %s: %s", repo_id, e)
    return None


def _files_valid_hf(entry: dict) -> Tuple[bool, str]:
    """Validate a ``store: "hf"`` manifest entry against the hub cache.

    Every recorded file (``"<repo_id>::<relpath>"``) must exist in its
    recorded snapshot with the exact recorded size — a wiped or trimmed
    cache is detected honestly.
    """
    repos = entry.get("hf_repos")
    files = entry.get("files") or {}
    if not isinstance(repos, dict) or not repos or not files:
        return False, "manifest entry records no HF cache files"
    roots: Dict[str, Path] = {}
    for repo_id, snapshot_name in repos.items():
        root = _hf_repo_snapshot_root(str(repo_id), str(snapshot_name or ""))
        if root is None:
            return False, f"HF cache snapshot missing for {repo_id}"
        roots[str(repo_id)] = root
    for key, size in files.items():
        repo_id, _, rel = str(key).partition("::")
        root = roots.get(repo_id)
        if root is None:
            return False, f"file {rel!r} has no recorded snapshot"
        f = root / rel
        if not f.exists():
            return False, f"missing HF cache file: {rel}"
        try:
            if f.stat().st_size != int(size):
                return False, f"size mismatch: {rel}"
        except (OSError, TypeError, ValueError):
            return False, f"unreadable file: {rel}"
    return True, ""


def asr_snapshot_dir(entry: Optional[dict] = None) -> Optional[Path]:
    """The HF cache snapshot dir recorded for an ASR install, when valid.

    ``entry`` defaults to the manifest's ``asr`` side. Used by the
    transformers engine to load the snapshot as a plain local directory
    (no hub interaction at all).
    """
    if entry is None:
        entry = load_manifest().get("asr") or {}
    if not isinstance(entry, dict) or entry.get("store") != "hf":
        return None
    repos = entry.get("hf_repos")
    if not isinstance(repos, dict) or not repos:
        return None
    for repo_id, snapshot_name in repos.items():
        root = _hf_repo_snapshot_root(str(repo_id), str(snapshot_name or ""))
        if root is not None:
            return root
    return None


def _pocket_tts_importable() -> bool:
    """find_spec-only probe for the TTS runtime package (never imports)."""
    try:
        return importlib.util.find_spec("pocket_tts") is not None
    except Exception:  # noqa: BLE001 — broken parent package
        return False


# ── spec resolution (profiles.yml via settings) ──────────────────────


def _resolved_voice_config() -> Any:
    """The resolved VoiceConfig (duck-typed .asr/.tts) or None.

    Indirection exists so tests can pin the spec without touching
    profiles.yml.
    """
    try:
        from app.config import settings

        return settings.get_voice_config()
    except Exception as e:  # profiles.yml missing / unparsable
        logger.warning("voice config resolution failed: %s", e)
        return None


def _current_spec(kind: str) -> Optional[Any]:
    cfg = _resolved_voice_config()
    if cfg is None:
        return None
    spec = getattr(cfg, kind, None)
    if spec is None:
        return None
    if not str(getattr(spec, "provider", "") or "").strip():
        return None
    if not str(getattr(spec, "model", "") or "").strip():
        return None
    return spec


def _spec_from_arg(kind: str, arg: Any) -> Optional[Any]:
    """Accept either a whole VoiceConfig or the single spec."""
    if arg is None:
        return None
    spec = getattr(arg, kind, None)
    if spec is not None:
        return spec
    if getattr(arg, "provider", None) and getattr(arg, "model", None):
        return arg  # already a bare spec
    return None


# ── directory helpers (legacy local layouts; used by the installer) ──


def _dir_name(model_id: str) -> str:
    return str(model_id).split("/")[-1] or str(model_id)


def asr_dir(spec: Any = None) -> Path:
    """Legacy ASR snapshot dir: ``MODELS_DIR/asr/<model-name>``."""
    model_id = str(getattr(spec, "model", "") or "") if spec is not None else ""
    return Path(MODELS_DIR) / "asr" / (_dir_name(model_id) if model_id else "")


def tts_dir(spec: Any = None) -> Path:
    """Legacy TTS asset dir: ``MODELS_DIR/tts/<model-name>``."""
    model_id = str(getattr(spec, "model", "") or "") if spec is not None else ""
    return Path(MODELS_DIR) / "tts" / (_dir_name(model_id) if model_id else "")


# ── validation (manifest entry + files on disk / in the cache) ───────


def _entry_matches(kind: str, spec: Any, entry: dict) -> bool:
    """True when the manifest entry was written for this exact selection.

    Mirrors voice_model_installer._entry_matches: provider+model always;
    revision for ASR; language for TTS (voice changes never re-download).
    """
    if entry.get("model") != getattr(spec, "model", None):
        return False
    if entry.get("provider") != getattr(spec, "provider", None):
        return False
    if kind == "asr" and entry.get("revision") != (
        getattr(spec, "revision", None) or "main"
    ):
        return False
    if kind == "tts" and entry.get("language") != getattr(spec, "language", None):
        return False
    return True


def _files_valid_local(kind: str, entry: dict) -> Tuple[bool, str]:
    """Validate a LEGACY manifest entry against the local models dir."""
    rel = entry.get("path")
    files = entry.get("files") or {}
    if not rel:
        return False, "manifest entry has no path"
    if kind == "tts" and not files:
        # TTS entries MUST record real asset files — the runtime never
        # downloads at first use (task spec §21). Empty file list → the
        # setup-side materialization failed → NOT valid.
        return False, "TTS assets not installed — run the setup wizard"
    if kind == "tts" and (
        _as_int(entry.get("assets_schema")) < 2 or "local-config.yaml" not in files
    ):
        # v1 layouts (weight file only, no local config) cannot load
        # offline — re-run the wizard (the natural HF install replaces it).
        return (
            False,
            "TTS assets use the legacy v1 layout (no local-config.yaml) — "
            "re-run the setup wizard to install into the HF cache",
        )
    root = Path(MODELS_DIR) / rel
    if not files:
        return False, "manifest entry records no files"
    for fname, size in files.items():
        f = root / fname
        if not f.exists():
            return False, f"missing file: {fname}"
        try:
            if f.stat().st_size != int(size):
                return False, f"size mismatch: {fname}"
        except (OSError, TypeError, ValueError):
            return False, f"unreadable file: {fname}"
    return True, ""


def _files_valid(kind: str, entry: dict) -> Tuple[bool, str]:
    """Dispatch validation by the entry's store (hf cache vs legacy local)."""
    if entry.get("store") == "hf":
        return _files_valid_hf(entry)
    if entry.get("store") == "package":
        if kind != "tts":
            return False, "package-backed store is only supported for TTS"
        return (
            (True, "")
            if _pocket_tts_importable()
            else (False, "runtime package missing (pocket-tts)")
        )
    return _files_valid_local(kind, entry)


def _as_int(value: Any) -> int:
    """Best-effort int coercion (manifest values may be str/None)."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1


def _side_status(kind: str, spec: Optional[Any]) -> dict:
    """Per-side status dict: provider/model/installed/valid (+ extras)."""
    out = {
        "provider": getattr(spec, "provider", None) if spec else None,
        "model": getattr(spec, "model", None) if spec else None,
        "revision": getattr(spec, "revision", None) if spec else None,
        "language": getattr(spec, "language", None) if spec else None,
        "voice": getattr(spec, "voice", None) if spec else None,
        "configured": spec is not None,
        "installed": False,
        "valid": False,
    }
    if spec is None:
        out["reason"] = "not_configured"
        return out
    entry = load_manifest().get(kind)
    if not isinstance(entry, dict) or not entry:
        out["reason"] = "not_installed"
        return out
    if not entry.get("complete"):
        out["reason"] = "incomplete"
        return out
    if not _entry_matches(kind, spec, entry):
        out["reason"] = "changed"
        return out
    if entry.get("store") not in {"hf", "package"}:
        out["reason"] = (
            "legacy voice install detected — run setup to migrate it to the "
            "persistent Hugging Face cache"
        )
        out["legacy"] = True
        return out
    ok, why = _files_valid(kind, entry)
    if not ok:
        out["reason"] = why
        return out
    if kind == "tts" and not _pocket_tts_importable():
        # Natural HF-store TTS needs the pocket_tts package importable —
        # the pip persistence layer replays it at startup, the wizard
        # installs it during setup. Missing → honest "not ready".
        out["reason"] = "runtime package missing (pocket-tts)"
        return out
    out["installed"] = True
    out["valid"] = True
    out["store"] = str(entry.get("store") or "local")
    return out


# ── public readiness API ─────────────────────────────────────────────


def tts_assets_dir() -> Optional[Path]:
    """LEGACY v2 TTS asset directory, when valid (compat with pre-cache
    installs — the engine's offline fallback). None for HF-store installs
    (the natural hub-cache path is used instead)."""
    entry = load_manifest().get("tts")
    if not isinstance(entry, dict) or not entry:
        return None
    if entry.get("store") == "hf":
        return None  # current installs live in the HF cache
    rel = entry.get("path")
    if not rel:
        return None
    root = Path(MODELS_DIR) / rel
    files = entry.get("files") or {}
    if not files:
        return None
    if "local-config.yaml" not in files and not (root / "local-config.yaml").exists():
        # v1 layout (weights only) — the engine cannot load offline from it.
        return None
    if not all((root / fname).exists() for fname in files):
        return None
    return root


def asr_ready() -> bool:
    """ASR assets installed AND valid for the CURRENT profiles.yml
    selection — the gate the voice session checks on ``start``."""
    return _side_status("asr", _current_spec("asr"))["valid"]


def tts_ready() -> bool:
    """TTS assets installed AND valid for the CURRENT selection."""
    return _side_status("tts", _current_spec("tts"))["valid"]


def runtime_manifest_entry() -> Optional[dict]:
    """The manifest's ``runtime`` entry (pip packages for startup replay)."""
    entry = load_manifest().get("runtime")
    return entry if isinstance(entry, dict) else None


def voice_dependency_status() -> dict:
    """Voice readiness summary (the shape ``app/api/setup.py`` prefers).

    ``{"enabled": bool, "configured": {...}, "asr": {...}, "tts": {...},
    "ready": bool}`` — each side dict carries provider / model / installed
    / valid (+ a human ``reason`` when not ready); ``ready`` is True only
    when BOTH sides are installed AND valid for the current selection.
    """
    asr_spec = _current_spec("asr")
    tts_spec = _current_spec("tts")
    asr_status = _side_status("asr", asr_spec)
    tts_status = _side_status("tts", tts_spec)
    try:
        from app.config import settings

        enabled = bool(settings.VOICE_ENABLED)
    except Exception:
        enabled = True
    runtime = _missing_runtime_entries()

    ready = bool(asr_status["valid"] and tts_status["valid"] and not runtime)
    out = {
        "enabled": enabled,
        "configured": {
            "asr": asr_spec is not None,
            "tts": tts_spec is not None,
        },
        "asr": asr_status,
        "tts": tts_status,
        "runtime": runtime,
        "ready": ready,
    }
    try:
        from app.voice.hf_cache import hub_cache_dir

        out["hf_cache"] = str(hub_cache_dir())
    except Exception:  # noqa: BLE001 — informational only
        pass
    return out


def _missing_runtime_entries() -> list:
    """Missing/outdated configured runtime packages (cheap import/version probe)."""
    try:
        from app.services.voice_model_installer import voice_runtime_entries

        return voice_runtime_entries()
    except Exception as e:  # noqa: BLE001 — status must remain available
        logger.debug("voice runtime readiness probe failed: %s", e)
        return [{"id": "unknown", "description": str(e)}]


# ── installer hooks (optional; probed via getattr by the installer) ──


def validate_asr_install(cfg: Any = None) -> Optional[bool]:
    """Manifest validation hook for voice_model_installer.

    Accepts either a VoiceConfig-like object (``.asr``) or a bare ASR
    spec. Returns True/False for the GIVEN selection, or None when the
    check could not be applied (no spec → let file validation decide).
    """
    spec = _spec_from_arg("asr", cfg) if cfg is not None else _current_spec("asr")
    if spec is None:
        return None
    return _side_status("asr", spec)["valid"]


def validate_tts_install(cfg: Any = None) -> Optional[bool]:
    """TTS counterpart of :func:`validate_asr_install`."""
    spec = _spec_from_arg("tts", cfg) if cfg is not None else _current_spec("tts")
    if spec is None:
        return None
    return _side_status("tts", spec)["valid"]
