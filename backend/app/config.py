from pathlib import Path
from typing import Dict, List, Optional

import yaml
from pydantic_settings import BaseSettings


class ModelConfig:
    """A single model entry from profiles.yml."""

    def __init__(self, data: dict):
        self.id: str = data["id"]
        self.type: str = data.get("type", "chat")
        self.role: str = data.get("role", "")
        self.description: str = data.get("description", "")
        self.size: str = data.get("size", "")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "type": self.type,
            "role": self.role,
            "description": self.description,
            "size": self.size,
        }


class ProfileConfig:
    """A hardware profile with its list of models."""

    def __init__(self, name: str, data: dict):
        self.name = name
        self.description: str = data.get("description", "")
        self.models: List[ModelConfig] = [
            ModelConfig(m) for m in data.get("models", [])
        ]

    def get_model_by_role(self, role: str) -> Optional[ModelConfig]:
        """Find a model by its role (e.g. 'default', 'default_vision')."""
        for m in self.models:
            if m.role == role:
                return m
        return None

    def get_models_by_type(self, model_type: str) -> List[ModelConfig]:
        """Find all models of a given type (e.g. 'chat', 'vision')."""
        return [m for m in self.models if m.type == model_type]

    def get_default_model(self) -> str:
        """Return the default chat model ID for this profile."""
        m = self.get_model_by_role("default")
        return m.id if m else "qwen3:4b"


def load_profiles(profiles_path: Optional[str] = None) -> Dict[str, ProfileConfig]:
    """Load all profiles from profiles.yml."""
    if profiles_path is None:
        # Look for profiles.yml in the project root (2 levels up from this file)
        # app/config.py -> app/ -> backend/ -> project_root/
        # But in Docker, profiles.yml is copied to /app/profiles.yml
        candidates = [
            Path("/app/profiles.yml"),  # Inside Docker container
            Path(__file__).parent.parent.parent
            / "profiles.yml",  # Dev: backend/../profiles.yml
        ]
        for p in candidates:
            if p.exists():
                profiles_path = str(p)
                break

    if profiles_path is None or not Path(profiles_path).exists():
        # Fallback: return a minimal default
        return {
            "8gb": ProfileConfig(
                "8gb",
                {
                    "description": "Fallback profile",
                    "models": [
                        {
                            "id": "qwen3:4b",
                            "type": "chat",
                            "role": "default",
                            "description": "Fallback model - profiles.yml not found",
                        }
                    ],
                },
            )
        }

    with open(profiles_path, "r") as f:
        data = yaml.safe_load(f)

    profiles = {}
    for name, pdata in data.get("profiles", {}).items():
        profiles[name] = ProfileConfig(name, pdata)

    return profiles


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Application
    APP_NAME: str = "RealOpen-AI"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # Database
    DATABASE_URL: str = "postgresql+asyncpg://realopen:realopen@postgres:5432/realopen"

    # Ollama (runs natively on host)
    OLLAMA_BASE_URL: str = "http://host.docker.internal:11434"

    # SearxNG
    SEARXNG_BASE_URL: str = "http://searxng:8080"

    # Redis
    REDIS_URL: str = "redis://redis:6379/0"

    # CORS
    CORS_ORIGINS: List[str] = [
        "http://localhost:5173",
        "http://localhost:3000",
        "http://localhost",
    ]

    # Hardware Profile (set by setup.sh from profiles.yml)
    HARDWARE_PROFILE: str = "8gb"
    DEFAULT_MODEL: str = (
        "qwen3:4b"  # Fallback only; resolved from profiles.yml at runtime
    )

    # Ngrok
    NGROK_ENABLED: bool = False
    NGROK_AUTHTOKEN: str = ""

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }

    # Profiles (loaded lazily)
    _profiles: Optional[Dict[str, ProfileConfig]] = None

    def get_profiles(self) -> Dict[str, ProfileConfig]:
        """Load profiles.yml once and cache it."""
        if self._profiles is None:
            self._profiles = load_profiles()
        return self._profiles

    def get_current_profile(self) -> ProfileConfig:
        """Get the ProfileConfig for the current HARDWARE_PROFILE."""
        profiles = self.get_profiles()
        return profiles.get(self.HARDWARE_PROFILE, profiles.get("8gb"))

    def resolve_model(self, model: str) -> str:
        """
        Resolve a model identifier to an actual Ollama model ID.

        Resolution order:
        1. If model is a known role (e.g. "default", "default_vision", "default_code"),
           look it up in the current hardware profile.
        2. If model is a known type (e.g. "chat", "vision", "code"),
           return the model with role="default_<type>" in the current profile.
        3. Otherwise, treat it as a direct Ollama model ID (e.g. "qwen3:4b").
        """
        profile = self.get_current_profile()

        # Try as a role first
        role_model = profile.get_model_by_role(model)
        if role_model:
            return role_model.id

        # Try as a type (e.g. "vision" -> "default_vision")
        type_model = profile.get_model_by_role(f"default_{model}")
        if type_model:
            return type_model.id

        # Direct model ID - pass through
        return model

    def get_available_models(self) -> List[dict]:
        """Get all available models for the current hardware profile."""
        profile = self.get_current_profile()
        return [m.to_dict() for m in profile.models]


settings = Settings()
