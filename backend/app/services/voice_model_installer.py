"""
Voice model installer — provider-aware setup for ASR + TTS models.

Used by the setup wizard (``POST /api/setup/pull-models`` SSE stream) and by
the host-side script ``scripts/install-voice-models.py`` (which re-implements
the download with stdlib-only urllib and writes the SAME manifest).

Key design rules (enforced by tests):

* **profiles.yml is the single source of truth.** The selected ASR/TTS
  provider + model + revision always come from ``settings.get_voice_config()``
  (when available) or from parsing ``profiles.yml`` directly. Nothing in this
  module (or anywhere else) hardcodes a *selection*. Provider IDs appear here
  only as *capability strings* — the installer implementations and validation
  whitelists below.
* **Real progress only.** ASR downloads stream bytes over HTTP and emit
  byte-accurate ``pull_progress`` events (completed/total/percent computed
  from actual file sizes). Stages without byte progress (pip installs,
  metadata listing, TTS package materialization) emit ``pull_status`` events
  with real output lines — never invented percentages.
* **Idempotent.** Before every install the manifest + files on disk are
  validated. Already-installed + valid + matching model/revision → skipped
  with ``pull_done {"already_installed": true}``. Changed model/revision,
  missing/corrupt files, or an incomplete manifest entry → reinstall.
* **No first-use downloads.** This module is only invoked from the setup
  wizard (or the explicit CLI). The voice runtime never calls it.

Manifest — ``data/models/voice/.manifest.json`` (schema shared with the
host-side script so both installers see the same state):

    {
      "asr": {
        "provider": "qwen3-asr",
        "model": "Qwen/Qwen3-ASR-0.6B",
        "revision": "main",
        "type": "asr",
        "role": "default_asr",
        "path": "asr/Qwen3-ASR-0.6B",          # relative to the models dir
        "files": {"config.json": 731, ...},     # relative path -> bytes
        "total_bytes": 1400000731,
        "complete": true,
        "installed_at": "2026-04-17T12:00:00",
        "source": "https://huggingface.co/<repo>@<rev>"
      },
      "tts": {
        "provider": "pocket-tts",
        "model": "pocket-tts",
        "language": "english_2026-04",
        "voice": "mary",
        "type": "tts",
        "role": "default_tts",
        "path": "tts/pocket-tts",
        "files": {...},                          # empty when not preloaded
        "total_bytes": 0,
        "complete": true,
        "preloaded": false,                      # assets materialize on 1st load
        "package_installed": true,
        "installed_at": "..."
      }
    }

SSE event dicts yielded by :func:`stream_install_voice_dependencies` use the
exact event names the setup wizard already consumes (``pull_start`` /
``pull_progress`` / ``pull_status`` / ``pull_done`` / ``pull_error``) with one
new field — ``provider`` — plus ``kind`` (``voice_model`` | ``voice_runtime``).
The setup API adds ``provider``/``kind`` to the Ollama events as well so the
frontend sees a uniform protocol.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import inspect
import json
import logging
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import quote

import yaml

try:  # httpx is a hard backend dependency, but keep the module importable
    import httpx
except ImportError:  # pragma: no cover - only hit in exotic environments
    httpx = None  # type: ignore[assignment]

try:  # optional: used only for HF metadata when present
    import huggingface_hub
except ImportError:  # normal in the stock backend image
    huggingface_hub = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# ─── Voice config resolution (profiles.yml = single source of truth) ─────────


@dataclass
class VoiceModelSpec:
    """One side (asr/tts) of the resolved voice configuration.

    Attribute-compatible with the VoiceModelConfig objects produced by
    ``settings.get_voice_config()`` (when present) so downstream code can
    treat both shapes uniformly.
    """

    kind: str  # "asr" | "tts"
    provider: str
    model: str
    revision: Optional[str] = None
    language: Optional[str] = None
    voice: Optional[str] = None
    type: str = ""
    role: str = ""
    description: str = ""
    size: str = ""

    @property
    def display_name(self) -> str:
        """Short display name (matches the SSE `model` field convention)."""
        return self.model.split("/")[-1] if "/" in self.model else self.model

    def to_dict(self) -> dict:
        return {
            "kind": self.kind,
            "provider": self.provider,
            "model": self.model,
            "revision": self.revision,
            "language": self.language,
            "voice": self.voice,
            "type": self.type,
            "role": self.role,
            "description": self.description,
            "size": self.size,
        }


@dataclass
class VoiceConfig:
    """Resolved voice config for a profile (asr + tts)."""

    asr: VoiceModelSpec
    tts: VoiceModelSpec

    def to_dict(self) -> dict:
        return {"asr": self.asr.to_dict(), "tts": self.tts.to_dict()}


_SPEC_FIELDS = (
    "provider",
    "model",
    "revision",
    "language",
    "voice",
    "type",
    "role",
    "description",
    "size",
)


def _find_profiles_yml() -> Optional[Path]:
    """Locate profiles.yml (Docker first, then dev layout) — same discovery
    order as app.config.load_profiles."""
    candidates = [
        Path("/app/profiles.yml"),  # Inside Docker container
        Path(__file__).resolve().parent.parent.parent.parent / "profiles.yml",
    ]
    for p in candidates:
        if p.exists():
            return p
    return None


def _load_profiles_raw() -> dict:
    """Parse profiles.yml as a raw dict (yaml). Returns {} when missing."""
    path = _find_profiles_yml()
    if path is None:
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return data if isinstance(data, dict) else {}
    except (yaml.YAMLError, OSError) as e:
        logger.warning("Failed to parse profiles.yml (%s): %s", path, e)
        return {}


def _coerce_spec(kind: str, data: Any) -> Optional[VoiceModelSpec]:
    """Adapt a dict / pydantic-ish object into a VoiceModelSpec."""
    if data is None:
        return None
    if isinstance(data, dict):
        source: Dict[str, Any] = data
    elif callable(getattr(data, "model_dump", None)):
        try:
            source = dict(data.model_dump())
        except Exception:
            source = {}
    else:
        source = {k: getattr(data, k, None) for k in _SPEC_FIELDS}

    provider = source.get("provider")
    model = source.get("model")
    if not provider or not model:
        return None
    return VoiceModelSpec(
        kind=kind,
        provider=str(provider),
        model=str(model),
        revision=source.get("revision"),
        language=source.get("language"),
        voice=source.get("voice"),
        type=str(source.get("type") or kind),
        role=str(source.get("role") or f"default_{kind}"),
        description=str(source.get("description") or ""),
        size=str(source.get("size") or ""),
    )


def _load_voice_config_from_yml(profile: Optional[str]) -> Optional[VoiceConfig]:
    """Minimal standalone resolver: parse profiles.yml directly.

    Merges the top-level ``voice:`` defaults with the optional per-profile
    override ``profiles.<name>.voice:`` (any key, any side).
    """
    data = _load_profiles_raw()
    base = data.get("voice")
    if not isinstance(base, dict) or not base:
        return None

    merged: Dict[str, dict] = {}
    for side in ("asr", "tts"):
        side_cfg = dict(base.get(side) or {})
        if profile:
            profiles = data.get("profiles") or {}
            pdata = profiles.get(profile) or {}
            override = pdata.get("voice") if isinstance(pdata, dict) else None
            if isinstance(override, dict):
                side_cfg.update(override.get(side) or {})
        merged[side] = side_cfg

    asr = _coerce_spec("asr", merged.get("asr"))
    tts = _coerce_spec("tts", merged.get("tts"))
    if asr is None or tts is None:
        logger.warning("profiles.yml voice section incomplete (asr/tts missing keys)")
        return None
    return VoiceConfig(asr=asr, tts=tts)


def resolve_voice_config(profile: Optional[str] = None) -> Optional[VoiceConfig]:
    """Resolve the voice config for a profile.

    Resolution order:
    1. ``settings.get_voice_config(profile)`` — the canonical resolver
       (app.config), when the voice feature has landed there.
    2. Direct profiles.yml parsing (standalone fallback so this installer
       works even when config.py has no voice support yet).
    """
    from app.config import settings  # local import avoids cycles

    getter = getattr(settings, "get_voice_config", None)
    if callable(getter):
        try:
            native = getter(profile)
        except TypeError:
            native = getter()
        except Exception as e:  # resolver blew up — fall back to yml
            logger.warning("settings.get_voice_config failed: %s", e)
            native = None
        if native is not None:
            asr = _coerce_spec("asr", getattr(native, "asr", None))
            tts = _coerce_spec("tts", getattr(native, "tts", None))
            if asr is not None and tts is not None:
                return VoiceConfig(asr=asr, tts=tts)
    return _load_voice_config_from_yml(profile)


# ─── Provider capabilities (installer implementations — NOT selections) ──────

# The set of provider IDs this installer knows how to install. Anything else
# in profiles.yml fails loudly (honest "unsupported provider" error) instead
# of silently skipping.
SUPPORTED_ASR_PROVIDERS = {"qwen3-asr"}
SUPPORTED_TTS_PROVIDERS = {"pocket-tts"}

HF_BASE_URL = "https://huggingface.co"


@dataclass(frozen=True)
class RuntimePackage:
    """A pip package required by the voice runtime.

    ``id`` is the setup-plan identifier; ``module`` is the import name used
    for the find_spec() presence check (find_spec only — heavy libs are never
    imported here).
    """

    id: str
    module: str
    display: str
    pip_name: str
    pip_extra_args: Tuple[str, ...] = ()
    size: str = ""

    def pip_command(self) -> List[str]:
        cmd = [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-cache-dir",
            "--progress-bar",
            "off",
            self.pip_name,
        ]
        cmd.extend(self.pip_extra_args)
        return cmd


# Registry of the voice runtime packages. Mirrors the "voice" category
# entries in app/services/deps_manager.py CATALOG (kept consistent by
# test_voice_installer.py).
RUNTIME_PACKAGES: Dict[str, RuntimePackage] = {
    p.id: p
    for p in (
        RuntimePackage(
            id="torch-cpu",
            module="torch",
            display="PyTorch (CPU)",
            pip_name="torch",
            pip_extra_args=("--index-url", "https://download.pytorch.org/whl/cpu"),
            size="~200 MB",
        ),
        RuntimePackage(
            id="transformers",
            module="transformers",
            display="Transformers",
            pip_name="transformers",
            size="~50 MB",
        ),
        RuntimePackage(
            id="soundfile",
            module="soundfile",
            display="SoundFile (audio I/O)",
            pip_name="soundfile",
            size="~3 MB",
        ),
        RuntimePackage(
            id="webrtcvad-wheels",
            module="webrtcvad",
            display="WebRTC VAD",
            pip_name="webrtcvad-wheels",
            size="~1 MB",
        ),
        RuntimePackage(
            id="huggingface-hub",
            module="huggingface_hub",
            display="Hugging Face Hub client",
            pip_name="huggingface-hub",
            size="~2 MB",
        ),
        RuntimePackage(
            id="pocket-tts",
            module="pocket_tts",
            display="Pocket TTS runtime",
            pip_name="pocket-tts",
            size="~600 MB",
        ),
    )
}

# Packages required regardless of provider (audio I/O + VAD for the voice
# pipeline) plus per-provider runtime requirements. Keyed by provider ID
# (capability string) — the *selection* still comes from profiles.yml.
VOICE_COMMON_RUNTIME: Tuple[str, ...] = ("soundfile", "webrtcvad-wheels")
PROVIDER_RUNTIME: Dict[str, Tuple[str, ...]] = {
    "qwen3-asr": ("torch-cpu", "transformers", "huggingface-hub"),
    "pocket-tts": ("pocket-tts",),
}


def required_runtime_package_ids(cfg: VoiceConfig) -> List[str]:
    """Package ids required by the configured providers (+ common set)."""
    ids: List[str] = list(VOICE_COMMON_RUNTIME)
    for provider in (cfg.asr.provider, cfg.tts.provider):
        ids.extend(PROVIDER_RUNTIME.get(provider, ()))
    seen: set = set()
    ordered: List[str] = []
    for i in ids:
        if i not in seen:
            seen.add(i)
            ordered.append(i)
    return ordered


def _module_importable(module_name: str) -> bool:
    """find_spec-only presence check — never imports heavy libs."""
    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


def missing_runtime_packages(cfg: VoiceConfig) -> List[RuntimePackage]:
    """Runtime packages that are not importable right now."""
    missing = []
    for pkg_id in required_runtime_package_ids(cfg):
        pkg = RUNTIME_PACKAGES.get(pkg_id)
        if pkg is None:
            # Unknown package id — provider mapping drift; fail loudly.
            raise RuntimeError(f"Unknown voice runtime package id: {pkg_id}")
        if not _module_importable(pkg.module):
            missing.append(pkg)
    return missing


# ─── Paths (everything under data/ — the persisted Docker bind mount) ────────

_MODELS_DIR_OVERRIDE: Optional[Path] = None


def get_data_dir() -> Path:
    """The persisted data directory (Docker: /app/data, dev: backend/../data)."""
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
    fallback = Path("/tmp/realopen-data")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _models_store():
    """Lazy import of app.voice.models_store (owned by the voice core agent).

    Returns None when the voice package isn't present yet — every use has a
    standalone fallback so the setup wizard keeps working.
    """
    try:
        from app.voice import models_store

        return models_store
    except Exception:
        return None


def get_models_dir() -> Path:
    """Voice model storage root: ``data/models/voice``.

    Prefers ``app.voice.models_store.MODELS_DIR`` when available (same path
    by construction) so both modules agree on the location.
    """
    if _MODELS_DIR_OVERRIDE is not None:
        return Path(_MODELS_DIR_OVERRIDE)
    ms = _models_store()
    if ms is not None:
        attr = getattr(ms, "MODELS_DIR", None)
        if attr is not None:
            return Path(attr() if callable(attr) else attr)
    return get_data_dir() / "models" / "voice"


def set_models_dir_for_testing(path: Optional[Path]) -> None:
    """Test hook: pin the models dir (keeps tests hermetic)."""
    global _MODELS_DIR_OVERRIDE
    _MODELS_DIR_OVERRIDE = Path(path) if path is not None else None


def _dir_name(model_id: str) -> str:
    """Directory name for a model id — last path segment."""
    return model_id.split("/")[-1] or model_id


def asr_target_dir(spec: VoiceModelSpec) -> Path:
    """ASR snapshot directory: ``data/models/voice/asr/<model-name>``."""
    ms = _models_store()
    if ms is not None:
        fn = getattr(ms, "asr_dir", None)
        if fn is not None:
            try:
                if callable(fn):
                    return Path(fn(spec))
                return Path(fn) / _dir_name(spec.model)
            except Exception:
                pass  # fall through to the standalone derivation
    return get_models_dir() / "asr" / _dir_name(spec.model)


def tts_target_dir(spec: VoiceModelSpec) -> Path:
    """TTS asset directory: ``data/models/voice/tts/<model-name>``."""
    return get_models_dir() / "tts" / _dir_name(spec.model)


def _manifest_rel_path(target: Path) -> str:
    """Manifest 'path' value for a target dir (relative to the models dir)."""
    try:
        return str(target.relative_to(get_models_dir()))
    except ValueError:
        return "/".join(target.parts[-2:])


# ─── Manifest ─────────────────────────────────────────────────────────────────

MANIFEST_FILENAME = ".manifest.json"


def manifest_path() -> Path:
    return get_models_dir() / MANIFEST_FILENAME


def read_manifest() -> dict:
    p = manifest_path()
    try:
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to read voice manifest %s: %s", p, e)
    return {}


def write_manifest(data: dict) -> None:
    p = manifest_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(p)


def _entry_matches(kind: str, spec: VoiceModelSpec, entry: dict) -> bool:
    """True when the manifest entry was written for this exact selection."""
    if entry.get("model") != spec.model:
        return False
    if entry.get("provider") != spec.provider:
        return False
    if kind == "asr" and entry.get("revision") != (spec.revision or "main"):
        return False
    if kind == "tts" and entry.get("language") != spec.language:
        # The language bundle determines which assets the runtime needs.
        # (Changing only the speaker `voice` never requires a re-download.)
        return False
    return True


def _files_valid(kind: str, entry: dict) -> Tuple[bool, str]:
    """Validate the files recorded in a manifest entry against disk."""
    rel = entry.get("path")
    files = entry.get("files") or {}
    if not rel:
        return False, "manifest entry has no path"
    if kind == "tts" and not files and not entry.get("preloaded"):
        # TTS entries may legitimately have no files when the package
        # materializes its cache on first load — the package_installed /
        # preloaded flags carry the state instead.
        return True, ""
    root = get_models_dir() / rel
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


def _run_models_store_validator(kind: str, cfg: VoiceConfig) -> Optional[bool]:
    """Call app.voice.models_store validators when available.

    Returns None when models_store is absent or the validator could not be
    applied (unknown signature etc.) — file-based validation still applies.
    """
    ms = _models_store()
    if ms is None:
        return None
    fn = getattr(ms, f"validate_{kind}_install", None)
    if not callable(fn):
        return None
    try:
        result = fn(cfg)
    except Exception as e:
        logger.warning("models_store validate_%s_install raised: %s", kind, e)
        return None
    if result is None:
        return None
    return bool(result)


def check_installed(kind: str, cfg: VoiceConfig) -> Tuple[bool, str]:
    """Idempotency check for one side (asr/tts) of the voice install.

    Returns (installed_ok, reason). ``reason`` is "" when installed.
    Reasons: not_installed | incomplete | changed | missing file: … |
    size mismatch: … | runtime package missing | validation_failed.
    """
    spec = cfg.asr if kind == "asr" else cfg.tts
    entry = read_manifest().get(kind)
    if not isinstance(entry, dict) or not entry:
        return False, "not_installed"
    if not entry.get("complete"):
        return False, "incomplete"
    if not _entry_matches(kind, spec, entry):
        return False, "changed"
    ok, why = _files_valid(kind, entry)
    if not ok:
        return False, why

    # Extra: the TTS python package must still be importable (pip packages
    # live in the image, not in the data volume — a rebuild wipes them).
    if kind == "tts":
        pkg = RUNTIME_PACKAGES.get("pocket-tts")
        if pkg and not _module_importable(pkg.module):
            return False, "runtime package missing"

    validator = _run_models_store_validator(kind, cfg)
    if validator is False:
        return False, "validation_failed"
    return True, ""


# ─── Install plan ─────────────────────────────────────────────────────────────


def voice_enabled_for_profile(
    profile: str, enabled_modules: Optional[Sequence[str]] = None
) -> bool:
    """Whether voice dependencies should be installed for this setup run.

    Semantics (matches the project's module architecture):
    * If modules.yml defines an optional ``voice`` module → voice is enabled
      only when the user selected it (enabled_modules) or it is already
      enabled in settings.
    * If no voice module is defined (current state — voice is a core
      capability of the assistant) → always enabled during setup, per the
      spec's "setup wizard must download ASR/TTS" requirement.
    """
    from app.config import settings

    try:
        modules = settings.get_modules()
    except Exception:
        modules = {}
    voice_module = modules.get("voice")
    if voice_module is None:
        return True
    if voice_module.required:
        return True
    if enabled_modules and "voice" in enabled_modules:
        return True
    try:
        return settings.is_module_enabled("voice")
    except Exception:
        return False


def voice_model_entries(profile: Optional[str] = None) -> List[dict]:
    """Voice MODEL entries for the setup wizard plan (``_get_models_to_pull``).

    The entries mirror the Ollama plan entries plus the provider/kind fields.
    """
    cfg = resolve_voice_config(profile)
    if cfg is None:
        return []
    entries = []
    for spec in (cfg.asr, cfg.tts):
        entry = {
            "id": spec.model,
            "role": spec.role,
            "description": spec.description,
            "size": spec.size,
            "module": "voice",
            "provider": spec.provider,
            "kind": "voice_model",
            "voice_type": spec.kind,
        }
        if spec.kind == "asr":
            entry["revision"] = spec.revision or "main"
        else:
            entry["language"] = spec.language
            entry["voice"] = spec.voice
        entries.append(entry)
    return entries


def voice_runtime_entries(profile: Optional[str] = None) -> List[dict]:
    """Runtime PIP package entries — one per missing package (find_spec)."""
    cfg = resolve_voice_config(profile)
    if cfg is None:
        return []
    entries = []
    try:
        missing = missing_runtime_packages(cfg)
    except RuntimeError as e:
        logger.error("Cannot resolve voice runtime packages: %s", e)
        return []
    for pkg in missing:
        entries.append(
            {
                "id": pkg.id,
                "role": "runtime",
                "description": pkg.display,
                "size": pkg.size,
                "module": "voice",
                "provider": "pip",
                "kind": "voice_runtime",
            }
        )
    return entries


def voice_install_plan(
    profile: Optional[str] = None,
    enabled_modules: Optional[Sequence[str]] = None,
) -> List[dict]:
    """Full voice install plan: runtime packages first, then ASR, then TTS."""
    if profile and not voice_enabled_for_profile(profile, enabled_modules):
        return []
    return voice_runtime_entries(profile) + voice_model_entries(profile)


def voice_status_summary(profile: Optional[str] = None) -> dict:
    """Voice dependency status summary for /setup/status.

    Distinguishes configured / installed / valid / ready (per the spec),
    with an honest per-package importable state for the runtime.
    """
    cfg = resolve_voice_config(profile)
    if cfg is None:
        return {
            "configured": False,
            "ready": False,
            "asr": None,
            "tts": None,
            "runtime": [],
        }

    runtime = []
    try:
        for pkg_id in required_runtime_package_ids(cfg):
            pkg = RUNTIME_PACKAGES[pkg_id]
            runtime.append(
                {
                    "id": pkg.id,
                    "display": pkg.display,
                    "installed": _module_importable(pkg.module),
                }
            )
    except RuntimeError:
        pass

    def _side(kind: str) -> dict:
        spec = cfg.asr if kind == "asr" else cfg.tts
        installed, reason = check_installed(kind, cfg)
        out = {
            "provider": spec.provider,
            "model": spec.model,
            "revision": spec.revision,
            "language": spec.language,
            "voice": spec.voice,
            "description": spec.description,
            "size": spec.size,
            "configured": True,
            "installed": installed,
            "valid": installed,  # valid == manifest + files verified
        }
        if not installed and reason:
            out["reason"] = reason
        return out

    asr_status = _side("asr")
    tts_status = _side("tts")
    runtime_ready = all(p["installed"] for p in runtime)
    ready = asr_status["installed"] and tts_status["installed"] and runtime_ready
    return {
        "configured": True,
        "ready": ready,
        "asr": asr_status,
        "tts": tts_status,
        "runtime": runtime,
    }


# ─── Event helpers ────────────────────────────────────────────────────────────


def _evt(
    event: str,
    model: str,
    provider: str,
    kind: str,
    index: Optional[int] = None,
    total_models: Optional[int] = None,
    **extra: Any,
) -> dict:
    payload: Dict[str, Any] = {
        "event": event,
        "model": model,
        "module": "voice",
        "provider": provider,
        "kind": kind,
    }
    if index is not None:
        payload["index"] = index
    if total_models is not None:
        payload["total_models"] = total_models
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


# ─── Runtime package install (pip) ────────────────────────────────────────────


async def _pip_install_package(
    pkg: RuntimePackage,
    index: int,
    total_models: int,
) -> AsyncGenerator[dict, None]:
    """pip install one runtime package, streaming real output lines.

    Indeterminate stage: only real pip output lines are emitted
    (pull_status), never invented percentages.
    """
    cmd = pkg.pip_command()
    yield _evt(
        "pull_status",
        pkg.id,
        "pip",
        "voice_runtime",
        index,
        total_models,
        status="installing",
        output=f"Running: {' '.join(cmd)}",
    )
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode(errors="replace").strip()
            if text:
                yield _evt(
                    "pull_status",
                    pkg.id,
                    "pip",
                    "voice_runtime",
                    index,
                    total_models,
                    status="installing",
                    output=text[:400],
                )
        rc = await proc.wait()
        if rc != 0:
            yield _evt(
                "pull_error",
                pkg.id,
                "pip",
                "voice_runtime",
                index,
                total_models,
                error=f"pip install failed (exit code {rc})",
            )
            return
        # pip installs into site-packages — refresh the import system's
        # caches so find_spec sees the new package in THIS process.
        importlib.invalidate_caches()
        if not _module_importable(pkg.module):
            yield _evt(
                "pull_error",
                pkg.id,
                "pip",
                "voice_runtime",
                index,
                total_models,
                error="pip reported success but module is still not importable",
            )
            return
    except FileNotFoundError:
        yield _evt(
            "pull_error",
            pkg.id,
            "pip",
            "voice_runtime",
            index,
            total_models,
            error="pip is not available in this environment",
        )
        return
    finally:
        if proc is not None and proc.returncode is None:
            proc.kill()
            try:
                await proc.wait()
            except Exception:
                pass
    yield _evt("pull_done", pkg.id, "pip", "voice_runtime", index, total_models)


# ─── ASR install (HF snapshot over streaming HTTP) ────────────────────────────


def _hf_token() -> Optional[str]:
    import os

    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")


async def _list_hf_files(
    repo_id: str,
    revision: str,
) -> List[Tuple[str, Optional[int]]]:
    """List repo files with sizes: (relative path, size or None).

    Prefers huggingface_hub (metadata call, run in a thread); falls back to
    the plain HTTP tree API. Raises on failure.
    """
    if huggingface_hub is not None:

        def _via_hub() -> List[Tuple[str, Optional[int]]]:
            api = huggingface_hub.HfApi()
            info = api.model_info(repo_id, revision=revision, files_metadata=True)
            out = []
            for s in getattr(info, "siblings", None) or []:
                path = getattr(s, "rfilename", None)
                if not path:
                    continue
                out.append((path, getattr(s, "size", None)))
            return out

        return await asyncio.to_thread(_via_hub)

    if httpx is None:
        raise RuntimeError("neither huggingface_hub nor httpx is available")
    url = (
        f"{HF_BASE_URL}/api/models/{quote(repo_id, safe='/')}"
        f"/tree/{quote(revision, safe='')}"
        "?recursive=true"
    )
    headers = {}
    token = _hf_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(url, headers=headers)
        resp.raise_for_status()
        entries = resp.json()
    out = []
    for e in entries:
        if not isinstance(e, dict) or e.get("type") != "file":
            continue
        path = e.get("path")
        if not path:
            continue
        size = e.get("size")
        out.append((path, int(size) if isinstance(size, int) else None))
    if not out:
        raise RuntimeError(f"no files listed for {repo_id}@{revision}")
    return out


def _resolve_url(repo_id: str, revision: str, path: str) -> str:
    return (
        f"{HF_BASE_URL}/{quote(repo_id, safe='/')}"
        f"/resolve/{quote(revision, safe='')}/{quote(path, safe='/')}"
    )


# Progress events are throttled by both bytes and wall time so the SSE stream
# stays informative without flooding the wizard.
_PROGRESS_MIN_BYTES = 512 * 1024
_PROGRESS_MIN_SECONDS = 0.25


async def _download_asr_model(
    cfg: VoiceConfig,
    spec: VoiceModelSpec,
    index: int,
    total_models: int,
) -> AsyncGenerator[dict, None]:
    """Download the ASR model snapshot with byte-accurate progress."""
    if spec.provider not in SUPPORTED_ASR_PROVIDERS:
        yield _evt(
            "pull_error",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            error=(
                f"Unsupported ASR provider '{spec.provider}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_ASR_PROVIDERS))}"
            ),
        )
        return

    revision = spec.revision or "main"
    dest_dir = asr_target_dir(spec)

    yield _evt(
        "pull_status",
        spec.display_name,
        spec.provider,
        "voice_model",
        index,
        total_models,
        status="listing",
        output=f"Listing files for {spec.model}@{revision}...",
    )
    try:
        files = await _list_hf_files(spec.model, revision)
    except Exception as e:
        yield _evt(
            "pull_error",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            error=f"Failed to list model files: {e}",
        )
        return

    total_bytes = sum(size for _, size in files if size is not None)
    all_sizes_known = all(size is not None for _, size in files)
    if all_sizes_known:
        yield _evt(
            "pull_status",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            status="preparing",
            output=f"{len(files)} files, {total_bytes / (1024 * 1024):.1f} MB total",
        )

    if httpx is None:
        yield _evt(
            "pull_error",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            error="httpx is not available — cannot download",
        )
        return

    headers = {}
    token = _hf_token()
    if token:
        headers["Authorization"] = f"Bearer {token}"

    completed = 0
    files_record: Dict[str, int] = {}
    last_emit_bytes = 0
    last_emit_time = time.monotonic()
    dest_dir.mkdir(parents=True, exist_ok=True)

    timeout = httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=60.0)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        for file_path, expected_size in files:
            target = dest_dir / file_path
            target.parent.mkdir(parents=True, exist_ok=True)

            # File-level idempotency: an existing file with the right size
            # is skipped and its bytes counted as completed.
            if target.exists() and expected_size is not None:
                actual = target.stat().st_size
                if actual == expected_size:
                    completed += actual
                    files_record[file_path] = actual
                    continue

            url = _resolve_url(spec.model, revision, file_path)
            try:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code != 200:
                        body = (await resp.aread())[:200]
                        yield _evt(
                            "pull_error",
                            spec.display_name,
                            spec.provider,
                            "voice_model",
                            index,
                            total_models,
                            error=(
                                f"HTTP {resp.status_code} for {file_path}: "
                                f"{body.decode(errors='replace')}"
                            ),
                        )
                        return
                    if not all_sizes_known:
                        # Unknown size — honest status line, no percent.
                        yield _evt(
                            "pull_status",
                            spec.display_name,
                            spec.provider,
                            "voice_model",
                            index,
                            total_models,
                            status="downloading",
                            output=f"Downloading {file_path} (size unknown — no percent)",
                        )
                    tmp = target.with_suffix(target.suffix + ".part")
                    got = 0
                    with open(tmp, "wb+") as fh:
                        async for chunk in resp.aiter_bytes(256 * 1024):
                            fh.write(chunk)
                            got += len(chunk)
                            completed += len(chunk)
                            if all_sizes_known and (
                                completed - last_emit_bytes >= _PROGRESS_MIN_BYTES
                                or time.monotonic() - last_emit_time
                                >= _PROGRESS_MIN_SECONDS
                            ):
                                last_emit_bytes = completed
                                last_emit_time = time.monotonic()
                                pct = (
                                    int(completed / total_bytes * 100)
                                    if total_bytes
                                    else 0
                                )
                                yield _evt(
                                    "pull_progress",
                                    spec.display_name,
                                    spec.provider,
                                    "voice_model",
                                    index,
                                    total_models,
                                    status="downloading",
                                    completed=completed,
                                    total=total_bytes,
                                    percent=pct,
                                )
                    if expected_size is not None and got != expected_size:
                        yield _evt(
                            "pull_error",
                            spec.display_name,
                            spec.provider,
                            "voice_model",
                            index,
                            total_models,
                            error=(
                                f"size mismatch after download: {file_path} "
                                f"({got} bytes, expected {expected_size})"
                            ),
                        )
                        return
                    tmp.replace(target)
                    files_record[file_path] = got
            except httpx.HTTPError as e:
                yield _evt(
                    "pull_error",
                    spec.display_name,
                    spec.provider,
                    "voice_model",
                    index,
                    total_models,
                    error=f"Download failed for {file_path}: {e}",
                )
                return

    # Final byte-accurate 100% event when sizes were known.
    if all_sizes_known and total_bytes:
        yield _evt(
            "pull_progress",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            status="downloading",
            completed=completed,
            total=total_bytes,
            percent=100,
        )

    # Persist the manifest entry (shared schema with the host script).
    entry = {
        "provider": spec.provider,
        "model": spec.model,
        "revision": revision,
        "type": spec.type or "asr",
        "role": spec.role or "default_asr",
        "path": _manifest_rel_path(asr_target_dir(spec)),
        "files": files_record,
        "total_bytes": completed,
        "complete": True,
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "source": f"{HF_BASE_URL}/{spec.model}@{revision}",
    }
    manifest = read_manifest()
    manifest["asr"] = entry
    write_manifest(manifest)

    validator = _run_models_store_validator("asr", cfg)
    if validator is False:
        yield _evt(
            "pull_error",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            error="models_store validator rejected the ASR install",
        )
        return
    yield _evt(
        "pull_status",
        spec.display_name,
        spec.provider,
        "voice_model",
        index,
        total_models,
        status="verifying",
        output=(
            f"Verified {len(files_record)} files "
            f"({completed / (1024 * 1024):.1f} MB)"
        ),
    )
    yield _evt(
        "pull_done", spec.display_name, spec.provider, "voice_model", index, total_models
    )


# ─── TTS install (pocket-tts package + optional asset precache) ───────────────


def _try_pocket_tts_precache(cache_dir: Path) -> Optional[str]:
    """Best-effort runtime introspection of the pocket_tts package.

    Looks for a download/precache-style API (never constructs a full model —
    that belongs to the voice runtime, not the installer). Returns a
    human-readable result line, or None when no programmatic API exists.
    """
    try:
        module = importlib.import_module("pocket_tts")
    except Exception:
        return None

    for name in (
        "download_model",
        "download",
        "fetch_model",
        "prepare_model",
        "precache",
    ):
        fn = getattr(module, name, None)
        if not callable(fn):
            continue
        try:
            sig = inspect.signature(fn)
        except (TypeError, ValueError):
            sig = None
        params = list(sig.parameters.keys()) if sig else []
        try:
            if params and params[0] not in ("self", "cls"):
                result = fn(str(cache_dir))
            else:
                result = fn()
            return f"pocket_tts.{name} → {result or 'done'}"
        except TypeError:
            try:
                result = fn()
                return f"pocket_tts.{name} → {result or 'done'}"
            except Exception as e:
                logger.warning("pocket_tts.%s failed: %s", name, e)
        except Exception as e:
            logger.warning("pocket_tts.%s failed: %s", name, e)
    return None


async def _install_tts(
    cfg: VoiceConfig,
    spec: VoiceModelSpec,
    index: int,
    total_models: int,
) -> AsyncGenerator[dict, None]:
    """Install the TTS runtime package and (best effort) its model assets."""
    if spec.provider not in SUPPORTED_TTS_PROVIDERS:
        yield _evt(
            "pull_error",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            error=(
                f"Unsupported TTS provider '{spec.provider}'. "
                f"Supported: {', '.join(sorted(SUPPORTED_TTS_PROVIDERS))}"
            ),
        )
        return

    pkg = RUNTIME_PACKAGES["pocket-tts"]
    if not _module_importable(pkg.module):
        # Install the package first (indeterminate, honest output lines).
        package_ok = False
        async for event in _pip_install_package(pkg, index, total_models):
            if event.get("event") == "pull_done":
                package_ok = True
                continue  # the model-level pull_done is emitted below
            if event.get("event") == "pull_error":
                yield event
                return
            yield event
        if not package_ok:
            return

    dest_dir = tts_target_dir(spec)
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Best-effort asset pre-download so assets persist under data/.
    precache_result = await asyncio.to_thread(_try_pocket_tts_precache, dest_dir)
    preloaded = False
    if precache_result:
        preloaded = True
        yield _evt(
            "pull_status",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            status="preparing",
            output=precache_result,
        )
    else:
        # Honest indeterminate state — never fabricate byte progress.
        yield _evt(
            "pull_status",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            status="preparing",
            output=(
                "pocket-tts exposes no programmatic model-download API; "
                "its assets materialize into the cache on first load"
            ),
        )

    files: Dict[str, int] = {}
    total_bytes = 0
    if preloaded:
        for f in dest_dir.rglob("*"):
            if f.is_file():
                size = f.stat().st_size
                files[str(f.relative_to(dest_dir))] = size
                total_bytes += size

    entry = {
        "provider": spec.provider,
        "model": spec.model,
        "language": spec.language,
        "voice": spec.voice,
        "type": spec.type or "tts",
        "role": spec.role or "default_tts",
        "path": _manifest_rel_path(tts_target_dir(spec)),
        "files": files,
        "total_bytes": total_bytes,
        "complete": True,
        "preloaded": preloaded,
        "package_installed": True,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    manifest = read_manifest()
    manifest["tts"] = entry
    write_manifest(manifest)

    validator = _run_models_store_validator("tts", cfg)
    if validator is False:
        yield _evt(
            "pull_error",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            error="models_store validator rejected the TTS install",
        )
        return

    yield _evt(
        "pull_done", spec.display_name, spec.provider, "voice_model", index, total_models
    )


# ─── Public installer entry points ────────────────────────────────────────────

# Reasons that do NOT require wiping the target directory before reinstall.
_KEEP_DIR_REASONS = ("not_installed", "runtime package missing")


async def stream_install_voice_dependencies(
    profile: str,
    enabled_modules: Optional[Sequence[str]] = None,
    index_offset: int = 0,
    total_models: Optional[int] = None,
    only: Optional[Sequence[str]] = None,
) -> AsyncGenerator[dict, None]:
    """Stream the voice dependency installation as setup-wizard SSE events.

    Yields the SAME event dicts the wizard already consumes
    (pull_start/pull_progress/pull_status/pull_done/pull_error) with the new
    ``provider`` + ``kind`` fields. Install order: runtime pip packages
    (missing only), ASR model, TTS package/assets.

    ``index_offset`` / ``total_models`` let the setup API keep a single
    sequential plan across Ollama + voice entries (ollama first, then voice
    runtime, then voice models). When ``total_models`` is None the voice-only
    count is used. ``only`` (values: "runtime", "asr", "tts") filters sides —
    used by the CLI's --asr-only/--tts-only flags.

    Cancellation: the consumer may break/close the stream at any time
    (client disconnect). GeneratorExit/CancelledError propagate through the
    ``async with`` blocks, closing HTTP streams and killing pip processes.
    """
    cfg = resolve_voice_config(profile)
    if cfg is None:
        # No voice section in profiles.yml — nothing to do (the caller
        # already gated the plan on voice being configured).
        return
    if profile and not voice_enabled_for_profile(profile, enabled_modules):
        return

    # Re-resolve the plan at install time (more accurate than plan time:
    # a package may have been installed by a previous step).
    try:
        missing = missing_runtime_packages(cfg)
    except RuntimeError as e:
        yield _evt(
            "pull_error",
            "voice",
            "pip",
            "voice_runtime",
            index_offset,
            total_models or 0,
            error=str(e),
        )
        return

    items: List[Any] = []
    if only is None or "runtime" in only:
        items.extend(missing)
    if only is None or "asr" in only:
        items.append(cfg.asr)
    if only is None or "tts" in only:
        items.append(cfg.tts)

    effective_total = (
        total_models if total_models is not None else (index_offset + len(items))
    )

    for local_index, item in enumerate(items):
        index = index_offset + local_index
        if isinstance(item, RuntimePackage):
            yield _evt(
                "pull_start", item.id, "pip", "voice_runtime", index, effective_total
            )
            async for event in _pip_install_package(item, index, effective_total):
                yield event
            continue

        spec: VoiceModelSpec = item
        installed, reason = check_installed(spec.kind, cfg)
        yield _evt(
            "pull_start",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            effective_total,
        )
        if installed:
            # Idempotent skip — "✓ ... already installed" semantics.
            yield _evt(
                "pull_status",
                spec.display_name,
                spec.provider,
                "voice_model",
                index,
                effective_total,
                status="already_installed",
                output=f"✓ {spec.model} already installed and valid — skipping",
            )
            yield _evt(
                "pull_done",
                spec.display_name,
                spec.provider,
                "voice_model",
                index,
                effective_total,
                already_installed=True,
            )
            continue

        # changed model/revision, corrupt/missing files, incomplete download
        # or missing runtime package → (re)install. Corrupt/changed installs
        # clear the target dir first so no stale bytes survive.
        if reason not in _KEEP_DIR_REASONS:
            target = (
                asr_target_dir(spec) if spec.kind == "asr" else tts_target_dir(spec)
            )
            if target.exists():
                shutil.rmtree(target, ignore_errors=True)

        if spec.kind == "asr":
            async for event in _download_asr_model(cfg, spec, index, effective_total):
                yield event
        else:
            async for event in _install_tts(cfg, spec, index, effective_total):
                yield event


async def install_voice_models_cli(
    profile: Optional[str] = None,
    asr_only: bool = False,
    tts_only: bool = False,
    on_line: Optional[Callable[[str], None]] = None,
) -> bool:
    """Plain-text variant of the installer (same logic, human progress).

    Prints real progress lines (byte-accurate where sizes are known) and
    returns True when no pull_error occurred. Usable inside the container::

        python -m app.services.voice_model_installer --profile cpu_small
    """
    emit = on_line if on_line is not None else print
    profile = profile or _current_profile()
    only: Optional[Tuple[str, ...]] = None
    if asr_only and tts_only:
        only = None
    elif asr_only:
        only = ("asr",)
    elif tts_only:
        only = ("tts",)
    ok = True

    async for event in stream_install_voice_dependencies(profile, only=only):
        name = event.get("event")
        if name == "pull_start":
            emit(f"→ Installing {event.get('model')} ({event.get('provider')})")
        elif name == "pull_progress":
            total = event.get("total") or 0
            completed = event.get("completed") or 0
            mb = completed / (1024 * 1024)
            if total:
                emit(
                    f"   [{event.get('percent', 0):3d}%] "
                    f"{mb:8.1f} MB / {total / (1024 * 1024):.1f} MB"
                )
            else:
                emit(f"   {mb:8.1f} MB")
        elif name == "pull_status":
            emit(f"   {event.get('output', event.get('status', ''))}")
        elif name == "pull_done":
            if event.get("already_installed"):
                emit(f"✓ {event.get('model')} already installed")
            else:
                emit(f"✓ {event.get('model')} installed")
        elif name == "pull_error":
            ok = False
            emit(f"✗ {event.get('model')}: {event.get('error')}")
    return ok


def _current_profile() -> str:
    try:
        from app.config import settings

        return settings.HARDWARE_PROFILE
    except Exception:
        return "cpu_small"


def _main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Install voice (ASR + TTS) models for a hardware profile"
    )
    parser.add_argument("--profile", default=None, help="hardware profile name")
    parser.add_argument("--asr-only", action="store_true")
    parser.add_argument("--tts-only", action="store_true")
    args = parser.parse_args(argv)

    profile = args.profile or _current_profile()
    print(f"Voice model installer — profile '{profile}'")
    cfg = resolve_voice_config(profile)
    if cfg is None:
        print("No voice section found in profiles.yml — nothing to install.")
        return 0
    print(f"  ASR: {cfg.asr.provider} / {cfg.asr.model}@{cfg.asr.revision}")
    print(f"  TTS: {cfg.tts.provider} / {cfg.tts.model} ({cfg.tts.language})")

    ok = asyncio.run(install_voice_models_cli(profile, args.asr_only, args.tts_only))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(_main())
