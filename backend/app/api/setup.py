"""
Setup Wizard API endpoints.

Provides endpoints for the web-based setup wizard:
- Check if setup is needed (first-run detection)
- Get hardware info (from data/hardware.json written by host detection script)
- Get available profiles and modules
- Apply configuration (profile + modules + .env update)
- Pull models with real-time SSE progress (Ollama models + voice runtime
  packages + voice ASR/TTS models — one provider-aware sequential plan)
- Mark setup as complete

All persistent data files are stored in the data/ directory:
- data/hardware.json   — hardware detection results
- data/.setup-complete — marker file indicating setup is done
- data/models/voice/   — ASR/TTS models + .manifest.json (voice installer)

SSE event protocol (identical names for every provider; every event carries
`provider` + `kind`):
- pull_start_all {total}
- pull_start  {model, module, provider, kind, index, total_models}
- pull_progress {model, module, provider, kind, index, total_models,
                 status, completed, total, percent}   (byte-accurate only)
- pull_status  {model, module, provider, kind, index?, total_models?,
                status, output}                       (indeterminate stages)
- pull_done    {model, module, provider, kind, index, total_models,
                already_installed?}
- pull_error   {model, module, provider, kind, error, index?, total_models?}
- pull_all_done {total}
- setup_error  {error}

`kind` is "ollama" | "voice_runtime" | "voice_model"; `provider` is
"ollama" | "pip" | "qwen3-asr" | "pocket-tts" (from profiles.yml).
"""

import json
import logging
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.base import get_tool_registry
from app.config import settings, VALID_PROFILES
from app.services import voice_model_installer

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── Constants ────────────────────────────────────────────────────

SETUP_COMPLETE_FILENAME = ".setup-complete"


# ─── Request/Response Models ─────────────────────────────────────


class ApplySetupRequest(BaseModel):
    """Request body for applying setup configuration."""

    profile: str
    enabled_modules: List[str]  # List of module names to enable


class InstallSetupModelsRequest(BaseModel):
    """Request body for installing models during setup."""

    profile: str
    enabled_modules: List[str]


# ─── Helpers ──────────────────────────────────────────────────────


def _get_project_root() -> Path:
    """Get the project root directory."""
    candidates = [
        Path("/app"),  # Inside Docker container
        Path(__file__).parent.parent.parent.parent,  # Dev: backend/../
    ]
    for p in candidates:
        if (p / "profiles.yml").exists() or (p / ".env").exists():
            return p
    return Path("/app")


def _get_data_dir() -> Path:
    """Get the data directory for persistent files.

    In Docker: /app/data (mounted volume)
    In dev: backend/../data
    """
    root = _get_project_root()
    data_dir = root / "data"
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    return data_dir


def _is_setup_complete() -> bool:
    """Check if setup has been completed previously."""
    data_dir = _get_data_dir()
    return (data_dir / SETUP_COMPLETE_FILENAME).exists()


def _load_hardware_info() -> Optional[dict]:
    """Load hardware info from data/hardware.json (written by detect-hardware.sh)."""
    candidates = [
        _get_data_dir() / "hardware.json",
    ]

    for p in candidates:
        if p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to load hardware.json from %s: %s", p, e)
    return None


def _mark_setup_complete() -> None:
    """Create the data/.setup-complete marker file."""
    data_dir = _get_data_dir()
    marker_path = data_dir / SETUP_COMPLETE_FILENAME
    try:
        marker_data = {
            "profile": settings.HARDWARE_PROFILE,
            "enabled_modules": sorted(settings.get_enabled_module_names()),
        }
        marker_path.write_text(json.dumps(marker_data, indent=2), encoding="utf-8")
        logger.info("Setup marked as complete: %s", marker_path)
    except OSError as e:
        logger.error("Failed to create setup-complete marker: %s", e)


def _apply_profile_and_modules(profile: str, enabled_modules: List[str]) -> None:
    """Apply the selected profile and modules to the running configuration.

    Updates in-memory settings and persists to .env and state files.
    """
    # Validate profile
    if profile not in VALID_PROFILES:
        raise ValueError(f"Invalid profile: {profile}")

    # Update HARDWARE_PROFILE
    old_profile = settings.HARDWARE_PROFILE
    settings.HARDWARE_PROFILE = profile

    # Clear cached profile data so it reloads with the new profile
    settings._profiles = None
    settings._modules = None
    # Voice config cache is derived from profiles.yml too — clear it so
    # the new profile's `voice:` overrides (if any) take effect.
    settings._voice_config = None

    # Update enabled modules
    # Always include required modules
    required_modules = {
        name for name, mod in settings.get_modules().items() if mod.required
    }
    target_enabled = required_modules | set(enabled_modules)

    # Clear cached enabled modules and set new values
    settings._enabled_modules_set = target_enabled
    settings.ENABLED_MODULES = ",".join(sorted(target_enabled))

    # Persist module state
    settings._persist_module_state()

    # Update .env file
    settings._update_env_file("HARDWARE_PROFILE", profile)
    settings._update_env_file("ENABLED_MODULES", ",".join(sorted(target_enabled)))

    logger.info(
        "Setup applied: profile=%s (was %s), modules=%s",
        profile,
        old_profile,
        sorted(target_enabled),
    )


def _apply_module_tools_for_setup(enabled_modules: List[str]) -> None:
    """Apply tool registration/unregistration based on the setup configuration."""
    registry = get_tool_registry()
    modules = settings.get_modules()

    for name, module in modules.items():
        is_enabled = name in enabled_modules or module.required
        if is_enabled:
            # Ensure tools are registered
            restored = registry.restore_many(module.tools)
            if restored > 0:
                logger.info("Setup: restored %d tools for module '%s'", restored, name)
        else:
            # Unregister tools for disabled modules
            removed = registry.unregister_many(module.tools)
            if removed > 0:
                logger.info(
                    "Setup: unregistered %d tools for module '%s'", removed, name
                )


def _get_models_to_pull(profile: str, enabled_modules: List[str]) -> List[dict]:
    """Get the full list of models that need to be pulled for a given profile + modules.

    The plan is provider-aware and sequential:
      1. Ollama models (profile + optional modules)   — kind "ollama"
      2. Voice runtime pip packages (missing only)     — kind "voice_runtime"
      3. Voice ASR + TTS models (from profiles.yml)    — kind "voice_model"

    Voice entries are appended when voice is enabled for the profile (see
    voice_model_installer.voice_enabled_for_profile — with no `voice` module
    defined in modules.yml, voice is a core capability and is always set up,
    per the "setup wizard must download ASR/TTS" requirement).
    """
    models = []

    # Required module models (from profiles.yml)
    profile_config = settings.get_profiles().get(profile)
    if profile_config:
        for m in profile_config.models:
            models.append(
                {
                    "id": m.id,
                    "role": m.role,
                    "description": m.description,
                    "size": m.size,
                    "module": "assistant",
                    "provider": "ollama",
                    "kind": "ollama",
                }
            )

    # Optional module models
    modules = settings.get_modules()
    for module_name in enabled_modules:
        module = modules.get(module_name)
        if module and not module.required and module.is_available_for_profile(profile):
            for m in module.get_models_for_profile(profile):
                models.append(
                    {
                        "id": m.id,
                        "role": m.role,
                        "description": m.description,
                        "size": m.size,
                        "module": module_name,
                        "provider": "ollama",
                        "kind": "ollama",
                    }
                )

    # Voice runtime packages (one per missing pip package) + voice models,
    # resolved from the profiles.yml `voice:` section (single source of
    # truth — changing profiles.yml changes what gets installed).
    if voice_model_installer.voice_enabled_for_profile(profile, enabled_modules):
        models.extend(voice_model_installer.voice_runtime_entries(profile))
        models.extend(voice_model_installer.voice_model_entries(profile))

    return models


# ─── API Endpoints ────────────────────────────────────────────────


@router.get("/setup/status")
async def get_setup_status():
    """Check whether setup is needed.

    Returns setup_complete=true if the data/.setup-complete marker exists,
    meaning the user has already gone through the setup wizard.

    Also includes a "voice" summary (configured / installed / valid / ready)
    so the wizard can show voice dependency readiness. Prefers the voice
    core's models_store.voice_dependency_status() when present, falling
    back to the installer's own summary — lazy imports keep setup working
    even when the voice package isn't fully present.
    """
    is_complete = _is_setup_complete()

    voice_summary = None
    try:
        from app.voice.models_store import voice_dependency_status  # noqa: PLC0415

        voice_summary = voice_dependency_status()
    except Exception:
        try:
            voice_summary = voice_model_installer.voice_status_summary()
        except Exception as e:
            logger.warning("voice status summary failed: %s", e)
            voice_summary = {"configured": False, "ready": False, "error": str(e)}

    return {
        "setup_complete": is_complete,
        "profile": settings.HARDWARE_PROFILE if is_complete else None,
        "voice": voice_summary,
    }


@router.get("/setup/hardware")
async def get_hardware_info():
    """Get hardware information for the setup wizard.

    Reads from data/hardware.json (written by the host-side detection script).
    Falls back to minimal info if the file doesn't exist.
    """
    hw_info = _load_hardware_info()
    if hw_info:
        return hw_info

    # Fallback: basic info from current settings
    return {
        "platform": "unknown",
        "platform_display": "Unknown",
        "ram_gb": 8,
        "cpu_cores": 1,
        "cpu_name": "Unknown",
        "gpu_type": "none",
        "gpu_vram_mb": 0,
        "gpu_vram_gb": 0,
        "gpu_name": "Unknown",
        "recommended_profile": "cpu_small",
        "ollama_installed": False,
        "ollama_running": False,
        "docker_available": True,
    }


@router.get("/setup/profiles")
async def get_setup_profiles():
    """Get all available hardware profiles for the setup wizard.

    Returns profiles with their labels, descriptions, and model lists
    so the user can choose the right one.
    """
    profiles = settings.get_profiles()
    result = {}
    for name, p in profiles.items():
        result[name] = {
            "description": p.description,
            "label": p.label,
            "engine": p.engine,
            "models": [m.to_dict() for m in p.models],
        }
    return result


@router.get("/setup/modules")
async def get_setup_modules():
    """Get all available modules for the setup wizard.

    Returns modules with profile availability so the wizard can
    show which modules are available for each hardware profile.
    """
    modules = settings.get_modules()
    hw_info = _load_hardware_info()
    ram_gb = hw_info.get("ram_gb", 0) if hw_info else 0
    vram_gb = hw_info.get("gpu_vram_gb", 0) if hw_info else 0

    result = []
    for name, module in modules.items():
        mod_info = {
            "name": name,
            "required": module.required,
            "label": module.label,
            "description": module.description,
            "icon": module.icon,
            "estimated_size": module.estimated_size,
            "minimum_requirements": module.minimum_requirements,
            # Per-profile availability
            "availability": {},
        }

        # Check availability for each profile
        # Also build per-profile model data so the frontend can show
        # exact sizes when the user switches profiles in step 3
        profile_models: dict = {}
        for profile_name in VALID_PROFILES:
            available = module.is_available_for_profile(profile_name)
            mod_info["availability"][profile_name] = available
            if available and not module.required:
                profile_models[profile_name] = [
                    m.to_dict() for m in module.get_models_for_profile(profile_name)
                ]

        # Check if hardware requirements are met
        mod_info["requirements_met"] = module.meets_requirements(ram_gb, vram_gb)

        # Get models for the recommended profile (default view)
        recommended = (
            hw_info.get("recommended_profile", "cpu_small") if hw_info else "cpu_small"
        )
        mod_info["models"] = [
            m.to_dict() for m in module.get_models_for_profile(recommended)
        ]

        # Per-profile model data (for exact size display when user changes profile)
        mod_info["profile_models"] = profile_models

        result.append(mod_info)

    return {"modules": result}


@router.post("/setup/apply")
async def apply_setup(request: ApplySetupRequest):
    """Apply the setup configuration chosen by the user.

    This endpoint:
    1. Validates the selected profile and modules
    2. Updates the in-memory configuration
    3. Persists changes to .env and state files
    4. Does NOT pull models (that's a separate endpoint)
    """
    # Validate profile
    if request.profile not in VALID_PROFILES:
        format_profiles = ", ".join(sorted(VALID_PROFILES))
        raise HTTPException(
            status_code=400,
            detail=f"Invalid profile '{request.profile}'. Must be one of: {format_profiles}",
        )

    # Validate modules
    modules = settings.get_modules()
    for mod_name in request.enabled_modules:
        if mod_name not in modules:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown module '{mod_name}'",
            )
        module = modules[mod_name]
        if not module.is_available_for_profile(request.profile):
            raise HTTPException(
                status_code=400,
                detail=f"Module '{mod_name}' is not available for profile '{request.profile}'",
            )

    # Apply the configuration
    try:
        _apply_profile_and_modules(request.profile, request.enabled_modules)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error("Failed to apply setup: %s", e)
        raise HTTPException(status_code=500, detail=f"Failed to apply setup: {e}")

    # Apply module tools
    _apply_module_tools_for_setup(request.enabled_modules)

    # Get models to pull for confirmation
    models_to_pull = _get_models_to_pull(request.profile, request.enabled_modules)

    return {
        "status": "applied",
        "profile": request.profile,
        "enabled_modules": sorted(settings.get_enabled_module_names()),
        "models_to_pull": models_to_pull,
    }


@router.post("/setup/pull-models")
async def pull_setup_models(request: InstallSetupModelsRequest):
    """Pull all required models for the selected profile and modules.

    Streams SSE events with real-time progress for each model being pulled.
    This is the same streaming mechanism used by /modules/install but pulls
    ALL models (profile + module + voice) in sequence.

    Single sequential, provider-aware plan with consistent indexing:
      1. Ollama models          (kind "ollama",      provider "ollama")
      2. Voice runtime packages (kind "voice_runtime", provider "pip")
      3. Voice ASR + TTS models (kind "voice_model",  provider from profiles.yml)

    Event names are unchanged from the original Ollama-only stream so the
    current frontend keeps working; every event now also carries
    ``provider`` + ``kind``. Ollama reachability is only required when the
    plan actually contains Ollama models (voice-only plans proceed even
    with Ollama down).
    """
    # Validate profile
    if request.profile not in VALID_PROFILES:
        raise HTTPException(
            status_code=400, detail=f"Invalid profile: {request.profile}"
        )

    models_to_pull = _get_models_to_pull(request.profile, request.enabled_modules)
    if not models_to_pull:
        return {"status": "no_models", "message": "No models to pull"}

    ollama_models = [m for m in models_to_pull if m.get("kind", "ollama") == "ollama"]
    has_voice_entries = any(
        m.get("kind") in ("voice_model", "voice_runtime") for m in models_to_pull
    )

    async def _stream_pull():
        """Stream model pull progress as SSE events (Ollama + voice)."""
        total_models = len(models_to_pull)

        # First, check if Ollama is reachable — only when the plan actually
        # contains Ollama models (voice-only plans must not abort on Ollama
        # being down).
        if ollama_models:
            try:
                async with httpx.AsyncClient(timeout=10.0) as client:
                    resp = await client.get(f"{settings.OLLAMA_BASE_URL}/api/version")
                    if resp.status_code != 200:
                        payload = {
                            "event": "setup_error",
                            "error": "Ollama is not reachable. Please start Ollama first.",
                        }
                        yield f"data: {json.dumps(payload)}\n\n"
                        return
            except Exception as e:
                payload = {
                    "event": "setup_error",
                    "error": f"Cannot connect to Ollama: {e}",
                }
                yield f"data: {json.dumps(payload)}\n\n"
                return

        # Emit total count
        yield f"data: {json.dumps({'event': 'pull_start_all', 'total': total_models})}\n\n"

        for i, model_info in enumerate(models_to_pull):
            model_id = model_info["id"]
            module_name = model_info["module"]
            kind = model_info.get("kind", "ollama")
            provider = model_info.get("provider", "ollama")

            # Voice entries are installed by the provider-aware installer
            # (delegated below, after the Ollama loop, to keep the original
            # sequential order: ollama → voice runtime → voice models).
            if kind != "ollama":
                continue

            # Emit start event for this model
            payload = {
                "event": "pull_start",
                "model": model_id,
                "module": module_name,
                "provider": provider,
                "kind": kind,
                "index": i,
                "total_models": total_models,
            }
            yield f"data: {json.dumps(payload)}\n\n"

            try:
                async with httpx.AsyncClient(timeout=1800.0) as client:
                    async with client.stream(
                        "POST",
                        f"{settings.OLLAMA_BASE_URL}/api/pull",
                        json={"name": model_id, "stream": True},
                    ) as response:
                        if response.status_code != 200:
                            error_text = await response.aread()
                            payload = {
                                "event": "pull_error",
                                "model": model_id,
                                "module": module_name,
                                "provider": provider,
                                "kind": kind,
                                "error": error_text.decode()[:200],
                            }
                            yield f"data: {json.dumps(payload)}\n\n"
                            continue

                        async for line in response.aiter_lines():
                            if not line.strip():
                                continue
                            try:
                                chunk = json.loads(line)
                                status = chunk.get("status", "")

                                if "pulling" in status:
                                    completed = chunk.get("completed", 0)
                                    total = chunk.get("total", 0)
                                    pct = (
                                        int(completed / total * 100) if total > 0 else 0
                                    )
                                    payload = {
                                        "event": "pull_progress",
                                        "model": model_id,
                                        "module": module_name,
                                        "provider": provider,
                                        "kind": kind,
                                        "status": status,
                                        "completed": completed,
                                        "total": total,
                                        "percent": pct,
                                        "index": i,
                                        "total_models": total_models,
                                    }
                                    yield f"data: {json.dumps(payload)}\n\n"
                                elif status == "success":
                                    payload = {
                                        "event": "pull_done",
                                        "model": model_id,
                                        "module": module_name,
                                        "provider": provider,
                                        "kind": kind,
                                        "index": i,
                                        "total": total_models,
                                    }
                                    yield f"data: {json.dumps(payload)}\n\n"
                                else:
                                    payload = {
                                        "event": "pull_status",
                                        "model": model_id,
                                        "module": module_name,
                                        "provider": provider,
                                        "kind": kind,
                                        "status": status,
                                    }
                                    yield f"data: {json.dumps(payload)}\n\n"
                            except json.JSONDecodeError:
                                continue

            except httpx.ConnectError:
                payload = {
                    "event": "pull_error",
                    "model": model_id,
                    "module": module_name,
                    "provider": provider,
                    "kind": kind,
                    "error": "Cannot connect to Ollama. Is it running?",
                }
                yield f"data: {json.dumps(payload)}\n\n"
            except httpx.TimeoutException:
                payload = {
                    "event": "pull_error",
                    "model": model_id,
                    "module": module_name,
                    "provider": provider,
                    "kind": kind,
                    "error": "Model pull timed out",
                }
                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as e:
                payload = {
                    "event": "pull_error",
                    "model": model_id,
                    "module": module_name,
                    "provider": provider,
                    "kind": kind,
                    "error": str(e)[:200],
                }
                yield f"data: {json.dumps(payload)}\n\n"

        # Voice dependencies (runtime pip packages + ASR/TTS models).
        # The installer yields the same event dicts (with provider/kind);
        # indexes continue after the Ollama entries for one sequential plan.
        if has_voice_entries:
            voice_stream = voice_model_installer.stream_install_voice_dependencies(
                request.profile,
                request.enabled_modules,
                index_offset=len(ollama_models),
                total_models=total_models,
            )
            try:
                async for event in voice_stream:
                    yield f"data: {json.dumps(event)}\n\n"
            finally:
                # Client disconnect → close the inner generator (kills pip
                # subprocesses and closes HTTP download streams).
                await voice_stream.aclose()

        # All models done
        yield f"data: {json.dumps({'event': 'pull_all_done', 'total': total_models})}\n\n"

    return StreamingResponse(
        _stream_pull(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/setup/complete")
async def complete_setup():
    """Mark the setup as complete.

    Creates the data/.setup-complete marker file so the app knows
    to skip the setup wizard on future startups.
    """
    _mark_setup_complete()
    return {"status": "complete", "profile": settings.HARDWARE_PROFILE}
