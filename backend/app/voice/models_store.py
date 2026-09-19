"""Runtime voice-model readiness + manifest validation (read side).

The setup wizard (``app/services/voice_model_installer.py``) DOWNLOADS
the ASR/TTS assets and writes the manifest; this module OWNS the read
side used at runtime by the voice session (and re-used by the installer
for idempotency checks):

* :data:`MODELS_DIR` — ``<repo>/data/models/voice`` (same resolution as
  ``voice_model_installer.get_data_dir()``: Docker ``/app/data`` first,
  then the repo-root ``data/``, cwd-independent). The ``./data:/app/data``
  bind mount persists it across container rebuilds.
* :func:`load_manifest` / :func:`save_manifest` — read/write
  ``data/models/voice/.manifest.json`` (the exact schema the installer
  writes — see its module docstring).
* :func:`voice_dependency_status` — the summary the setup wizard status
  endpoint prefers: ``{"enabled", "configured", "asr", "tts", "ready"}``
  with per-side ``provider`` / ``model`` / ``installed`` / ``valid``.
* :func:`asr_ready` / :func:`tts_ready` — the gates the voice WebSocket
  session checks before accepting a ``start`` frame.

Model selection ALWAYS comes from profiles.yml via
``settings.get_voice_config()`` — no hardcoded ids here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional, Tuple

logger = logging.getLogger(__name__)

# Manifest file name (schema shared with the setup-side installer and the
# host-side scripts/install-voice-models.py writer).
MANIFEST_FILENAME = ".manifest.json"


def _default_models_dir() -> Path:
    """``<repo>/data/models/voice`` — robust to the process cwd.

    Same candidate order as ``voice_model_installer.get_data_dir()``:
    the Docker bind mount (``/app/data``) when writable/present, else the
    repository root's ``data/`` directory located relative to this file
    (backend/app/voice/models_store.py → repo root = four parents up).
    There is no REALOPEN_DATA_DIR override in the installer, so none is
    used here either — both modules must agree on the location.
    """
    candidates = [
        Path("/app/data"),  # inside the Docker container (bind mount)
        Path(__file__).resolve().parent.parent.parent.parent / "data",
    ]
    for data_dir in candidates:
        # mkdir doubles as the writability probe (same as the installer);
        # a fresh dev checkout simply gets the directory created.
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


# ── directory helpers (used by the setup-side installer via getattr) ──


def _dir_name(model_id: str) -> str:
    return str(model_id).split("/")[-1] or str(model_id)


def asr_dir(spec: Any = None) -> Path:
    """ASR snapshot dir: ``MODELS_DIR/asr/<model-name>``.

    Optional hook consumed by ``voice_model_installer.asr_target_dir``
    so the installer and the runtime agree on the layout by construction.
    """
    model_id = str(getattr(spec, "model", "") or "") if spec is not None else ""
    return Path(MODELS_DIR) / "asr" / (_dir_name(model_id) if model_id else "")


def tts_dir(spec: Any = None) -> Path:
    """TTS asset dir: ``MODELS_DIR/tts/<model-name>``."""
    model_id = str(getattr(spec, "model", "") or "") if spec is not None else ""
    return Path(MODELS_DIR) / "tts" / (_dir_name(model_id) if model_id else "")


# ── validation (manifest entry + files on disk) ──────────────────────


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


def _files_valid(kind: str, entry: dict) -> Tuple[bool, str]:
    """Validate the files recorded in a manifest entry against disk.

    Mirrors voice_model_installer._files_valid (same reasons strings).
    """
    rel = entry.get("path")
    files = entry.get("files") or {}
    if not rel:
        return False, "manifest entry has no path"
    if kind == "tts" and not files and not entry.get("preloaded"):
        # pocket-tts materializes its cache on first load — the
        # package_installed / preloaded flags carry the state instead.
        return True, ""
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
    ok, why = _files_valid(kind, entry)
    if not ok:
        out["reason"] = why
        return out
    out["installed"] = True
    out["valid"] = True
    return out


# ── public readiness API ─────────────────────────────────────────────


def asr_ready() -> bool:
    """ASR assets installed AND valid for the CURRENT profiles.yml
    selection — the gate the voice session checks on ``start``."""
    return _side_status("asr", _current_spec("asr"))["valid"]


def tts_ready() -> bool:
    """TTS assets installed AND valid for the CURRENT selection."""
    return _side_status("tts", _current_spec("tts"))["valid"]


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
    ready = bool(asr_status["valid"] and tts_status["valid"])
    return {
        "enabled": enabled,
        "configured": {
            "asr": asr_spec is not None,
            "tts": tts_spec is not None,
        },
        "asr": asr_status,
        "tts": tts_status,
        "ready": ready,
    }


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
