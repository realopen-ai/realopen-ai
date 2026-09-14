"""
use_report_gen tool — generates a formatted PDF or DOCX report about a topic.

The tool:
  1. Calls the LLM to generate a well-structured Markdown report.
  2. Converts the Markdown to the requested format (PDF via WeasyPrint,
     DOCX via python-docx).
  3. Saves the file to data/reports/{report_id}.{ext}.
  4. Returns the file metadata as genResults so the frontend can render
     a deliverable badge with a download link.

The deliverable metadata is also persisted to the messages.deliverables
JSONB column by the chat API's _BlockBuilder, so the download badge
survives page refresh.

Keyword-triggered: "report", "document", "pdf", "docx", "deliverable".
"""

from __future__ import annotations

import logging
import time
from typing import List, Optional

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.services.report_gen import generate_report

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[report_gen_tool] {formatted}", flush=True)


class ReportGenTool(BaseTool):
    """Generate a formatted report (PDF or DOCX) about a topic."""

    name = "use_report_gen"
    display_name = "Report Generation"
    description = (
        "Generate a formatted report (PDF or DOCX) about a topic. "
        "Use when the user asks for a report, document, or deliverable file. "
        "The report is generated from scratch by the AI and saved as a downloadable file."
    )
    tool_type = ToolType.IMAGE_GEN  # reuse — produces a file deliverable

    param_aliases = {
        "topic": "topic",
        "outline": "outline",
        "format": "format",
    }

    def get_parameters(self) -> dict:
        return {
            "topic": {
                "type": "string",
                "description": "What the report is about (e.g. 'Climate change impacts on agriculture')",
            },
            "outline": {
                "type": "string",
                "description": "Optional section outline to guide the report structure",
            },
            "format": {
                "type": "string",
                "enum": ["pdf", "docx"],
                "description": 'Output format. Default: "pdf". Use "docx" if the user asks for a Word document.',
            },
        }

    def get_required_params(self) -> List[str]:
        return ["topic"]

    async def execute(
        self,
        *,
        topic: str,
        outline: Optional[str] = None,
        format: str = "pdf",
        **kwargs,
    ) -> ToolResult:
        """Generate the report and return deliverable metadata."""
        start = time.time()
        tool_call = ToolCall(
            id=f"tc-report-{int(start * 1000)}",
            type=self.tool_type,
            name=self.name,
            status="running",
            title="Generating report",
            started_at=start,
        )

        if not topic or not topic.strip():
            tool_call.status = "error"
            tool_call.completed_at = time.time()
            tool_call.error = "Topic cannot be empty"
            return ToolResult(
                success=False,
                output="Report generation failed: topic cannot be empty. Provide a 'topic' field.",
                tool_call=tool_call,
            )

        fmt = format if format in ("pdf", "docx") else "pdf"
        _log(
            "execute START topic=%r format=%s outline=%s",
            topic[:60],
            fmt,
            bool(outline),
        )

        try:
            # Model override (Brain ▸ Tools) when set, else the service
            # falls back to the Report task slot (Settings ▸ AI ▸ Models).
            from app.agent.tools import config_store

            model_override = await config_store.tool_model_override(self.name)
            result = await generate_report(
                topic=topic.strip(),
                outline=outline,
                format=fmt,
                model=model_override,
            )

            tool_call.status = "completed"
            tool_call.completed_at = time.time()
            tool_call.gen_results = [
                {
                    "type": "report",
                    "format": result["format"],
                    "filename": result["filename"],
                    "file_path": result["file_path"],
                    "download_url": result["download_url"],
                    "report_id": result["report_id"],
                    "created_at": result["created_at"],
                }
            ]

            output_msg = (
                f"Report generated successfully: {result['filename']} ({result['format'].upper()})."
                f" The user can download it using the deliverable badge shown in the chat."
                f" Tell the user the report is ready for download."
            )

            _log(
                "execute DONE topic=%r format=%s time=%.1fs",
                topic[:60],
                fmt,
                time.time() - start,
            )

            return ToolResult(
                success=True,
                output=output_msg,
                tool_call=tool_call,
            )

        except Exception as e:
            logger.exception("Report generation failed: %s", e)
            tool_call.status = "error"
            tool_call.completed_at = time.time()
            tool_call.error = str(e)
            return ToolResult(
                success=False,
                output=f"Report generation failed: {e}",
                tool_call=tool_call,
            )


# Register the tool
tool_registry.register(ReportGenTool())
logger.info("[report_gen_tool] ReportGenTool registered")
