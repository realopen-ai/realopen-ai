from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings
from app.services import model_prefs
from app.services import providers

router = APIRouter()


@router.get("/profile/models")
async def get_profile_models():
    """Get all available models for the current hardware profile.

    Returns the list from profiles.yml for the active HARDWARE_PROFILE,
    plus any models from enabled optional modules.
    The frontend uses this to populate the model selector dropdown.
    """
    profile = settings.get_current_profile()
    return {
        "profile": settings.HARDWARE_PROFILE,
        "label": profile.label,
        "engine": profile.engine,
        "description": profile.description,
        "models": settings.get_available_models(),
    }


@router.get("/profile")
async def get_profile():
    """Get current hardware profile info."""
    profile = settings.get_current_profile()
    return {
        "profile": settings.HARDWARE_PROFILE,
        "label": profile.label,
        "engine": profile.engine,
        "description": profile.description,
        "default_model": profile.get_default_model(),
        "model_count": len(profile.models),
    }


@router.get("/profiles")
async def get_all_profiles():
    """Get all hardware profiles (for admin/settings UI)."""
    profiles = settings.get_profiles()
    return {
        name: {
            "description": p.description,
            "label": p.label,
            "engine": p.engine,
            "models": [m.to_dict() for m in p.models],
        }
        for name, p in profiles.items()
    }


# ─── AI tab: model picker + per-task preferences ─────────────────────


@router.get("/models/available")
async def get_available_models():
    """Everything the Settings ▸ AI ▸ Models tab needs in one call.

    Returns the merged model list (Ollama installed + profile defaults +
    cloud providers when connected) and the per-task slot overview with the
    current selections and their setup defaults.
    """
    listing = await providers.list_available_models()
    tasks = await model_prefs.task_overview()
    return {**listing, "tasks": tasks}


class ModelPreferenceRequest(BaseModel):
    task: str
    # Model id, or null/"" to reset the task to its setup default
    model: str | None = None


@router.put("/models/preferences")
async def set_model_preference(request: ModelPreferenceRequest):
    """Set (or clear) one task slot's model preference."""
    try:
        row = await model_prefs.set_task_model(request.task, request.model)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return row
