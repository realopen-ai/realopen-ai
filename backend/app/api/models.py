from fastapi import APIRouter

from app.config import settings

router = APIRouter()


@router.get("/profile/models")
async def get_profile_models():
    """Get all available models for the current hardware profile.

    Returns the list from profiles.yml for the active HARDWARE_PROFILE.
    The frontend uses this to populate the model selector dropdown.
    """
    profile = settings.get_current_profile()
    return {
        "profile": settings.HARDWARE_PROFILE,
        "description": profile.description,
        "models": settings.get_available_models(),
    }


@router.get("/profile")
async def get_profile():
    """Get current hardware profile info."""
    profile = settings.get_current_profile()
    return {
        "profile": settings.HARDWARE_PROFILE,
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
            "models": [m.to_dict() for m in p.models],
        }
        for name, p in profiles.items()
    }
