"""
Vision tool using Ollama's multimodal models.

When the user sends an image alongside their message, the agent
automatically uses this tool to describe/analyze the image using
the configured vision model (from profiles.yml).

The inference timeout is configurable (Brain ▸ Tools ▸ Vision).
"""

import logging
import time
from typing import Any, Dict, Optional, List

import httpx

from app.agent.base import BaseTool, ToolResult, ToolCall, ToolType, tool_registry
from app.config import settings
from app.agent.tools.config_base import (
    ConfigField,
    ToolConfigDefinition,
    register_config,
)

logger = logging.getLogger(__name__)

# Fallback inference timeout (seconds) when the persisted configuration
# predates the setting — the actual value lives in the tool config.
DEFAULT_TIMEOUT_S = 120


class VisionTool(BaseTool):
    name = "use_vision"
    display_name = "Vision"
    description = (
        "Analyze an image using a vision model. "
        "Automatically invoked when the user sends an image."
    )
    tool_type = ToolType.VISION

    def get_parameters(self) -> dict:
        return {
            "prompt": {
                "type": "string",
                "description": "What to look for in the image",
            },
        }

    def get_required_params(self) -> List[str]:
        return []

    async def execute(
        self,
        *,
        image_base64: str,
        prompt: str = "Describe this image in detail.",
        model: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        """Send an image to the vision model and return the description."""
        start = time.time()
        # Model priority: explicit kwarg (agent passes the Brain ▸ Tools
        # override) → tool config override → profile default role.
        if model:
            resolved_model = model
        else:
            from app.agent.tools import config_store

            resolved_model = await config_store.resolve_tool_model(
                "use_vision", fallback_role="default_vision"
            ) or settings.resolve_model("default_vision")
        tool_call = ToolCall(
            id=f"tc-vision-{int(start * 1000)}",
            type=self.tool_type,
            name=self.name,
            status="running",
            title="Analyzing image",
            started_at=start,
        )

        try:
            description = await self._query_vision_model(
                resolved_model, image_base64, prompt
            )
            tool_call.status = "completed"
            tool_call.completed_at = time.time()
            tool_call.image_description = description

            return ToolResult(
                success=True,
                output=f"Image analysis: {description}",
                tool_call=tool_call,
            )

        except Exception as e:
            logger.error("Vision analysis failed: %s", e)
            tool_call.status = "error"
            tool_call.completed_at = time.time()
            tool_call.error = str(e)
            return ToolResult(
                success=False,
                output=f"Vision analysis failed: {e}",
                tool_call=tool_call,
            )

    async def _query_vision_model(
        self, model: str, image_base64: str, prompt: str
    ) -> str:
        """Query Ollama's vision model with an image."""
        # Strip data URI prefix if present (e.g. "data:image/png;base64,")
        if "," in image_base64 and image_base64.startswith("data:"):
            image_base64 = image_base64.split(",", 1)[1]

        from app.config import settings as app_settings

        base_url = _configured_base_url() or app_settings.OLLAMA_BASE_URL
        async with httpx.AsyncClient(timeout=float(_configured_timeout_s())) as client:
            response = await client.post(
                f"{base_url.rstrip('/')}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "user",
                            "content": prompt,
                            "images": [image_base64],
                        }
                    ],
                    "stream": False,
                },
            )
            response.raise_for_status()
            data = response.json()
            return data.get("message", {}).get("content", "No description available")


# ── Configuration definition (Brain ▸ Tools ▸ Vision) ─────────────


def _configured_timeout_s() -> int:
    """The persisted inference timeout (custom.timeout_s)."""
    from app.agent.tools import config_store

    cfg = config_store.get_tool_config("use_vision") or {}
    try:
        value = int((cfg.get("custom") or {}).get("timeout_s", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        return DEFAULT_TIMEOUT_S
    return max(5, min(600, value))


def _configured_base_url():
    """The persisted Ollama base URL override (custom.base_url)."""
    from app.agent.tools import config_store

    cfg = config_store.get_tool_config("use_vision") or {}
    base = (cfg.get("custom") or {}).get("base_url")
    if isinstance(base, str) and base.strip():
        return base.strip()
    return None


def _validate_vision_custom(custom: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the custom settings; returns the cleaned object."""
    if not isinstance(custom, dict):
        raise ValueError("'custom' must be an object")
    if "timeout_s" in custom:
        try:
            v = float(custom["timeout_s"])
        except (TypeError, ValueError):
            raise ValueError("'timeout_s' must be a number")
        if v <= 0 or v > 600:
            raise ValueError("'timeout_s' must be between 1 and 600")
        custom["timeout_s"] = int(v)
    if "base_url" in custom:
        base = custom["base_url"]
        if base is not None:
            if not isinstance(base, str) or not base.strip():
                raise ValueError(
                    "'base_url' must be a non-empty URL (or empty to use "
                    "the default Ollama server)"
                )
            if not base.strip().startswith(("http://", "https://")):
                raise ValueError("'base_url' must start with http:// or https://")
    return custom


VISION_CONFIG = ToolConfigDefinition(
    tool_name="use_vision",
    display_name="Vision",
    description=(
        "Analyze an image using a vision model. "
        "Automatically invoked when the user sends an image."
    ),
    model_fallback_role="default_vision",
    custom_defaults={
        "timeout_s": DEFAULT_TIMEOUT_S,
        "base_url": None,  # None → settings.OLLAMA_BASE_URL
    },
    custom_schema=[
        {
            "key": "inference",
            "label": "Inference",
            "fields": [
                ConfigField(
                    "timeout_s",
                    "Timeout (s)",
                    "int",
                    default=DEFAULT_TIMEOUT_S,
                    help=(
                        "Maximum seconds to wait for the vision model — "
                        "large images on CPU can be slow"
                    ),
                ).to_dict(),
                ConfigField(
                    "base_url",
                    "Base URL",
                    "string",
                    help="Ollama server (empty = the global Ollama URL)",
                    placeholder="http://localhost:11434",
                ).to_dict(),
            ],
        },
    ],
    validate_custom=_validate_vision_custom,
)

register_config(VISION_CONFIG)


# Register the tool
tool_registry.register(VisionTool())
