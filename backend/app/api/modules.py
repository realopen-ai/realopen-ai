"""
Module management API endpoints.

Provides endpoints for:
- Listing all modules with their status
- Toggling modules on/off (runtime, no restart needed)
- Checking model download status
- Installing (pulling) module models
"""

import asyncio
import json
import logging

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.agent.base import get_tool_registry
from app.config import settings, ModuleConfig
from app.core.logger import is_debug

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── Request/Response Models ────────────────────────────────────────


class ToggleModuleRequest(BaseModel):
    module: str
    enabled: bool


class InstallModuleRequest(BaseModel):
    module: str


# ─── Helper Functions ────────────────────────────────────────────────


async def _get_hardware_info() -> dict:
    """Get hardware info for requirement checking.

    Tries to read from data/hardware.json (written by setup detection script).
    """
    from pathlib import Path

    candidates = [
        Path("/app/data/hardware.json"),
        Path(__file__).parent.parent.parent.parent / "data" / "hardware.json",
    ]

    for p in candidates:
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                return data
            except Exception:
                pass

    # Fallback: estimate based on profile
    profile_name = settings.HARDWARE_PROFILE
    if "small" in profile_name:
        return {"ram_gb": 8, "gpu_type": "cpu", "gpu_vram_gb": 0}
    elif "medium" in profile_name:
        return {"ram_gb": 16, "gpu_type": "unknown", "gpu_vram_gb": 8}
    elif "large" in profile_name:
        return {"ram_gb": 32, "gpu_type": "unknown", "gpu_vram_gb": 16}
    else:
        return {"ram_gb": 64, "gpu_type": "unknown", "gpu_vram_gb": 32}


def _apply_module_tools(module_name: str, enabled: bool) -> None:
    """Register or unregister tools for a module in the tool registry."""
    module = settings.get_modules().get(module_name)
    if not module:
        return

    registry = get_tool_registry()

    if enabled:
        restored = registry.restore_many(module.tools)
        logger.info(
            "Module '%s' enabled: restored %d/%d tools",
            module_name,
            restored,
            len(module.tools),
        )
    else:
        removed = registry.unregister_many(module.tools)
        logger.info(
            "Module '%s' disabled: unregistered %d/%d tools",
            module_name,
            removed,
            len(module.tools),
        )


async def _check_module_models_downloaded(
    module: ModuleConfig, profile_name: str
) -> bool:
    """Check if all models for a module are downloaded (parallelized)."""
    module_models = module.get_models_for_profile(profile_name)
    if not module_models:
        return True

    # Check all models in parallel
    tasks = [ModuleConfig.check_model_downloaded(m.id) for m in module_models]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    return all(r is True for r in results)


# ─── API Endpoints ──────────────────────────────────────────────────


@router.get("/modules")
async def list_modules():
    """List all modules with their current status.

    Returns each module's name, label, description, whether it's enabled,
    whether it's available for the current profile, and model download status.
    """
    modules = settings.get_modules()
    profile_name = settings.HARDWARE_PROFILE
    hw_info = await _get_hardware_info()

    ram_gb = hw_info.get("ram_gb", 0)
    vram_gb = hw_info.get("gpu_vram_gb", 0)

    # Build module info list with parallelized model checks
    result = []
    for name, module in modules.items():
        enabled = settings.is_module_enabled(name)
        available = module.is_available_for_profile(profile_name)

        # Check model download status (parallelized per module)
        models_downloaded = True
        if available and not module.required:
            models_downloaded = await _check_module_models_downloaded(
                module, profile_name
            )

        # Check hardware requirements
        requirements_met = True
        if not module.required:
            requirements_met = module.meets_requirements(ram_gb, vram_gb)

        module_dict = module.to_dict(profile_name, enabled, models_downloaded)
        module_dict["requirements_met"] = requirements_met
        module_dict["can_toggle"] = not module.required

        result.append(module_dict)

    return {"modules": result, "profile": profile_name}


@router.post("/modules/toggle")
async def toggle_module(request: ToggleModuleRequest):
    """Toggle a module on or off.

    Required modules cannot be toggled. Changes take effect immediately
    (tools are registered/unregistered in the tool registry) and are
    persisted to the state file.
    """
    module = settings.get_modules().get(request.module)
    if module is None:
        raise HTTPException(
            status_code=404,
            detail=f"Module '{request.module}' not found",
        )

    if module.required:
        raise HTTPException(
            status_code=400,
            detail=f"Cannot toggle required module '{request.module}'",
        )

    # Check if module is available for current profile
    if request.enabled and not module.is_available_for_profile(
        settings.HARDWARE_PROFILE
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Module '{request.module}' is not available for "
                f"hardware profile '{settings.HARDWARE_PROFILE}'"
            ),
        )

    # Check hardware requirements when enabling
    if request.enabled:
        hw_info = await _get_hardware_info()
        ram_gb = hw_info.get("ram_gb", 0)
        vram_gb = hw_info.get("gpu_vram_gb", 0)
        if not module.meets_requirements(ram_gb, vram_gb):
            required_ram = (
                module.minimum_requirements.get("ram", 0)
                if module.minimum_requirements
                else 0
            )
            required_vram = (
                module.minimum_requirements.get("vram", 0)
                if module.minimum_requirements
                else 0
            )
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Module '{request.module}' requires {required_ram} GB RAM and "
                    f"{required_vram} GB VRAM. Your hardware has {ram_gb} GB RAM "
                    f"and {vram_gb} GB VRAM."
                ),
            )

    # Toggle the module
    success = settings.toggle_module(request.module, request.enabled)
    if not success:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to toggle module '{request.module}'",
        )

    # Apply tool changes immediately
    _apply_module_tools(request.module, request.enabled)

    # Check model download status for the response
    models_downloaded = True
    if request.enabled:
        models_downloaded = await _check_module_models_downloaded(
            module, settings.HARDWARE_PROFILE
        )

    return {
        "module": request.module,
        "enabled": request.enabled,
        "models_downloaded": models_downloaded,
    }


@router.get("/modules/{module_name}/status")
async def get_module_status(module_name: str):
    """Get detailed status of a specific module."""
    module = settings.get_modules().get(module_name)
    if module is None:
        raise HTTPException(
            status_code=404,
            detail=f"Module '{module_name}' not found",
        )

    enabled = settings.is_module_enabled(module_name)
    profile_name = settings.HARDWARE_PROFILE
    available = module.is_available_for_profile(profile_name)

    # Check model download status
    models_status = []
    if available and not module.required:
        module_models = module.get_models_for_profile(profile_name)
        # Check in parallel
        tasks = [ModuleConfig.check_model_downloaded(m.id) for m in module_models]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for m, downloaded in zip(module_models, results):
            models_status.append(
                {
                    "id": m.id,
                    "role": m.role,
                    "description": m.description,
                    "size": m.size,
                    "downloaded": downloaded is True,
                }
            )

    return {
        "name": module_name,
        "enabled": enabled,
        "available": available,
        "models": models_status,
    }


@router.post("/modules/install")
async def install_module(request: InstallModuleRequest):
    """Install (pull) models for a module.

    Returns an SSE stream with real-time progress for each model being pulled.
    """
    module = settings.get_modules().get(request.module)
    if module is None:
        raise HTTPException(
            status_code=404,
            detail=f"Module '{request.module}' not found",
        )

    if module.required:
        raise HTTPException(
            status_code=400,
            detail=f"Required module '{request.module}' models are pulled during setup",
        )

    profile_name = settings.HARDWARE_PROFILE
    if not module.is_available_for_profile(profile_name):
        raise HTTPException(
            status_code=400,
            detail=f"Module '{request.module}' is not available for profile '{profile_name}'",
        )

    module_models = module.get_models_for_profile(profile_name)
    if not module_models:
        raise HTTPException(
            status_code=400,
            detail=f"No models defined for module '{request.module}' on profile '{profile_name}'",
        )

    async def _stream_pull():
        """Stream Ollama pull progress as SSE events."""
        total_models = len(module_models)

        for i, model in enumerate(module_models):
            # Check if module is still enabled before pulling
            if not settings.is_module_enabled(request.module):
                payload = {
                    "event": "install_cancelled",
                    "module": request.module,
                    "reason": "Module was disabled during install",
                }
                yield f"data: {json.dumps(payload)}\n\n"
                return

            # Emit start event
            payload = {
                "event": "pull_start",
                "model": model.id,
                "index": i,
                "total": total_models,
            }
            yield f"data: {json.dumps(payload)}\n\n"

            try:
                async with httpx.AsyncClient(timeout=1800.0) as client:
                    async with client.stream(
                        "POST",
                        f"{settings.OLLAMA_BASE_URL}/api/pull",
                        json={"name": model.id, "stream": True},
                    ) as response:
                        if response.status_code != 200:
                            error_text = await response.aread()
                            payload = {
                                "event": "pull_error",
                                "model": model.id,
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
                                if is_debug():
                                    print(chunk)

                                if "pulling" in status:
                                    completed = chunk.get("completed", 0)
                                    total = chunk.get("total", 0)
                                    pct = (
                                        int(completed / total * 100) if total > 0 else 0
                                    )
                                    payload = {
                                        "event": "pull_progress",
                                        "model": model.id,
                                        "status": status,
                                        "completed": completed,
                                        "total": total,
                                        "percent": pct,
                                    }
                                    yield f"data: {json.dumps(payload)}\n\n"
                                elif status == "success":
                                    payload = {
                                        "event": "pull_done",
                                        "model": model.id,
                                        "index": i,
                                        "total": total_models,
                                    }
                                    yield f"data: {json.dumps(payload)}\n\n"
                                else:
                                    payload = {
                                        "event": "pull_status",
                                        "model": model.id,
                                        "status": status,
                                    }
                                    yield f"data: {json.dumps(payload)}\n\n"
                            except json.JSONDecodeError:
                                continue

            except httpx.ConnectError:
                payload = {
                    "event": "pull_error",
                    "model": model.id,
                    "error": "Cannot connect to Ollama. Is it running?",
                }
                yield f"data: {json.dumps(payload)}\n\n"
            except httpx.TimeoutException:
                payload = {
                    "event": "pull_error",
                    "model": model.id,
                    "error": "Model pull timed out",
                }
                yield f"data: {json.dumps(payload)}\n\n"
            except Exception as e:
                logger.exception("Model installation failed: %s", e)
                payload = {
                    "event": "pull_error",
                    "model": model.id,
                    "error": "Model installation failed. Check server logs.",
                }
                yield f"data: {json.dumps(payload)}\n\n"

        # All models done
        yield f"data: {json.dumps({'event': 'install_complete', 'module': request.module})}\n\n"

    return StreamingResponse(
        _stream_pull(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/modules/models/downloaded")
async def check_models_downloaded():
    """Check download status for all models of all enabled optional modules."""
    profile_name = settings.HARDWARE_PROFILE
    result = {}

    for name, module in settings.get_modules().items():
        if module.required:
            continue
        if not settings.is_module_enabled(name):
            continue
        if not module.is_available_for_profile(profile_name):
            continue

        module_models = module.get_models_for_profile(profile_name)
        # Check all models in parallel
        tasks = [ModuleConfig.check_model_downloaded(m.id) for m in module_models]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        model_status = []
        for m, downloaded in zip(module_models, results):
            model_status.append(
                {
                    "id": m.id,
                    "downloaded": downloaded is True,
                }
            )
        result[name] = model_status

    return result
