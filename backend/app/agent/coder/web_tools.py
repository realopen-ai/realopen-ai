"""Request-local access to shared web tools; no duplicate fetch/search engine."""

import json
import re
import uuid

from app.agent.base import (
    get_tool_registry,
    ToolRegistry,
    ToolResult,
    ToolArgumentError,
)
from app.agent.coder.context import tokens, prefix
from app.agent.results import compact_result
from app.config import settings

WEB_NAMES = ("use_websearch", "use_webfetch")
WEB_TYPES = {
    "use_websearch": "websearch",
    "use_webfetch": "webfetch",
    "discover_tools": "tool_access",
    "read_tool_result": "tool_output",
}


def _config(name):
    # General tool discovery imports the coder; resolve preferences at execution time.
    from app.agent.tools import config_store

    return config_store.get_tool_config(name) or {}


def _schema(tool):
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": {
                "type": "object",
                "properties": tool.get_parameters(),
                "required": tool.get_required_params(),
            },
        },
    }


class CoderWebTools:
    def __init__(self, request):
        # Routing imports general tool discovery, which itself imports CoderAgent.
        from app.agent import routing

        registry = get_tool_registry()
        self.tools = {
            name: registry.get(name)
            for name in WEB_TYPES
            if registry.get(name) and _config(name).get("enabled", True)
        }
        configs = {name: _config(name) for name in WEB_NAMES}
        selectable = ToolRegistry()
        for name in WEB_NAMES:
            if name in self.tools:
                selectable.register(self.tools[name])
        self.active = routing.select_tools(request, selectable, configs, limit=2)
        if re.search(r"https?://\S+", request) and "use_webfetch" in self.tools:
            # A supplied URL is an explicit source request, not a keyword guess.
            self.active.add("use_webfetch")
        if self.available and "discover_tools" in self.tools:
            self.active.add("discover_tools")
        self.results = {}
        self.metadata = {}

    @property
    def available(self):
        return [
            name
            for name in WEB_NAMES
            if name in self.tools and _config(name).get("enabled", True)
        ]

    @property
    def schemas(self):
        schemas = []
        for name in WEB_TYPES:
            if name not in self.active or not _config(name).get("enabled", True):
                continue
            if name == "discover_tools" and not self.available:
                continue
            schema = _schema(self.tools[name])
            if name == "discover_tools":
                schema["function"]["parameters"]["properties"]["names"]["items"][
                    "enum"
                ] = self.available
            schemas.append(schema)
        return schemas

    def catalog(self):
        return json.dumps(
            {
                "optional_web_tools": self.available,
                "schema_loader": (
                    "discover_tools" if "discover_tools" in self.active else None
                ),
            },
            separators=(",", ":"),
        )

    async def _load(self, names):
        if any(name not in self.available for name in names):
            raise ToolArgumentError(
                "Only enabled web tools from this request's catalog can be loaded."
            )
        self.active.update(names)
        return list(dict.fromkeys(names))

    def _view(self, name, result, ref, budget):
        paging = "read_tool_result" in self.tools and _config("read_tool_result").get(
            "enabled", True
        )
        view = compact_result(
            name,
            result.output,
            result.tool_call,
            success=result.success,
            result_ref=ref,
            limit=min(
                settings.AGENT_TOOL_RESULT_MAX_CHARS,
                result.model_output_limit or settings.AGENT_TOOL_RESULT_MAX_CHARS,
            ),
        )
        if not view.shortened and tokens(view.content) <= budget:
            return view.content
        if paging:
            self.active.add("read_tool_result")
        content = view.content
        if tokens(content) > budget or not paging:
            data = {
                "source_tool": name,
                "shortened": True,
                "message": (
                    "Use read_tool_result with this reference and character offsets for omitted content."
                    if paging
                    else "Paging is disabled. This is only an excerpt; full output remains in tool history."
                ),
            }
            if paging:
                data["result_ref"] = ref
            data["excerpt"] = prefix(result.output, max(0, budget - tokens(data) - 40))
            content = json.dumps(data, ensure_ascii=False)
            while tokens(content) > budget and data["excerpt"]:
                data["excerpt"] = data["excerpt"][: len(data["excerpt"]) // 2]
                content = json.dumps(data, ensure_ascii=False)
        return content

    async def execute(self, name, args, budget):
        tool = self.tools.get(name)
        if not tool or not _config(name).get("enabled", True):
            result = ToolResult(
                False,
                "This tool is disabled in Brain → Tools. Continue using local workspace evidence; do not retry it.",
            )
        elif name in WEB_NAMES and name not in self.active:
            result = ToolResult(
                False,
                "This web schema is not loaded. Use discover_tools first; no web request was executed.",
            )
        elif name == "discover_tools":
            result = await tool.execute(
                **args, _tool_loader=self._load, _loaded_tools=set(self.active)
            )
        elif name == "read_tool_result":
            page_args = dict(args)
            while True:
                result = await tool.execute(
                    **page_args,
                    _result_store=self.results,
                    _result_metadata=self.metadata,
                )
                if (
                    not result.success
                    or tokens(result.output) <= budget
                    or page_args.get("limit", 3000) <= 1
                ):
                    break
                page_args["limit"] = max(1, page_args.get("limit", 3000) // 2)
        else:
            result = await tool.execute(**args)
        raw = result.output
        ref = f"coder-web-{uuid.uuid4().hex}"
        if name in WEB_NAMES and result.success:
            self.results[ref] = raw
            self.metadata[ref] = {
                "source_tool": name,
                "source_title": tool.get_display_name() if tool else name,
            }
            while len(self.results) > 6:
                oldest = next(iter(self.results))
                self.results.pop(oldest)
                self.metadata.pop(oldest, None)
            if (
                result.success
                and "read_tool_result" in self.tools
                and _config("read_tool_result").get("enabled", True)
            ):
                self.active.add("read_tool_result")
        view = (
            self._view(name, result, ref, budget)
            if name in WEB_NAMES and result.success
            else raw
        )
        call = result.tool_call
        event = {
            "exit_code": 0 if result.success else 1,
            "model_output": view,
            "web_results": getattr(call, "web_results", None),
            "web_tool": True,
        }
        if name in WEB_NAMES and result.success:
            event["web_source"] = {
                **({"result_ref": ref} if "read_tool_result" in self.active else {}),
                "tool": name,
                **{
                    key: str(args[key])
                    for key in ("url", "query")
                    if key in args and len(str(args[key])) <= 400
                },
            }
        return raw, event
