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
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from app.agent.base import (
    BaseTool,
    ToolCall,
    ToolResult,
    ToolType,
    ToolArgumentError,
    tool_registry,
)
from app.agent.tools.config_base import (
    ConfigField,
    ToolConfigDefinition,
    register_config,
)
from app.services.report_gen import generate_report
from app.agent.tools import config_store
from app.services import artifacts as artifact_service
from app.db.session import async_session_factory
from fastapi import HTTPException

logger = logging.getLogger(__name__)

# Fallback default format when the persisted configuration predates
# the setting — the actual value lives in the tool config.
DEFAULT_FORMAT = "pdf"


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[report_gen_tool] {formatted}", flush=True)


class ReportGenTool(BaseTool):
    """Generate a formatted report (PDF or DOCX) about a topic."""

    name = "use_report_gen"
    guidance_prompt = "agent_reports"
    output_formats = ("pdf", "docx")
    display_name = "Report Generation"
    description = (
        "Generate a formatted report (PDF or DOCX) about a topic. "
        "Use when the user asks for a report, document, or deliverable file. "
        "The writer receives evidence from source tools used in this request. "
        "Compile supported findings, preserve citations and disclose evidence gaps. "
        "The report is saved as a downloadable file."
    )
    tool_type = ToolType.IMAGE_GEN  # reuse — produces a file deliverable

    param_aliases = {
        "topic": "topic",
        "outline": "outline",
        "requirements": "requirements",
        "format": "format",
    }

    def get_parameters(self) -> dict:
        return {
            "topic": {
                "type": "string",
                "description": (
                    "What the report is about (e.g. 'Climate change impacts on agriculture')"
                ),
            },
            "outline": {
                "type": "string",
                "description": (
                    "Optional section outline to guide the report structure"
                ),
            },
            "requirements": {
                "type": "string",
                "maxLength": 8000,
                "description": "Optional detail, length and style requirements, separate from section headings. Original user requirements take priority.",
            },
            "length_multiplier": {
                "type": "number",
                "minimum": 0.1,
                "maximum": 10,
                "description": "Optional requested size ratio for EACH chapter of source_artifact (e.g. 2 for twice as long). Omit unless the user specifies a relative length.",
            },
            "source_artifact": {
                "type": "object",
                "additionalProperties": False,
                "description": "For rewriting/expanding a saved report: exact artifact_id and optional version. The writer reads the entire saved draft, not only chat excerpts.",
                "properties": {
                    "artifact_id": {"type": "string"},
                    "version": {"type": "integer", "minimum": 1},
                },
                "required": ["artifact_id"],
            },
            "format": {
                "type": "string",
                "enum": ["pdf", "docx"],
                "description": (
                    'Output format. Default: "pdf". '
                    'Use "docx" if the user asks for a Word document.'
                ),
            },
        }

    def get_required_params(self) -> List[str]:
        return ["topic"]

    async def execute(
        self,
        *,
        topic: str,
        outline: Optional[str] = None,
        requirements: Optional[str] = None,
        source_artifact: Optional[dict] = None,
        length_multiplier: Optional[float] = None,
        format: Optional[str] = None,
        **kwargs,
    ) -> ToolResult:
        """Generate the report and return deliverable metadata.

        ``format`` honors the LLM's explicit choice; when omitted it
        falls back to the configured default (Brain ▸ Tools ▸ Report
        Generation).
        """
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
                failure_kind="validation",
            )

        fmt = format if format in ("pdf", "docx") else _configured_default_format()
        _log(
            "execute START topic=%r format=%s outline=%s",
            topic[:60],
            fmt,
            bool(outline),
        )

        try:
            # Model override (Brain ▸ Tools) when set, else the service
            # falls back to the Report task slot (Settings ▸ AI ▸ Models).
            model_override = await config_store.tool_model_override(self.name)
            draft = (
                await _load_report_draft(source_artifact, kwargs.get("conversation_id"))
                if source_artifact is not None
                else None
            )
            extra = {}
            if length_multiplier is not None:
                if (
                    draft is None
                    or isinstance(length_multiplier, bool)
                    or not isinstance(length_multiplier, (int, float))
                    or not 0.1 <= length_multiplier <= 10
                ):
                    raise ToolArgumentError(
                        "length_multiplier needs a source_artifact and must be between 0.1 and 10."
                    )
                draft["length_multiplier"] = length_multiplier
            for key, value in (
                ("requirements", requirements),
                ("user_request", kwargs.get("_user_request")),
                ("evidence", kwargs.get("_evidence")),
                ("draft", draft),
            ):
                if value:
                    extra[key] = value
            if kwargs.get("_event_queue") is not None:

                async def progress(update):
                    tool_call.progress = update
                    await kwargs["_event_queue"].put(
                        {
                            "id": kwargs.get("_parent_tool_call_id", tool_call.id),
                            "progress": update,
                        }
                    )

                extra["progress"] = progress
            result = await generate_report(
                topic=topic.strip(),
                outline=outline,
                format=fmt,
                model=model_override,
                **extra,
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
                    **(
                        {"statistics": result["statistics"]}
                        if result.get("statistics")
                        else {}
                    ),
                }
            ]

            output_msg = (
                f"Report generated successfully: {result['filename']} ({result['format'].upper()})."
                f" The user can download it using the deliverable badge shown in the chat."
                f" Tell the user the report is ready for download."
            )
            if result.get("statistics"):
                output_msg += (
                    "\nDocument statistics (measured, not a quality evaluation): "
                    + json.dumps(result["statistics"])
                )
            if result.get("source_statistics"):
                output_msg += "\nSource draft statistics: " + json.dumps(
                    result["source_statistics"]
                )
            if result.get("warnings"):
                output_msg += "\nWarnings: " + "; ".join(result["warnings"])

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

        except (ToolArgumentError, HTTPException) as exc:
            return self.invalid_arguments(
                str(exc.detail) if isinstance(exc, HTTPException) else str(exc),
                tool_call,
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


def generation_key(arguments):
    return (
        arguments.get("topic", "").strip().casefold(),
        arguments.get("format") or _configured_default_format(),
    )


async def _load_report_draft(reference, conversation_id):
    """Read a complete, version-pinned draft using normal artifact permissions."""
    try:
        identifier = uuid.UUID(str(reference["artifact_id"]))
        conversation = uuid.UUID(str(conversation_id)) if conversation_id else None
        number = reference.get("version")
        if number is not None and (
            isinstance(number, bool) or not isinstance(number, int) or number < 1
        ):
            raise ValueError("Version must be positive")
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ToolArgumentError(
            "Use an exact source_artifact UUID and a positive version from attachments/read_artifact."
        ) from exc
    async with async_session_factory() as db:
        artifact = await artifact_service.get_artifact(
            db, identifier, conversation, scoped=True
        )
        if artifact.kind != "report":
            raise ToolArgumentError(
                "source_artifact must reference a saved report; read other sources with their source tools."
            )
        version = await artifact_service.get_version(db, artifact, number)
        if not isinstance(version.source, str) or not version.source.strip():
            raise ToolArgumentError(
                "The saved report has no readable Markdown draft; it cannot be reliably expanded."
            )
        draft = {
            "artifact_id": str(artifact.id),
            "version": version.number,
            "title": artifact.title,
            "source": version.source,
        }
        await db.commit()
        return draft


# ── Configuration definition (Brain ▸ Tools ▸ Report Generation) ──


def _configured_default_format() -> str:
    """The persisted default output format (custom.default_format)."""
    cfg = config_store.get_tool_config("use_report_gen") or {}
    value = (cfg.get("custom") or {}).get("default_format")
    return value if value in ("pdf", "docx") else DEFAULT_FORMAT


def _validate_report_custom(custom: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the custom settings; returns the cleaned object."""
    if not isinstance(custom, dict):
        raise ValueError("'custom' must be an object")
    if "default_format" in custom:
        if custom["default_format"] not in ("pdf", "docx", None):
            raise ValueError("'default_format' must be 'pdf' or 'docx'")
    return custom


REPORT_GEN_CONFIG = ToolConfigDefinition(
    tool_name="use_report_gen",
    display_name="Report Generation",
    description=(
        "Generate a formatted report (PDF or DOCX) about a topic. "
        "Use when the user asks for a report, document, or deliverable file. "
        "The report is generated from scratch by the AI and saved as a downloadable file."
    ),
    model_task_slot="report",
    custom_defaults={"default_format": DEFAULT_FORMAT},
    custom_schema=[
        {
            "key": "output",
            "label": "Output",
            "fields": [
                ConfigField(
                    "default_format",
                    "Default format",
                    "select",
                    options=[
                        {"value": "pdf", "label": "PDF"},
                        {"value": "docx", "label": "Word (.docx)"},
                    ],
                    default=DEFAULT_FORMAT,
                    help=(
                        "Used when the request doesn't specify a format — "
                        "the model can still explicitly ask for the other"
                    ),
                ).to_dict(),
            ],
        },
    ],
    validate_custom=_validate_report_custom,
)

register_config(REPORT_GEN_CONFIG)


# Register the tool
tool_registry.register(ReportGenTool())
logger.info("[report_gen_tool] ReportGenTool registered")
