"""
Vision tool using Ollama's multimodal models.

When the user sends an image alongside their message, the agent
automatically uses this tool to describe/analyze the image using
the configured vision model (from profiles.yml).
"""

import logging
import time
from typing import Optional

import httpx

from app.agent.base import BaseTool, ToolResult, ToolCall, ToolType, tool_registry
from app.config import settings

logger = logging.getLogger(__name__)


class VisionTool(BaseTool):
    name = "use_vision"
    description = (
        "Analyze an image using a vision model. "
        "Provide a base64-encoded image and an optional prompt describing "
        "what you want to know about the image. "
        "This tool is automatically invoked when the user sends an image."
    )
    tool_type = ToolType.VISION

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
        resolved_model = model or settings.resolve_model("default_vision")
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

        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"{settings.OLLAMA_BASE_URL}/api/chat",
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


# Register the tool
tool_registry.register(VisionTool())
