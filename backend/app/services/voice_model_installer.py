"""
Voice model installer — natural HuggingFace downloads into the persisted
data volume.

Used by the setup wizard (``POST /api/setup/pull-models`` SSE stream) and
by the CLI (``python -m app.services.voice_model_installer``).

**Design (user directive, 2026-09-19)** — let HuggingFace handle the model
downloads the way the packages do it NATURALLY (exactly like running
``mlx_qwen3_asr.Session(model=…)`` / ``pocket_tts.TTSModel.load_model(…)``
on a laptop: files land in the hub cache), and pin that hub cache to the
persisted data directory so everything survives rebuilds:

* ``app.voice.hf_cache.ensure_hf_env()`` pins ``HF_HOME`` to
  ``<data>/huggingface`` (Docker ``/app/data/huggingface`` — the
  ``./data:/app/data`` bind mount already persists it).
* **ASR** — ``huggingface_hub.snapshot_download(repo_id, revision)`` warms
  the cache; the wizard reports REAL byte progress by watching the cache
  directory grow (blobs on disk, including in-flight ``*.incomplete``).
  An existing legacy local snapshot (``data/models/voice/asr/…``) is
  SEEDED into the cache layout first so upgraded installs do not
  re-download ~1.9 GB.
* **TTS** — the pocket_tts package itself performs the natural download:
  ``TTSModel.load_model(language=…)`` + ``get_state_for_audio_prompt(<voice>)``
  during setup. The wizard watches the hub cache grow (honest MB-so-far
  status lines — the package owns the actual byte stream). Afterwards
  the manifest records the resulting ``models--*`` repos/snapshots.
* **Runtime pip packages** — installed through
  :mod:`app.services.pip_persistence` (a wheelhouse in the data volume +
  offline-first installs), so they persist like LibreOffice: after a
  rebuild the backend startup replays them offline from the wheelhouse.

Key design rules (enforced by tests):

* **profiles.yml is the single source of truth.** The selected ASR/TTS
  provider + model + revision always come from ``settings.get_voice_config()``
  (when available) or from parsing ``profiles.yml`` directly. Nothing here
  hardcodes a *selection*; provider IDs are capability strings only.
* **Real progress only.** Progress events carry bytes actually observed on
  disk; stages without byte granularity emit real output lines — never
  invented percentages.
* **Idempotent.** Before every install the manifest + cache are validated
  (models_store read side). Already-installed + valid + matching
  selection → skipped with ``pull_done {"already_installed": true}``.
* **No first-use downloads.** Only this module (invoked from the setup
  wizard or the CLI) downloads. Runtime engine loads run offline
  (``app.voice.hf_cache.offline_hub``) and fail fast with clear errors.

Manifest — ``data/models/voice/.manifest.json`` (schema v3; legacy v1/v2
entries stay readable for validation)::

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
        "type": "tts", "role": "default_tts", "store": "hf",
        "hf_repos": {"kyutai/pocket-tts-without-voice-cloning": "snapshots/<sha>"},
        "files": {"kyutai/...::model.safetensors": 209709196, ...},
        "complete": true, "package_installed": true, "installed_at": "..."
      },
      "runtime": {
        "packages": [
          {"id": "pocket-tts", "module": "pocket_tts",
           "pip_name": "pocket-tts", "pip_extra_args": [],
           "display": "Pocket TTS runtime"}, ...
        ],
        "installed_at": "..."
      }
    }

SSE event dicts use the exact names the setup wizard consumes
(``pull_start`` / ``pull_progress`` / ``pull_status`` / ``pull_done`` /
``pull_error``) with ``provider`` + ``kind``
(``voice_model`` | ``voice_runtime``) fields.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.util
import json
import logging
import os
import shutil
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import (
    Any,
    AsyncGenerator,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
)
from urllib.parse import quote

import yaml

try:  # httpx is a hard backend dependency, but keep the module importable
    import httpx
except ImportError:  # pragma: no cover - only hit in exotic environments
    httpx = None  # type: ignore[assignment]

from app.services import pip_persistence
from app.voice.hf_cache import (
    dir_size_bytes,
    ensure_hf_env,
    hub_cache_dir,
    human_mb,
    repo_cache_dir,
    run_monitored,
    snapshot_files,
    snapshot_dir as hf_snapshot_dir,
    suppress_hub_progress_bars,
)

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
    runtime: Optional[str] = None

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
            "runtime": self.runtime,
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
    "runtime",
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
        runtime=source.get("runtime"),
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
    imported here). Installs go through pip_persistence so the wheelhouse
    in the data volume persists them across rebuilds.
    """

    id: str
    module: str
    display: str
    pip_name: str
    pip_extra_args: Tuple[str, ...] = ()
    size: str = ""

    def to_manifest_dict(self) -> dict:
        return {
            "id": self.id,
            "module": self.module,
            "display": self.display,
            "pip_name": self.pip_name,
            "pip_extra_args": list(self.pip_extra_args),
        }


# Registry of the voice runtime packages. Mirrors the "voice" category
# entries in app/services/deps_manager.py CATALOG.
RUNTIME_PACKAGES: Dict[str, RuntimePackage] = {
    p.id: p
    for p in (
        # ── Apple-Silicon host runtime (native MLX ASR — no torch needed) ──
        RuntimePackage(
            id="mlx-qwen3-asr",
            module="mlx_qwen3_asr",
            display="Qwen3-ASR MLX runtime (Apple Silicon)",
            pip_name="mlx-qwen3-asr==0.4.4",
            size="~60 MB",
        ),
        # ── Cross-platform (Linux/Docker) ASR runtime ──
        RuntimePackage(
            id="torch-cpu",
            module="torch",
            display="PyTorch (CPU)",
            pip_name="torch==2.14.0+cpu",
            pip_extra_args=("--index-url", "https://download.pytorch.org/whl/cpu"),
            size="~200 MB",
        ),
        # The official qwen-asr runtime: the Qwen3-ASR checkpoints ship in
        # the "thinker export" layout that plain transformers releases
        # mis-parse. qwen-asr pins its own transformers (4.57.6) and carries
        # the matching implementation. --no-deps keeps its demo deps out.
        RuntimePackage(
            id="qwen-asr",
            module="qwen_asr",
            display="Qwen3-ASR runtime (official)",
            pip_name="qwen-asr==0.0.6",
            pip_extra_args=("--no-deps",),
            size="~60 MB",
        ),
        RuntimePackage(
            id="transformers",
            module="transformers",
            display="Transformers (qwen-asr pin)",
            pip_name="transformers==4.57.6",
            size="~50 MB",
        ),
        RuntimePackage(
            id="accelerate",
            module="accelerate",
            display="Accelerate (low-memory model loading)",
            # Its small dependency set is already covered by the pinned
            # runtime entries/base backend. Resolving dependencies here can
            # replace CPU Torch with PyPI's CUDA build on Linux.
            pip_name="accelerate==1.12.0",
            pip_extra_args=("--no-deps",),
            size="~5 MB",
        ),
        RuntimePackage(
            id="psutil",
            module="psutil",
            display="psutil (accelerate dependency)",
            pip_name="psutil==7.2.2",
            size="~1 MB",
        ),
        RuntimePackage(
            id="qwen-omni-utils",
            module="qwen_omni_utils",
            display="Qwen audio utils",
            pip_name="qwen-omni-utils==0.0.9",
            size="~1 MB",
        ),
        RuntimePackage(
            id="nagisa",
            module="nagisa",
            display="nagisa (qwen-asr dependency)",
            pip_name="nagisa==0.2.11",
            size="~5 MB",
        ),
        RuntimePackage(
            id="soynlp",
            module="soynlp",
            display="soynlp (qwen-asr dependency)",
            pip_name="soynlp==0.0.493",
            size="~2 MB",
        ),
        RuntimePackage(
            id="soundfile",
            module="soundfile",
            display="SoundFile (audio I/O)",
            pip_name="soundfile==0.14.0",
            size="~3 MB",
        ),
        RuntimePackage(
            id="webrtcvad-wheels",
            module="webrtcvad",
            display="WebRTC VAD",
            pip_name="webrtcvad-wheels==2.0.14",
            size="~1 MB",
        ),
        RuntimePackage(
            id="huggingface-hub",
            module="huggingface_hub",
            display="Hugging Face Hub client",
            pip_name="huggingface-hub==0.36.2",
            size="~2 MB",
        ),
        RuntimePackage(
            id="pocket-tts",
            module="pocket_tts",
            display="Pocket TTS runtime",
            pip_name="pocket-tts==3.1.0",
            # Prevent Linux from resolving the multi-GB CUDA PyTorch stack.
            pip_extra_args=(
                "--extra-index-url",
                "https://download.pytorch.org/whl/cpu",
            ),
            size="~600 MB",
        ),
    )
}

# Packages required regardless of provider (audio I/O + VAD for the voice
# pipeline) plus per-provider runtime requirements. Keyed by provider ID
# (capability string) — the *selection* still comes from profiles.yml.
VOICE_COMMON_RUNTIME: Tuple[str, ...] = ("soundfile", "webrtcvad-wheels")


def _on_apple_host() -> bool:
    """True when running natively on macOS (MLX-capable host).

    Docker containers (even on a Mac) report linux — the cross-platform
    runtime set applies there, which is exactly right: MLX has no Linux
    builds, so containerized backends use the transformers engine.
    """
    return sys.platform == "darwin"


def _asr_runtime_packages() -> Tuple[str, ...]:
    """ASR provider runtime package ids for THIS host."""
    if _on_apple_host():
        # Native MLX runtime — mlx, numpy, regex, huggingface-hub (no torch).
        return ("mlx-qwen3-asr", "huggingface-hub")
    return (
        "torch-cpu",
        "qwen-asr",
        "transformers",
        "accelerate",
        "psutil",
        "qwen-omni-utils",
        "nagisa",
        "soynlp",
        "huggingface-hub",
    )


PROVIDER_RUNTIME: Dict[str, Tuple[str, ...]] = {
    "qwen3-asr": (),  # resolved per-host by _asr_runtime_packages()
    "pocket-tts": ("pocket-tts",),
}


def required_runtime_package_ids(cfg: VoiceConfig) -> List[str]:
    """Package ids required by the configured providers (+ common set)."""
    ids: List[str] = list(VOICE_COMMON_RUNTIME)
    # Install TTS first because Pocket TTS has the broadest dependency set;
    # the exact ASR pins that follow then stabilize shared Torch/HF packages.
    for provider in (cfg.tts.provider, cfg.asr.provider):
        if provider == "qwen3-asr":
            ids.extend(_asr_runtime_packages())
        else:
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


def _runtime_package_satisfied(pkg: RuntimePackage) -> bool:
    return pip_persistence.package_satisfied(pkg.pip_name, pkg.module)


def missing_runtime_packages(cfg: VoiceConfig) -> List[RuntimePackage]:
    """Runtime packages that are not importable right now."""
    missing = []
    for pkg_id in required_runtime_package_ids(cfg):
        pkg = RUNTIME_PACKAGES.get(pkg_id)
        if pkg is None:
            # Unknown package id — provider mapping drift; fail loudly.
            raise RuntimeError(f"Unknown voice runtime package id: {pkg_id}")
        if not _runtime_package_satisfied(pkg):
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
    """Lazy import of app.voice.models_store (the read-side owner)."""
    try:
        from app.voice import models_store

        return models_store
    except Exception:
        return None


def get_models_dir() -> Path:
    """Voice manifest storage root: ``data/models/voice``.

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
    """LEGACY ASR snapshot directory (seeding source): ``data/models/voice/asr/<model-name>``."""
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
    """LEGACY TTS asset directory: ``data/models/voice/tts/<model-name>``."""
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
    """Validate a manifest entry (hf store or legacy local) — mirrors
    app.voice.models_store._files_valid so idempotency checks agree."""
    ms = _models_store()
    if ms is not None:
        fn = getattr(ms, "_files_valid", None)
        if callable(fn):
            try:
                return fn(kind, entry)
            except Exception as e:  # noqa: BLE001 — mirror must never crash
                logger.warning("models_store _files_valid raised: %s", e)
    # Standalone fallback (hf store).
    if entry.get("store") == "hf":
        repos = entry.get("hf_repos")
        files = entry.get("files") or {}
        if not isinstance(repos, dict) or not repos or not files:
            return False, "manifest entry records no HF cache files"
        for repo_id, snapshot_name in repos.items():
            root = repo_cache_dir(str(repo_id)) / str(snapshot_name or "")
            if not (root.is_dir() and any(root.iterdir())):
                return False, f"HF cache snapshot missing for {repo_id}"
        for key, size in (files or {}).items():
            repo_id, _, rel = str(key).partition("::")
            root = repo_cache_dir(repo_id) / str((repos.get(repo_id) or "snapshots/x"))
            f = root / rel
            if not f.exists():
                return False, f"missing HF cache file: {rel}"
            try:
                if f.stat().st_size != int(size):
                    return False, f"size mismatch: {rel}"
            except (OSError, TypeError, ValueError):
                return False, f"unreadable file: {rel}"
        return True, ""
    # Standalone fallback (legacy local layout).
    rel = entry.get("path")
    files = entry.get("files") or {}
    if not rel or not files:
        return False, "manifest entry records no files"
    if kind == "tts" and "local-config.yaml" not in files:
        return False, "legacy TTS layout incomplete (no local-config.yaml)"
    root = get_models_dir() / rel
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
    applied — file-based validation still applies.
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
    """
    spec = cfg.asr if kind == "asr" else cfg.tts
    entry = read_manifest().get(kind)
    if not isinstance(entry, dict) or not entry:
        return False, "not_installed"
    if not entry.get("complete"):
        return False, "incomplete"
    if entry.get("store") != "hf":
        return False, "legacy_install"
    if not _entry_matches(kind, spec, entry):
        return False, "changed"
    ok, why = _files_valid(kind, entry)
    if not ok:
        return False, why

    # The TTS python package must still be importable (pip persistence
    # replays it at startup; a fresh container reports it honestly).
    if kind == "tts":
        pkg = RUNTIME_PACKAGES.get("pocket-tts")
        if pkg and not _runtime_package_satisfied(pkg):
            return False, "runtime package missing"

    validator = _run_models_store_validator(kind, cfg)
    if validator is False:
        return False, "validation_failed"
    return True, ""


# ─── Install plan ─────────────────────────────────────────────────────────────


def voice_enabled_for_profile(
    profile: str, enabled_modules: Optional[Sequence[str]] = None
) -> bool:
    """Whether voice dependencies should be installed for this setup run."""
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
    """Voice MODEL entries for the setup wizard plan (``_get_models_to_pull``)."""
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
    """Voice dependency status summary for /setup/status."""
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
                    "installed": _runtime_package_satisfied(pkg),
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
            "valid": installed,  # valid == manifest + cache verified
            "store": "hf",
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


# ─── Runtime package install (persistent pip) ─────────────────────────────────


async def _pip_install_package(
    pkg: RuntimePackage,
    index: int,
    total_models: int,
) -> AsyncGenerator[dict, None]:
    """pip install one runtime package through the persistent wheelhouse,
    streaming real output lines (never invented percentages)."""
    yield _evt(
        "pull_status",
        pkg.id,
        "pip",
        "voice_runtime",
        index,
        total_models,
        status="installing",
        output=f"Installing {pkg.display} (persistent wheelhouse)",
    )
    try:
        async for line in pip_persistence.pip_install_persistent(
            pkg.pip_name, pkg.pip_extra_args
        ):
            yield _evt(
                "pull_status",
                pkg.id,
                "pip",
                "voice_runtime",
                index,
                total_models,
                status="installing",
                output=line[:400],
            )
    except Exception as e:  # noqa: BLE001 — pip failures are install errors
        yield _evt(
            "pull_error",
            pkg.id,
            "pip",
            "voice_runtime",
            index,
            total_models,
            error=f"pip install failed: {e}",
        )
        return
    # pip installs into site-packages — refresh the import system's caches
    # so find_spec sees the new package in THIS process.
    importlib.invalidate_caches()
    if not _runtime_package_satisfied(pkg):
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
    yield _evt("pull_done", pkg.id, "pip", "voice_runtime", index, total_models)


def _record_runtime_manifest(cfg: VoiceConfig) -> None:
    """Record only a fully importable runtime set.

    A previous implementation recorded the desired package list even after
    an SSE pip failure, making rebuilds repeatedly attempt packages that had
    never installed successfully. The manifest is now a success record, not
    an installation wish list.
    """
    try:
        required = [
            RUNTIME_PACKAGES[pkg_id] for pkg_id in required_runtime_package_ids(cfg)
        ]
    except RuntimeError:
        return
    packages = [pkg.to_manifest_dict() for pkg in required]
    missing = [pkg.display for pkg in required if not _runtime_package_satisfied(pkg)]
    manifest = read_manifest()
    if missing:
        manifest.pop("runtime", None)
        write_manifest(manifest)
        raise RuntimeError(
            "voice runtime incomplete; not recording manifest: " + ", ".join(missing)
        )
    manifest["runtime"] = {
        "packages": packages,
        "installed_at": datetime.now(timezone.utc).isoformat(),
    }
    write_manifest(manifest)


# ─── HF helpers (listing, seeding, monitoring) ────────────────────────────────


def _hf_token() -> Optional[str]:
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")


async def _list_hf_files(
    repo_id: str,
    revision: str,
) -> List[Tuple[str, Optional[int]]]:
    """List repo files with sizes: (relative path, size or None).

    Prefers huggingface_hub (metadata call, run in a thread); falls back to
    the plain HTTP tree API. Raises on failure.
    """

    def _via_hub() -> List[Tuple[str, Optional[int]]]:
        import huggingface_hub  # noqa: PLC0415 — lazy by design

        api = huggingface_hub.HfApi()
        info = api.model_info(repo_id, revision=revision, files_metadata=True)
        out = []
        for s in getattr(info, "siblings", None) or []:
            path = getattr(s, "rfilename", None)
            if not path:
                continue
            out.append((path, getattr(s, "size", None)))
        return out

    try:
        return await asyncio.to_thread(_via_hub)
    except ImportError:
        pass

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


def _seed_legacy_asr_into_cache(spec: VoiceModelSpec) -> int:
    """Seed an existing LEGACY local snapshot into the HF cache layout.

    Upgraded installs already hold ``data/models/voice/asr/<model>/`` from
    the previous installer; registering those bytes as hub blobs (named by
    their LFS sha256 / git blob id) lets the natural snapshot_download
    VERIFY instead of re-downloading ~1.9 GB. Best-effort: any mismatch
    simply falls through to a normal download.

    Returns the number of files seeded (0 = nothing seeded).
    """
    legacy = asr_target_dir(spec)
    if not legacy.is_dir() or not any(legacy.iterdir()):
        return 0
    try:
        import huggingface_hub  # noqa: PLC0415

        api = huggingface_hub.HfApi()
        info = api.model_info(
            spec.model, revision=spec.revision or "main", files_metadata=True
        )
    except ImportError:
        return 0
    except Exception as e:  # noqa: BLE001 — metadata unreachable → normal download
        logger.info("cache seeding skipped (metadata unreachable): %s", e)
        return 0
    commit = getattr(info, "sha", None)
    if not commit:
        return 0
    repo_dir = repo_cache_dir(spec.model)
    snap = repo_dir / "snapshots" / commit
    seeded = 0
    for s in getattr(info, "siblings", None) or []:
        rel = getattr(s, "rfilename", None)
        if not rel:
            continue
        local = legacy / rel
        try:
            if not local.is_file():
                continue
            size = getattr(s, "size", None)
            if size is not None and local.stat().st_size != int(size):
                continue  # stale/foreign file — let the download replace it
            lfs = getattr(s, "lfs", None)
            blob_name = getattr(lfs, "sha256", None) or getattr(s, "blob_id", None)
            if not blob_name:
                continue
            blob = repo_dir / "blobs" / blob_name
            if not blob.exists():
                blob.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local, blob)
            target = snap / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists():
                try:
                    os.link(blob, target)
                except OSError:
                    shutil.copy2(blob, target)
            seeded += 1
        except OSError:
            continue
    if seeded:
        refs = repo_dir / "refs" / (spec.revision or "main")
        try:
            refs.parent.mkdir(parents=True, exist_ok=True)
            refs.write_text(commit, encoding="utf-8")
        except OSError:
            return 0
        logger.info(
            "seeded %d legacy ASR files into the HF cache (%s)", seeded, repo_dir
        )
    return seeded


def _repo_snapshot_name(repo_id: str) -> Optional[str]:
    """``snapshots/<sha>`` name recorded in the repo's refs/main."""
    ref = repo_cache_dir(repo_id) / "refs" / "main"
    try:
        if ref.is_file():
            sha = ref.read_text(encoding="utf-8").strip()
            if sha:
                return f"snapshots/{sha}"
    except OSError:
        pass
    return None


def _hub_state_before() -> Dict[str, int]:
    """{repo_name: size} of every models--* dir in the hub cache."""
    state: Dict[str, int] = {}
    hub = hub_cache_dir()
    if not hub.is_dir():
        return state
    for d in hub.iterdir():
        try:
            if d.is_dir() and d.name.startswith("models--"):
                state[d.name] = dir_size_bytes(d)
        except OSError:
            continue
    return state


def _new_or_grown_repos(before: Dict[str, int]) -> List[str]:
    """Repo dir names that appeared or grew since ``before``."""
    out: List[str] = []
    hub = hub_cache_dir()
    if not hub.is_dir():
        return out
    for d in hub.iterdir():
        try:
            if not (d.is_dir() and d.name.startswith("models--")):
                continue
            size = dir_size_bytes(d)
            if before.get(d.name) is None or before.get(d.name) != size:
                out.append(d.name)
        except OSError:
            continue
    return out


async def _run_with_progress_queue(
    fn,
    watch_dirs: List[Path],
) -> Tuple[Any, asyncio.Queue]:
    """run_monitored with progress routed to an asyncio queue.

    The watcher thread cannot await; the queue bridges threads → the event
    loop (call_soon_threadsafe). Returns (task, queue) — await the task for
    the result, drain the queue for byte-progress events.
    """
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue = asyncio.Queue()

    def on_progress(size: int) -> None:
        loop.call_soon_threadsafe(queue.put_nowait, size)

    task = asyncio.create_task(run_monitored(fn, watch_dirs, on_progress))
    return task, queue


async def _stream_warmup_subprocess(
    script: str,
    watch_dirs: Sequence[Path],
    timeout_s: float,
) -> AsyncGenerator[Tuple[str, Any], None]:
    """Run a setup-only Python warm-up in a killable child process.

    Events are ``("progress", bytes_on_disk)`` and one terminal
    ``("done", output)``. Timeout and non-zero exit raise RuntimeError.
    Unlike ``asyncio.to_thread``, cancellation and timeouts terminate the
    child, so a stalled hub request cannot hang the FastAPI process.
    """
    home = ensure_hf_env()
    target = pip_persistence.activate_persistent_site_packages()
    env = os.environ.copy()
    env["HF_HOME"] = str(home)
    env["HF_HUB_OFFLINE"] = "0"
    env["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    pythonpath = [str(target)]
    if env.get("PYTHONPATH"):
        pythonpath.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath)

    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
    )
    communicate = asyncio.create_task(proc.communicate())
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    last_size = -1
    try:
        while not communicate.done():
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise asyncio.TimeoutError
            try:
                await asyncio.wait_for(
                    asyncio.shield(communicate), timeout=min(0.4, remaining)
                )
            except asyncio.TimeoutError:
                if loop.time() >= deadline:
                    raise
                size = await asyncio.to_thread(
                    lambda: sum(dir_size_bytes(Path(p)) for p in watch_dirs)
                )
                if size != last_size:
                    last_size = size
                    yield "progress", size
        stdout, _ = await communicate
    except (asyncio.CancelledError, asyncio.TimeoutError):
        if proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        communicate.cancel()
        if isinstance(sys.exc_info()[1], asyncio.CancelledError):
            raise
        raise RuntimeError(f"model warm-up timed out after {timeout_s:.0f}s")

    output = (stdout or b"").decode(errors="replace").strip()
    if proc.returncode != 0:
        raise RuntimeError(f"model warm-up exited {proc.returncode}: {output[-1200:]}")
    yield "done", output


# ─── ASR install (natural snapshot_download into the persisted cache) ────────

# Overall bound for the monitored warm-up (seconds) — slow links may take a
# while for ~1.9 GB; a stuck network attempt fails at this bound.
_ASR_WARMUP_TIMEOUT_S = 3600.0


async def _install_asr_model(
    cfg: VoiceConfig,
    spec: VoiceModelSpec,
    index: int,
    total_models: int,
) -> AsyncGenerator[dict, None]:
    """Warm the HF cache for the ASR model — the natural way.

    ``huggingface_hub.snapshot_download`` performs the download (resumable,
    race-safe, into ``data/huggingface/hub``); the wizard reports REAL byte
    progress by watching the repo's cache directory grow. A legacy local
    snapshot is seeded first so upgrades skip the re-download.
    """
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
    ensure_hf_env()
    suppress_hub_progress_bars()

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

    known = [(p, s) for p, s in files if s is not None]
    total_bytes = sum(s for _, s in known)
    all_sizes_known = len(known) == len(files)
    if all_sizes_known and total_bytes:
        yield _evt(
            "pull_status",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            status="preparing",
            output=(
                f"{len(files)} files, {total_bytes / (1024 * 1024):.1f} MB total "
                f"(HF cache: {hub_cache_dir()})"
            ),
        )

    # Seed a legacy local snapshot into the cache (best-effort, offline).
    try:
        seeded = await asyncio.to_thread(_seed_legacy_asr_into_cache, spec)
    except Exception as e:  # noqa: BLE001 — seeding is an optimization
        logger.warning("cache seeding failed: %s", e)
        seeded = 0
    if seeded:
        yield _evt(
            "pull_status",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            status="preparing",
            output=(
                f"seeded {seeded} existing local files into the HF cache — "
                "only missing files will be downloaded"
            ),
        )

    repo_root = repo_cache_dir(spec.model)
    repo_root.mkdir(parents=True, exist_ok=True)
    yield _evt(
        "pull_status",
        spec.display_name,
        spec.provider,
        "voice_model",
        index,
        total_models,
        status="downloading",
        output=(
            "snapshot_download → the HuggingFace client handles resumable "
            "downloads into the persisted cache"
        ),
    )

    warm_script = (
        "from huggingface_hub import snapshot_download\n"
        f"print(snapshot_download(repo_id={spec.model!r}, revision={revision!r}))\n"
    )
    last_emit = 0.0
    snap_path: Optional[str] = None
    try:
        async for event_type, value in _stream_warmup_subprocess(
            warm_script, [repo_root], _ASR_WARMUP_TIMEOUT_S
        ):
            if event_type == "done":
                snap_path = str(value).splitlines()[-1] if value else None
                continue
            size = int(value)
            now = time.monotonic()
            if all_sizes_known and total_bytes and now - last_emit >= 0.25:
                last_emit = now
                yield _evt(
                    "pull_progress",
                    spec.display_name,
                    spec.provider,
                    "voice_model",
                    index,
                    total_models,
                    status="downloading",
                    completed=min(size, total_bytes),
                    total=total_bytes,
                    percent=min(int(size / total_bytes * 100), 100),
                )
    except Exception as e:  # noqa: BLE001 — honest install failure
        yield _evt(
            "pull_error",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            error=f"snapshot_download failed: {e}",
        )
        return

    if all_sizes_known and total_bytes:
        yield _evt(
            "pull_progress",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            status="downloading",
            completed=total_bytes,
            total=total_bytes,
            percent=100,
        )

    # Record the warmed snapshot in the manifest (schema v3, hf store).
    snapshot_name = _repo_snapshot_name(spec.model) or (
        Path(str(snap_path)).name and f"snapshots/{Path(snap_path).name}"
    )
    files_record: Dict[str, int] = {}
    snap_root = hf_snapshot_dir(spec.model, revision)
    if snap_root is None and not snap_path:
        yield _evt(
            "pull_error",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            error="snapshot_download completed without a readable snapshot path",
        )
        return
    root = snap_root or Path(str(snap_path))
    for p in root.rglob("*"):
        try:
            if p.is_file():
                files_record[f"{spec.model}::{p.relative_to(root).as_posix()}"] = (
                    p.stat().st_size
                )
        except OSError:
            continue
    if not files_record:
        yield _evt(
            "pull_error",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            error="snapshot_download returned no files — cache unreadable",
        )
        return

    entry = {
        "provider": spec.provider,
        "model": spec.model,
        "revision": revision,
        "type": spec.type or "asr",
        "role": spec.role or "default_asr",
        "store": "hf",
        "hf_repos": {spec.model: snapshot_name},
        "files": files_record,
        "total_bytes": sum(files_record.values()),
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
            f"verified {len(files_record)} files "
            f"({sum(files_record.values()) / (1024 * 1024):.1f} MB) in "
            f"{repo_root}"
        ),
    )
    yield _evt(
        "pull_done",
        spec.display_name,
        spec.provider,
        "voice_model",
        index,
        total_models,
    )


# ─── TTS install (natural pocket_tts load into the persisted cache) ───────────

# The setup-side forced load may legitimately download hundreds of MB —
# bounded so a stuck network attempt fails honestly.
_TTS_WARMUP_TIMEOUT_S = 900.0


def _warm_pocket_tts_sync(spec: VoiceModelSpec) -> Any:
    """Construct the model + voice state ONCE (setup-side, natural).

    Exactly the released API the user's local pipeline exercises:
    ``TTSModel.load_model(language=…)`` downloads the weights through the
    HF hub cache; ``get_state_for_audio_prompt(<voice name>)`` fetches the
    pretrained embedding into the same cache. After this call every
    runtime load is a local cache hit.
    """
    ensure_hf_env()
    importlib.invalidate_caches()
    pocket_tts = importlib.import_module("pocket_tts")
    TTSModel = getattr(pocket_tts, "TTSModel", None)
    if TTSModel is None:
        raise RuntimeError("pocket_tts exposes no TTSModel (unrecognized API)")
    load = getattr(TTSModel, "load_model", None)
    model = None
    if callable(load):
        lang = spec.language or "english_2026-04"
        errors: List[str] = []
        for args, kwargs in (
            ((), {"language": lang}),
            ((), {}),
            ((lang,), {}),
        ):
            try:
                model = load(*args, **kwargs)
                if model is not None:
                    break
            except TypeError:
                continue
            except Exception as e:  # noqa: BLE001 — remember, keep probing
                errors.append(f"{type(e).__name__}: {e}")
                continue
        if model is None:
            for args in ((), (lang,)):
                try:
                    model = TTSModel(*args)
                    if model is not None:
                        break
                except TypeError:
                    continue
        if model is None:
            raise RuntimeError(
                "pocket_tts load_model failed: "
                + ("; ".join(errors) or "unrecognized signature")
            )
    else:
        for args in ((), (spec.language or "english_2026-04",)):
            try:
                model = TTSModel(*args)
                if model is not None:
                    break
            except TypeError:
                continue
        if model is None:
            raise RuntimeError("pocket_tts API not recognized (no load_model)")

    # Voice state — the pretrained embedding for the configured speaker
    # (downloads into the same repo cache on first use; cached afterwards).
    get_state = getattr(model, "get_state_for_audio_prompt", None)
    voice_name = str(spec.voice or "mary").strip() or "mary"
    if callable(get_state):
        try:
            get_state(voice_name)
        except Exception as e:  # noqa: BLE001 — voice prompt is best-effort
            logger.warning(
                "voice state warm-up for %r failed (%s) — the runtime will "
                "retry and report a clear error if it persists",
                voice_name,
                e,
            )
    return model


async def _install_tts(
    cfg: VoiceConfig,
    spec: VoiceModelSpec,
    index: int,
    total_models: int,
) -> AsyncGenerator[dict, None]:
    """Install the TTS runtime package AND warm its HF cache (setup-time).

    The pocket_tts package performs its own natural download
    (``load_model(language=…)``); the wizard watches the persisted hub
    cache grow for honest progress. The manifest records the resulting
    repos/snapshots so runtime gates + startup replay see the state.
    """
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
    if not _runtime_package_satisfied(pkg):
        # Install the package first (persistent, honest output lines).
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

    ensure_hf_env()
    suppress_hub_progress_bars()
    yield _evt(
        "pull_status",
        spec.display_name,
        spec.provider,
        "voice_model",
        index,
        total_models,
        status="preparing",
        output=(
            f"Warming the Pocket TTS model (load_model(language={spec.language!r}) "
            f"— natural HF download into {hub_cache_dir()})"
        ),
    )

    before = await asyncio.to_thread(_hub_state_before)
    watch = [hub_cache_dir()]
    language = spec.language or "english_2026-04"
    voice = str(spec.voice or "mary").strip() or "mary"
    warm_script = (
        "import torch\n"
        "threads = torch.get_num_threads()\n"
        "from pocket_tts import TTSModel\n"
        "torch.set_num_threads(threads)\n"
        f"model = TTSModel.load_model(language={language!r})\n"
        f"state = model.get_state_for_audio_prompt({voice!r})\n"
        "assert state is not None\n"
        "print('pocket-tts-ready')\n"
    )
    baseline = sum(before.values())
    last_emit = 0.0
    try:
        async for event_type, value in _stream_warmup_subprocess(
            warm_script, watch, _TTS_WARMUP_TIMEOUT_S
        ):
            if event_type != "progress":
                continue
            size = int(value)
            now = time.monotonic()
            if now - last_emit >= 1.0:
                last_emit = now
                yield _evt(
                    "pull_status",
                    spec.display_name,
                    spec.provider,
                    "voice_model",
                    index,
                    total_models,
                    status="downloading",
                    output=(
                        f"pocket_tts is downloading — {human_mb(max(size - baseline, 0))} "
                        "in the persisted cache so far (no byte-accurate total: "
                        "the package owns the stream)"
                    ),
                )
    except Exception as e:  # noqa: BLE001 — honest install failure
        yield _evt(
            "pull_error",
            spec.display_name,
            spec.provider,
            "voice_model",
            index,
            total_models,
            error=f"Pocket TTS model warm-up failed: {e}",
        )
        return

    # Record the repos/snapshots the package materialized (schema v3).
    repos: Dict[str, str] = {}
    files_record: Dict[str, int] = {}
    grown = await asyncio.to_thread(_new_or_grown_repos, before)
    for repo_dir_name in grown:
        repo_id = repo_dir_name.removeprefix("models--").replace("--", "/")
        snapshot_name = _repo_snapshot_name(repo_id)
        if snapshot_name is None:
            continue
        repos[repo_id] = snapshot_name
        for rel, size in snapshot_files(repo_id).items():
            files_record[f"{repo_id}::{rel}"] = size
    if not files_record:
        # The model was already fully cached (no growth). Record the
        # language repo from the package's bundled config when possible,
        # else every currently-present models--* repo (the load we just
        # performed demonstrably used this cache state).
        repo_id = _pocket_tts_language_repo(spec.language)
        candidates = [repo_id] if repo_id else _all_cached_repo_ids()
        for repo_id in candidates:
            if not repo_id:
                continue
            snapshot_name = _repo_snapshot_name(repo_id)
            if snapshot_name is None:
                continue
            repos[repo_id] = snapshot_name
            for rel, size in snapshot_files(repo_id).items():
                files_record[f"{repo_id}::{rel}"] = size
    # Pocket TTS 3.x may bundle the selected language/voice assets in its
    # persisted wheel rather than materializing a Hugging Face repo. The
    # authoritative readiness check is the successful model + voice-state
    # warm-up above. Record HF files when they exist; otherwise record the
    # package-backed layout and let runtime package validation guard it.
    store = "hf" if files_record else "package"

    entry = {
        "provider": spec.provider,
        "model": spec.model,
        "language": spec.language,
        "voice": spec.voice,
        "type": spec.type or "tts",
        "role": spec.role or "default_tts",
        "store": store,
        "hf_repos": repos,
        "files": files_record,
        "total_bytes": sum(files_record.values()),
        "complete": True,
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
        "pull_status",
        spec.display_name,
        spec.provider,
        "voice_model",
        index,
        total_models,
        status="verifying",
        output=(
            "verified: model + voice state load from persisted "
            + (
                f"HF cache ({len(files_record)} files, "
                f"{human_mb(sum(files_record.values()))})"
                if files_record
                else "Pocket TTS package assets"
            )
        ),
    )
    yield _evt(
        "pull_done",
        spec.display_name,
        spec.provider,
        "voice_model",
        index,
        total_models,
    )


def _all_cached_repo_ids() -> List[str]:
    """Repo ids of every models--* dir currently in the hub cache."""
    out: List[str] = []
    hub = hub_cache_dir()
    if not hub.is_dir():
        return out
    for d in hub.iterdir():
        try:
            if d.is_dir() and d.name.startswith("models--"):
                out.append(d.name.removeprefix("models--").replace("--", "/"))
        except OSError:
            continue
    return out


def _pocket_tts_language_repo(language: Optional[str]) -> Optional[str]:
    """The HF repo id the pocket_tts config references for ``language``.

    Read from the package's bundled language config (hf:// source paths) —
    no hardcoded model ids. None when the config cannot be parsed.
    """
    try:
        import pocket_tts  # type: ignore  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — find_spec passed, import may fail
        return None
    pkg_file = getattr(pocket_tts, "__file__", None)
    if not pkg_file:
        return None
    cfg_dir = Path(pkg_file).parent / "config"
    lang = (language or "").strip() or "english"
    for cand in (cfg_dir / f"{lang}.yaml", cfg_dir / f"{lang}.yml"):
        if not cand.exists():
            continue
        try:
            data = yaml.safe_load(cand.read_text()) or {}
        except Exception:  # noqa: BLE001 — unreadable config
            continue
        for key in ("weights_path", "weights_path_without_voice_cloning"):
            src = str(data.get(key) or "")
            if src.startswith("hf://"):
                parts = src.removeprefix("hf://").split("/")
                if len(parts) >= 3:
                    return "/".join(parts[:2])
    return None


# ─── Public installer entry points ────────────────────────────────────────────

# Reasons that do NOT require wiping a legacy target directory before
# reinstall (the HF store needs no wiping at all — the hub client verifies
# and re-downloads only what changed).
_KEEP_DIR_REASONS = (
    "not_installed",
    "legacy_install",
    "runtime package missing",
    "TTS assets not installed — run the setup wizard",
    "TTS assets use the legacy v1 layout (no local-config.yaml) — "
    "re-run the setup wizard to install into the HF cache",
)


# One install at a time, process-wide. Two concurrent setup wizard runs
# (double click / auto-retry) MUST NOT race on the same cache — the second
# waits for the first, then runs idempotently (snapshot_download + the
# wheelhouse are safe under the lock).
_PULL_LOCK = asyncio.Lock()


async def stream_install_voice_dependencies(
    profile: str,
    enabled_modules: Optional[Sequence[str]] = None,
    index_offset: int = 0,
    total_models: Optional[int] = None,
    only: Optional[Sequence[str]] = None,
) -> AsyncGenerator[dict, None]:
    """Stream the voice dependency installation as setup-wizard SSE events.

    Yields the SAME event dicts the wizard already consumes
    (pull_start/pull_progress/pull_status/pull_done/pull_error) with the
    ``provider`` + ``kind`` fields. Install order: runtime pip packages
    (missing only), ASR model, TTS package+cache.

    ``index_offset`` / ``total_models`` let the setup API keep a single
    sequential plan across Ollama + voice entries. ``only`` (values:
    "runtime", "asr", "tts") filters sides — used by the CLI flags.

    Concurrency: installs are serialized process-wide (``_PULL_LOCK``).

    Cancellation: the consumer may break/close the stream at any time.
    The hub download thread finishes its current file (resumable); pip
    subprocesses are killed by their wrappers.
    """
    if _PULL_LOCK.locked():
        yield _evt(
            "pull_status",
            "Voice dependencies",
            "pip",
            "voice_runtime",
            index_offset,
            total_models or 0,
            status="waiting",
            output="Another model installation is running — waiting for it to finish…",
        )
    async with _PULL_LOCK:
        async for event in _stream_install_unlocked(
            profile,
            enabled_modules=enabled_modules,
            index_offset=index_offset,
            total_models=total_models,
            only=only,
        ):
            yield event


async def _stream_install_unlocked(
    profile: str,
    enabled_modules: Optional[Sequence[str]] = None,
    index_offset: int = 0,
    total_models: Optional[int] = None,
    only: Optional[Sequence[str]] = None,
) -> AsyncGenerator[dict, None]:
    """The actual install stream — call only while holding ``_PULL_LOCK``."""
    cfg = resolve_voice_config(profile)
    if cfg is None:
        # No voice section in profiles.yml — nothing to do.
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

        # Changed selection / corrupt legacy layout → drop the stale
        # manifest entry; the hub cache itself self-verifies (no wipe).
        manifest = read_manifest()
        stale = manifest.get(spec.kind)
        wiped = ""
        if isinstance(stale, dict) and stale.get("store") != "hf":
            target = (
                asr_target_dir(spec) if spec.kind == "asr" else tts_target_dir(spec)
            )
            if reason not in _KEEP_DIR_REASONS and target.exists():
                shutil.rmtree(target, ignore_errors=True)
                wiped = " (stale legacy assets removed)"
            manifest.pop(spec.kind, None)
            write_manifest(manifest)

        if reason == "changed" or reason:
            yield _evt(
                "pull_status",
                spec.display_name,
                spec.provider,
                "voice_model",
                index,
                effective_total,
                status="reinstalling",
                output=f"reinstalling ({reason}){wiped}",
            )

        if spec.kind == "asr":
            async for event in _install_asr_model(cfg, spec, index, effective_total):
                yield event
        else:
            async for event in _install_tts(cfg, spec, index, effective_total):
                yield event

    # Record the runtime packages for the startup replay (pip persistence).
    if only is None or "runtime" in only:
        try:
            _record_runtime_manifest(cfg)
        except Exception as e:  # noqa: BLE001 — recording is best-effort
            logger.warning("runtime manifest recording failed: %s", e)


async def install_voice_models_cli(
    profile: Optional[str] = None,
    asr_only: bool = False,
    tts_only: bool = False,
    on_line: Optional[Any] = None,
) -> bool:
    """Plain-text variant of the installer (same logic, human progress)."""
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
    ensure_hf_env()
    print(f"  HF cache: {hub_cache_dir()} (persisted in the data volume)")

    ok = asyncio.run(install_voice_models_cli(profile, args.asr_only, args.tts_only))
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover - manual entry point
    raise SystemExit(_main())
