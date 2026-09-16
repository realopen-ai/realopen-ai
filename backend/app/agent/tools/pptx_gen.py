"""
use_pptx_gen tool — generates a PowerPoint presentation about a topic.

Templates are loaded dynamically from the database. The tool description
and parameter descriptions are updated at request time by the agent
service (via get_dynamic_description) so the LLM can see the actual
available templates with their names and descriptions.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.agent.tools.config_base import (
    ConfigField,
    ToolConfigDefinition,
    register_config,
)
from app.services.pptx_gen import (
    generate_presentation,
    _get_available_templates_from_db,
    _get_templates_with_descriptions,
    AVAILABLE_TEMPLATES,
    DEFAULT_TEMPLATE,
    MAX_SLIDES,
)

logger = logging.getLogger(__name__)


class PptxGenTool(BaseTool):
    name = "use_pptx_gen"
    display_name = "Presentation Generation"
    # Base description — the agent service appends the dynamic template list
    description = (
        "Generate a PowerPoint presentation (.pptx) about a topic. "
        "Use when the user asks for a presentation, slides, a slideshow, or a deck. "
        "The presentation is generated from scratch by the AI with speaker notes "
        "and saved as a downloadable file. See the available templates list below "
        f"to choose the right visual style. Default template: '{DEFAULT_TEMPLATE}'."
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
                "description": (
                    "Template slug (e.g. 'corporate', 'modern', 'elegant'). "
                    "Must match one of the available templates listed in the tool description. "
                    f"Default: '{DEFAULT_TEMPLATE}'."
                ),
            },
        }

    def get_required_params(self) -> List[str]:
        return ["topic"]

    async def get_dynamic_description(self) -> str:
        """Return a description that includes the current template list from the DB.

        Called by the agent service at request time (async) to build the
        tool description that the LLM actually sees.
        """
        templates = await _get_templates_with_descriptions()
        if not templates:
            # Fallback to default ones
            templates = [(slug, "") for slug in AVAILABLE_TEMPLATES]

        lines = [self.description, "Available templates:"]
        for slug, desc in templates:
            label = f"  - {slug}"
            if desc:
                label += f": {desc}"
            lines.append(label)

        return "\n".join(lines)

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

        # Fetch the actual available templates from the DB at runtime.
        db_templates = await _get_available_templates_from_db()

        # Validate the requested template against the DB list — the
        # fallback is the configured default (Brain ▸ Tools), itself
        # falling back to the service's DEFAULT_TEMPLATE.
        default_tpl = _configured_default_template()
        tpl = (
            template
            if template in db_templates
            else (default_tpl if default_tpl in db_templates else DEFAULT_TEMPLATE)
        )

        try:
            # Model override (Brain ▸ Tools) when set, else the service
            # falls back to the Report task slot (Settings ▸ AI ▸ Models).
            from app.agent.tools import config_store

            model_override = await config_store.tool_model_override(self.name)
            result = await generate_presentation(
                topic=topic.strip(),
                outline=outline,
                template=tpl,
                model=model_override,
                max_slides=_configured_max_slides(),
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
                output=(
                    f"Presentation generated: {result['filename']} "
                    f"({result.get('slide_count', 0)} slides, template: "
                    f"{result.get('template', tpl)}). "
                    "Tell the user it's ready for download."
                ),
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


# ── Configuration definition (Brain ▸ Tools ▸ Presentation Generation) ──


def _configured_max_slides() -> int:
    """The persisted slide cap (custom.max_slides)."""
    from app.agent.tools import config_store

    cfg = config_store.get_tool_config("use_pptx_gen") or {}
    try:
        value = int((cfg.get("custom") or {}).get("max_slides", MAX_SLIDES))
    except (TypeError, ValueError):
        return MAX_SLIDES
    return max(3, min(100, value))


def _configured_default_template() -> str:
    """The persisted default template slug (custom.default_template)."""
    from app.agent.tools import config_store

    cfg = config_store.get_tool_config("use_pptx_gen") or {}
    value = (cfg.get("custom") or {}).get("default_template")
    return value if isinstance(value, str) and value.strip() else DEFAULT_TEMPLATE


def _validate_pptx_custom(custom: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the custom settings; returns the cleaned object."""
    if not isinstance(custom, dict):
        raise ValueError("'custom' must be an object")
    if "max_slides" in custom:
        try:
            v = float(custom["max_slides"])
        except (TypeError, ValueError):
            raise ValueError("'max_slides' must be a number")
        if v < 3 or v > 100:
            raise ValueError("'max_slides' must be between 3 and 100")
        custom["max_slides"] = int(v)
    if "default_template" in custom and custom["default_template"] is not None:
        tpl = custom["default_template"]
        if not isinstance(tpl, str) or not tpl.strip():
            raise ValueError("'default_template' must be a template slug")
    return custom


PPTX_GEN_CONFIG = ToolConfigDefinition(
    tool_name="use_pptx_gen",
    display_name="Presentation Generation",
    description=(
        "Generate a PowerPoint presentation (.pptx) about a topic. "
        "Use when the user asks for a presentation, slides, a slideshow, or a deck. "
        "The presentation is generated from scratch by the AI with speaker notes "
        "and saved as a downloadable file."
    ),
    model_task_slot="report",
    custom_defaults={
        "max_slides": MAX_SLIDES,
        "default_template": DEFAULT_TEMPLATE,
    },
    custom_schema=[
        {
            "key": "deck",
            "label": "Deck",
            "fields": [
                ConfigField(
                    "max_slides",
                    "Max slides",
                    "int",
                    default=MAX_SLIDES,
                    help="Hard cap on the deck length — longer decks are trimmed",
                ).to_dict(),
                ConfigField(
                    "default_template",
                    "Default template",
                    "select",
                    options=[
                        {"value": slug, "label": slug.capitalize()}
                        for slug in AVAILABLE_TEMPLATES
                    ],
                    default=DEFAULT_TEMPLATE,
                    help=(
                        "Used when the request doesn't pick a template — "
                        "custom DB templates can still be requested by name"
                    ),
                ).to_dict(),
            ],
        },
    ],
    validate_custom=_validate_pptx_custom,
)

register_config(PPTX_GEN_CONFIG)


tool_registry.register(PptxGenTool())
