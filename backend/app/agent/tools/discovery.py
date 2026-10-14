"""Request-local tool exposure and output paging; never execute target tools."""

import json

from app.agent.base import (
    BaseTool,
    ToolResult,
    ToolType,
    tool_registry,
    ToolArgumentError,
)


class DiscoverToolsTool(BaseTool):
    name = "discover_tools"
    display_name = "Discover tools"
    description = "Load schemas for up to three enabled tools from the available-tools catalog. This does not execute them."
    tool_type = ToolType.TOOL_ACCESS

    def get_parameters(self):
        return {
            "names": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "maxItems": 3,
            }
        }

    def get_required_params(self):
        return ["names"]

    async def execute(self, *, names, _tool_loader=None, _loaded_tools=(), **kwargs):
        if _tool_loader is None:
            return ToolResult(False, "Tool discovery requires an active agent request.")
        try:
            loaded = await _tool_loader(names)
        except ToolArgumentError as exc:
            return self.invalid_arguments(str(exc))
        return ToolResult(
            True,
            json.dumps(
                {
                    "operation": "load_schema",
                    "requested": loaded,
                    "loaded": [name for name in loaded if name not in _loaded_tools],
                    "already_loaded": [
                        name for name in loaded if name in _loaded_tools
                    ],
                    "action_executed": False,
                    "message": "Schemas are now available. Call a loaded tool only if needed for the original request.",
                }
            ),
        )


class ReadToolResultTool(BaseTool):
    name = "read_tool_result"
    display_name = "Read tool result"
    description = "Read exact text from a shortened result in this request. Offsets and limits count characters, not pages or lines. Omit offset/limit to read the first 3000 characters; continue using returned next_offset."
    tool_type = ToolType.TOOL_OUTPUT

    def get_parameters(self):
        return {
            "result_ref": {"type": "string"},
            "offset": {
                "type": "integer",
                "minimum": 0,
                "default": 0,
                "description": "Zero-based character offset. Start at 0 or use the previous next_offset exactly.",
            },
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 6000,
                "default": 3000,
                "description": "Number of characters to read (default 3000). Small values retrieve only tiny fragments, not whole sections.",
            },
        }

    def get_required_params(self):
        return ["result_ref"]

    async def execute(
        self,
        *,
        result_ref,
        offset=0,
        limit=3000,
        _result_store=None,
        _result_metadata=None,
        **kwargs
    ):
        if _result_store is None or result_ref not in _result_store:
            return self.invalid_arguments(
                "Unknown result_ref. Use an exact reference returned in this request."
            )
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(limit, int)
            or isinstance(limit, bool)
            or not 1 <= limit <= 6000
        ):
            return self.invalid_arguments(
                "Use a nonnegative offset and a limit between 1 and 6000."
            )
        text = _result_store[result_ref]
        if offset > len(text):
            return self.invalid_arguments("Offset exceeds result length.")
        end = min(len(text), offset + limit)
        source = (_result_metadata or {}).get(result_ref, {})
        return ToolResult(
            True,
            json.dumps(
                {
                    "result_ref": result_ref,
                    "content": text[offset:end],
                    "offset": offset,
                    "next_offset": end if end < len(text) else None,
                    "total_chars": len(text),
                    **{
                        key: source[key]
                        for key in ("source_tool", "source_title")
                        if isinstance(source.get(key), str)
                    },
                },
                ensure_ascii=False,
            ),
        )


tool_registry.register(DiscoverToolsTool())
tool_registry.register(ReadToolResultTool())
