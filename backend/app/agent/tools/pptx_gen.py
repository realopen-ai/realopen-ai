"""
use_pptx_gen tool — generates a PowerPoint presentation about a topic.
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.services.pptx_gen import (
    generate_presentation,
    AVAILABLE_TEMPLATES,
    DEFAULT_TEMPLATE,
)

logger = logging.getLogger(__name__)


class PptxGenTool(BaseTool):
    name = "use_pptx_gen"
    description = (
        "Generate a PowerPoint presentation (.pptx) about a topic. "
        "Use when the user asks for a presentation, slides, a slideshow, or a deck. "
        "The presentation is generated from scratch by the AI with speaker notes "
        f"and saved as a downloadable file. Template options: {', '.join(AVAILABLE_TEMPLATES)} "
        f"(default: {DEFAULT_TEMPLATE})."
    )
    tool_type = ToolType.IMAGE_GEN
    param_aliases = {"topic": "topic", "outline": "outline", "template": "template"}

    def get_parameters(self) -> dict:
        return {
            "topic": {
                "type": "string",
                "description": "What the presentation is about",
            },
            "outline": {"type": "string", "description": "Optional slide outline"},
            "template": {
                "type": "string",
                "enum": AVAILABLE_TEMPLATES,
                "description": f"Visual template. Default: '{DEFAULT_TEMPLATE}'. corporate=navy, modern=teal/orange, elegant=purple/gold.",
            },
        }

    def get_required_params(self) -> List[str]:
        return ["topic"]

    async def execute(
        self,
        *,
        topic: str,
        outline: Optional[str] = None,
        template: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        start = time.time()
        tool_call = ToolCall(
            id=f"tc-pptx-{int(start*1000)}",
            type=self.tool_type,
            name=self.name,
            status="running",
            title="Generating presentation",
            started_at=start,
        )
        if not topic or not topic.strip():
            tool_call.status = "error"
            tool_call.completed_at = time.time()
            tool_call.error = "Topic cannot be empty"
            return ToolResult(
                success=False, output="Topic cannot be empty.", tool_call=tool_call
            )
        tpl = template if template in AVAILABLE_TEMPLATES else DEFAULT_TEMPLATE
        try:
            result = await generate_presentation(
                topic=topic.strip(), outline=outline, template=tpl
            )
            tool_call.status = "completed"
            tool_call.completed_at = time.time()
            tool_call.gen_results = [
                {
                    "type": "presentation",
                    "format": "pptx",
                    "filename": result["filename"],
                    "file_path": result["file_path"],
                    "download_url": result["download_url"],
                    "report_id": result["report_id"],
                    "created_at": result["created_at"],
                    "slide_count": result.get("slide_count", 0),
                    "template": result.get("template", tpl),
                }
            ]
            return ToolResult(
                success=True,
                output=f"Presentation generated: {result['filename']} ({result.get('slide_count', 0)} slides). Tell the user it's ready for download.",
                tool_call=tool_call,
            )
        except Exception as e:
            logger.exception("Presentation generation failed: %s", e)
            tool_call.status = "error"
            tool_call.completed_at = time.time()
            tool_call.error = str(e)
            return ToolResult(
                success=False,
                output=f"Presentation generation failed: {e}",
                tool_call=tool_call,
            )


tool_registry.register(PptxGenTool())
