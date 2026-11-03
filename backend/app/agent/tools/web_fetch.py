"""Fetch readable Markdown, retaining full output for result storage and paging."""

import time
import uuid
from typing import Any, Dict, List
from urllib.parse import urlsplit

import httpx

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.services import web_fetch
from app.agent.tools.config_base import (
    ConfigField,
    ToolConfigDefinition,
    register_config,
)

# Fallbacks when the persisted configuration predates the settings —
# the actual values live in the tool config (Brain ▸ Tools).
DEFAULT_TIMEOUT_S = 20
DEFAULT_MAX_CHARS = 6000


class WebFetchTool(BaseTool):
    name = "use_webfetch"
    provides_evidence = True
    display_name = "Web Fetch"
    description = (
        "Fetch and extract readable text from a URL. "
        "Use after web search for full context, or when the user provides "
        "a URL and asks about its content."
    )
    tool_type = ToolType.WEB_FETCH

    param_aliases = {
        "url": "url",
        "link": "url",
        "page": "url",
    }

    def get_parameters(self) -> dict:
        return {
            "url": {
                "type": "string",
                "description": "The URL to fetch and extract text from",
            },
        }

    def get_required_params(self) -> List[str]:
        return ["url"]

    async def execute(self, *, url: str, **kwargs) -> ToolResult:
        try:
            urlsplit(
                url
            )  # Reject malformed bracketed hosts before httpx normalizes them.
            parsed = httpx.URL(url)
        except (httpx.InvalidURL, TypeError, ValueError):
            return self.invalid_arguments("Provide a valid absolute HTTP or HTTPS URL.")
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            return self.invalid_arguments("Provide a valid absolute HTTP or HTTPS URL.")
        timeout_s, max_chars = _configured_limits()
        call = ToolCall(
            id=f"tc-{uuid.uuid4().hex}",
            type=self.tool_type,
            name=self.name,
            title="Fetching page",
            query=url,
            started_at=time.time(),
        )
        try:
            page = await web_fetch.fetch(url, timeout_s)
            call.query = page.url
            call.status = "completed"
            call.completed_at = time.time()
            call.title = f"Read page: {page.title}"
            call.output = page.render()
            return ToolResult(True, call.output, call, model_output_limit=max_chars)
        except (httpx.HTTPError, ValueError, TimeoutError) as exc:
            return ToolResult(
                False, f"Web Fetch failed: {str(exc) or type(exc).__name__}", call
            )


# ── Configuration definition (Brain ▸ Tools ▸ Web Fetch) ──────────


def _configured_limits() -> tuple:
    """The persisted fetch settings (custom.timeout_s / custom.max_chars).

    Read at every execution — UI changes apply immediately.
    """
    from app.agent.tools import config_store

    cfg = config_store.get_tool_config("use_webfetch") or {}
    custom = cfg.get("custom") or {}
    try:
        timeout_s = int(custom.get("timeout_s", DEFAULT_TIMEOUT_S))
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    try:
        max_chars = int(custom.get("max_chars", DEFAULT_MAX_CHARS))
    except (TypeError, ValueError):
        max_chars = DEFAULT_MAX_CHARS
    return max(1, min(120, timeout_s)), max(200, min(200_000, max_chars))


def _validate_webfetch_custom(custom: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the custom settings; returns the cleaned object."""
    if not isinstance(custom, dict):
        raise ValueError("'custom' must be an object")
    if "timeout_s" in custom:
        try:
            v = float(custom["timeout_s"])
        except (TypeError, ValueError):
            raise ValueError("'timeout_s' must be a number")
        if v <= 0 or v > 120:
            raise ValueError("'timeout_s' must be between 1 and 120")
        custom["timeout_s"] = int(v)
    if "max_chars" in custom:
        try:
            v = float(custom["max_chars"])
        except (TypeError, ValueError):
            raise ValueError("'max_chars' must be a number")
        if v < 200 or v > 200_000:
            raise ValueError("'max_chars' must be between 200 and 200000")
        custom["max_chars"] = int(v)
    return custom


WEB_FETCH_CONFIG = ToolConfigDefinition(
    tool_name="use_webfetch",
    display_name="Web Fetch",
    description=(
        "Fetch and extract readable text from a URL. "
        "Use after web search for full context, or when the user provides "
        "a URL and asks about its content."
    ),
    custom_defaults={
        "timeout_s": DEFAULT_TIMEOUT_S,
        "max_chars": DEFAULT_MAX_CHARS,
    },
    custom_schema=[
        {
            "key": "fetching",
            "label": "Fetching",
            "fields": [
                ConfigField(
                    "timeout_s",
                    "Timeout (s)",
                    "int",
                    default=DEFAULT_TIMEOUT_S,
                    help="Maximum seconds to wait for the page to respond",
                ).to_dict(),
                ConfigField(
                    "max_chars",
                    "Initial model view (characters)",
                    "int",
                    default=DEFAULT_MAX_CHARS,
                    help=(
                        "Limit the initial model view; the full extracted page remains "
                        "available through read_tool_result when paging is enabled. "
                        "The agent's global tool-result limit also applies."
                    ),
                ).to_dict(),
            ],
        },
    ],
    validate_custom=_validate_webfetch_custom,
)

register_config(WEB_FETCH_CONFIG)


# Register the tool
tool_registry.register(WebFetchTool())
