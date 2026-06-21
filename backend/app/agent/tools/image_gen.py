"""
Image generation tool using Ollama-compatible image models.
"""

import logging
import time
from typing import List
from base64 import b64decode, b64encode

import httpx

from app.agent.base import BaseTool, ToolResult, ToolCall, ToolType, tool_registry
from app.config import settings, ModuleConfig

logger = logging.getLogger(__name__)


class ImageGenTool(BaseTool):
    name = "use_image_gen"
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
        start = time.time()
        resolved_model = settings.resolve_model("default_image_gen")
        tool_call = ToolCall(
            id=f"tc-imgen-{int(start * 1000)}",
            type=self.tool_type,
            name=self.name,
            status="running",
            title="Generating image",
            started_at=start,
        )

        try:
            # Check if the model is available in Ollama
            if not await self._is_model_available(resolved_model):
                msg = f"Model '{resolved_model}' not installed. Run: ollama pull {resolved_model}"
                tool_call.status = "error"
                tool_call.completed_at = time.time()
                tool_call.error = msg
                return ToolResult(success=False, output=msg, tool_call=tool_call)

            # Generate image using Ollama API
            # Note: This uses the standard Ollama generate endpoint.
            # For image generation models, the response contains image data.
            image_data = await self._generate_image(resolved_model, prompt)

            tool_call.status = "completed"
            tool_call.completed_at = time.time()
            tool_call.image_description = prompt
            tool_call.gen_results = [
                {"type": "image", "data": b64encode(image_data).decode("utf-8")}
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

    async def _is_model_available(self, model: str) -> bool:
        """Check if a model is available in Ollama."""
        return await ModuleConfig.check_model_downloaded(model)

    async def _generate_image(self, model: str, prompt: str) -> bytes:
        """Generate an image using Ollama's API.

        This calls the Ollama /api/generate endpoint which, for image
        generation models, returns base64-encoded image data.
        """
        async with httpx.AsyncClient(timeout=1200.0) as client:
            response = await client.post(
                f"{settings.OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": model,
                    "prompt": prompt,
                    "stream": False,
                },
            )
            response.raise_for_status()
            data = response.json()

            # For image generation models, the response contains base64 image data
            if "images" in data and data["images"]:
                return data["images"][0]

            if model.startswith("x/flux2-klein") and "image" in data and data["image"]:
                return b64decode(data["image"])

            # If no images field, the model may not support image generation
            raise ValueError(
                f"Model '{model}' did not return image data. "
                "It may not support image generation."
            )


# Register the tool — will be conditionally activated based on module state
tool_registry.register(ImageGenTool())
