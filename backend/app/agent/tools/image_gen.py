"""
Image generation tool (use_image_gen) — thin execution wrapper.

The provider IMPLEMENTATIONS (Ollama today; ComfyUI planned) live in
app/services/image_gen.py, like the other services; this module holds
the tool itself and its CONFIGURATION definition (Brain ▸ Tools),
which follows the standard layering:

    TOOL IMPLEMENTATION      → app/services/image_gen.py
    TOOL CONFIG DEFINITION   → this file (config_base)
    TOOL CONFIG DATA         → PostgreSQL (tool_configs)

Providers section (like Web Search):
- Ollama  — enabled, base URL (empty = the global Ollama URL),
            generation timeout. The model itself is the standard
            "Model" setting above (override → image task slot).
- ComfyUI — planned (workflow-based local generation). The provider
            matrix already carries the structure; its card appears in
            the UI as "coming soon" until the service implements it.
"""

import logging
import time
from base64 import b64encode
from typing import Any, Dict, List

from app.agent.base import BaseTool, ToolResult, ToolCall, ToolType, tool_registry
from app.agent.tools.config_base import (
    ConfigField,
    ToolConfigDefinition,
    register_config,
)
from app.services import image_gen as image_gen_service

logger = logging.getLogger(__name__)


class ImageGenTool(BaseTool):
    name = "use_image_gen"
    display_name = "Image Generation"
    description = (
        "Generate an image from a text description. Be specific and descriptive."
    )
    tool_type = ToolType.IMAGE_GEN

    def get_parameters(self) -> dict:
        return {
            "prompt": {
                "type": "string",
                "description": "Detailed description of the image to generate",
            },
        }

    def get_required_params(self) -> List[str]:
        return ["prompt"]

    async def execute(self, *, prompt: str, **kwargs) -> ToolResult:
        """Run the enabled image-generation provider (service)."""
        start = time.time()
        # Model priority: Brain ▸ Tools override for this tool → the
        # image task slot (Settings ▸ AI ▸ Models) — local-only model.
        from app.agent.tools import config_store

        resolved_model = await config_store.resolve_tool_model(
            "use_image_gen", task="image"
        )
        tool_call = ToolCall(
            id=f"tc-imgen-{int(start * 1000)}",
            type=self.tool_type,
            name=self.name,
            status="running",
            title="Generating image",
            started_at=start,
        )

        try:
            outcome = await image_gen_service.generate(prompt, model=resolved_model)

            tool_call.status = "completed"
            tool_call.completed_at = time.time()
            tool_call.image_description = prompt
            tool_call.gen_results = [
                {
                    "type": "image",
                    "data": b64encode(outcome["image"]).decode("utf-8"),
                }
            ]

            return ToolResult(
                success=True,
                output=f"Image generated for: {prompt}",
                tool_call=tool_call,
            )

        except Exception as e:
            logger.error("Image generation failed: %s", e)
            tool_call.status = "error"
            tool_call.completed_at = time.time()
            tool_call.error = str(e)
            return ToolResult(
                success=False,
                output=f"Image generation failed: {e}",
                tool_call=tool_call,
            )


# ── Configuration definition (Brain ▸ Tools ▸ Image Generation) ────


def _validate_image_gen_custom(custom: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the custom provider matrix; returns the cleaned object.

    Raises ValueError with a user-facing message on invalid input.
    """
    if not isinstance(custom, dict):
        raise ValueError("'custom' must be an object")
    providers_cfg = custom.get("providers")
    if providers_cfg is not None:
        if not isinstance(providers_cfg, dict):
            raise ValueError("'custom.providers' must be an object")

        known = ("ollama",)
        for key, value in providers_cfg.items():
            if key not in known:
                raise ValueError(
                    f"Unknown image generation provider: {key!r} "
                    f"(known: {', '.join(known)}; comfyui is planned "
                    "but not available yet)"
                )
            if not isinstance(value, dict):
                raise ValueError(f"Provider {key!r} settings must be an object")
            if "enabled" in value and not isinstance(value["enabled"], bool):
                raise ValueError(f"Provider {key!r} 'enabled' must be a boolean")
            if "timeout_s" in value:
                try:
                    v = float(value["timeout_s"])
                except (TypeError, ValueError):
                    raise ValueError(f"Provider {key!r} 'timeout_s' must be a number")
                if v <= 0 or v > 7200:
                    raise ValueError(
                        f"Provider {key!r} 'timeout_s' must be between " "1 and 7200"
                    )
            if key == "ollama" and "base_url" in value:
                base = value["base_url"]
                if base is not None:
                    if not isinstance(base, str) or not base.strip():
                        raise ValueError(
                            "Ollama 'base_url' must be a non-empty URL "
                            "(or empty to use the default)"
                        )
                    if not base.strip().startswith(("http://", "https://")):
                        raise ValueError(
                            "Ollama 'base_url' must start with http:// or " "https://"
                        )
    return custom


IMAGE_GEN_CONFIG = ToolConfigDefinition(
    tool_name="use_image_gen",
    display_name="Image Generation",
    description=(
        "Generate an image from a text description. Be specific and descriptive."
    ),
    model_task_slot="image",
    custom_defaults={
        "providers": {
            "ollama": {
                "enabled": True,
                "base_url": None,  # None → settings.OLLAMA_BASE_URL
                "timeout_s": 600,
            },
            # NOTE: ComfyUI (planned, workflow-based local generation)
            # is intentionally NOT in the matrix yet — the frontend
            # shows a static "coming soon" card. When the service
            # grows generate_comfyui(): add its defaults here, its
            # ConfigFields below, and its name to ``known`` in the
            # validator — the rest of the architecture is ready.
        },
    },
    custom_schema=[
        {
            "key": "providers",
            "label": "Providers",
            "fields": [
                f.to_dict()
                for f in [
                    ConfigField(
                        "providers.ollama.enabled",
                        "Enabled",
                        "bool",
                    ),
                    ConfigField(
                        "providers.ollama.base_url",
                        "Base URL",
                        "string",
                        help="Ollama server (empty = the global Ollama URL)",
                        placeholder="http://localhost:11434",
                    ),
                    ConfigField(
                        "providers.ollama.timeout_s",
                        "Timeout (s)",
                        "int",
                        default=600,
                        help=(
                            "Generation timeout — image diffusion is slow "
                            "on CPU-only machines"
                        ),
                    ),
                ]
            ],
        },
    ],
    validate_custom=_validate_image_gen_custom,
)

register_config(IMAGE_GEN_CONFIG)


# Register the tool — will be conditionally activated based on module state
tool_registry.register(ImageGenTool())
