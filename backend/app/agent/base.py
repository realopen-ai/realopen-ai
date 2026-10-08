"""
Base tool interface and tool registry for the AI agent.

Each tool supports:
- A text description for the system prompt
- JSON Schema parameters for native Ollama/OpenAI function calling
- Multi-format tool call parsing (fenced blocks, JSON, XML, native)
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ToolType(str, Enum):
    WEB_SEARCH = "websearch"
    WEB_FETCH = "webfetch"
    VISION = "vision"
    CODE_EXEC = "code_exec"
    SKILL = "skill"
    FLASHCARDS = "flashcards"
    NOTES = "notes"
    ARTIFACT = "artifact"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    PREVIEW = "preview"
    IMAGE_GEN = "image_gen"
    SANDBOX = "sandbox"


@dataclass
class ToolCall:
    """Represents a single tool call from the agent."""

    id: str
    type: ToolType
    name: str
    status: str = "running"  # running | completed | error
    title: str = ""
    started_at: float = 0.0
    completed_at: Optional[float] = None

    # Tool-specific fields
    query: Optional[str] = None  # web search
    web_results: Optional[List[Dict]] = None  # web search results
    gen_results: Optional[List[Dict]] = None  # media generation results
    language: Optional[str] = None  # code exec
    code: Optional[str] = None  # code exec
    output: Optional[str] = None  # code exec / file ops
    exit_code: Optional[int] = None  # code exec
    file_path: Optional[str] = None  # file ops
    file_content: Optional[str] = None  # file ops
    image_description: Optional[str] = None  # vision
    error: Optional[str] = None  # any tool
    progress: Optional[Dict] = None  # live artifact summary stage and batch counts


@dataclass
class ToolResult:
    """The result of executing a tool."""

    success: bool
    output: str  # Text to feed back to the LLM
    tool_call: Optional[ToolCall] = None  # Updated tool call for SSE events


class BaseTool(ABC):
    """Abstract base class for all agent tools.

    Override get_parameters() and get_required_params() to expose
    JSON Schema for native Ollama/OpenAI function calling. The base
    returns an empty schema (tool accepts any kwargs).
    """

    name: str = ""
    description: str = ""
    display_name: str = ""
    tool_type: ToolType = ToolType.WEB_SEARCH

    # Parameter aliases — maps common names to the tool's expected kwarg names.
    # e.g. {"query": "query", "search": "query"} means both "query" and
    # "search" in tool args map to the `query` kwarg.
    param_aliases: Dict[str, str] = {}

    @abstractmethod
    async def execute(self, **kwargs) -> ToolResult:
        """Execute the tool with the given arguments."""
        ...

    def get_schema(self) -> dict:
        """Return a JSON-schema-style description for the LLM."""
        return {
            "name": self.name,
            "description": self.description,
            "type": self.tool_type.value,
        }

    def get_display_name(self) -> str:
        """Friendly UI name: explicit display_name or prettified name."""
        if self.display_name:
            return self.display_name
        base = self.name
        if base.startswith("use_"):
            base = base[4:]
        return base.replace("_", " ").strip().title()

    def get_parameters(self) -> dict:
        """Return JSON Schema properties for native function calling.

        Override in subclasses to declare specific parameters.
        The default returns a simple 'input' parameter.
        """
        return {
            "input": {
                "type": "string",
                "description": self.description[:200],
            },
        }

    def get_required_params(self) -> List[str]:
        """Return list of required parameter names."""
        return ["input"]

    def _apply_aliases(self, kwargs: dict) -> dict:
        """Apply parameter aliases to normalize argument names."""
        if not self.param_aliases:
            return kwargs
        result = {}
        used_source = set()
        for target, sources in self._invert_aliases().items():
            for src in sources:
                if src in kwargs and src not in used_source:
                    result[target] = kwargs[src]
                    used_source.add(src)
                    break
        # Pass through any unknown keys
        for k, v in kwargs.items():
            if k not in result:
                result[k] = v
        return result

    def _invert_aliases(self) -> Dict[str, List[str]]:
        """Invert param_aliases to {target_param: [alias1, alias2, ...]}."""
        inverted: Dict[str, List[str]] = {}
        for alias, target in self.param_aliases.items():
            inverted.setdefault(target, []).append(alias)
            inverted[target].append(target)  # canonical name is also valid
        return inverted


class ToolRegistry:
    """Registry that holds all available tools.

    Supports dynamic registration and unregistration so that module
    toggles take effect immediately without restarting the backend.
    """

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}
        # Backup of unregistered tools so they can be re-registered
        # when a module is toggled back on.
        self._backup: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool
        # Also keep in backup so we can restore after unregister
        self._backup[tool.name] = tool

    def unregister(self, name: str) -> bool:
        """Remove a tool from the active registry.

        Returns True if the tool was found and removed, False otherwise.
        The tool is kept in _backup so it can be restored later.
        """
        if name in self._tools:
            del self._tools[name]
            return True
        return False

    def restore(self, name: str) -> bool:
        """Restore a previously unregistered tool from backup.

        Returns True if the tool was found in backup and restored.
        """
        if name in self._backup and name not in self._tools:
            self._tools[name] = self._backup[name]
            return True
        return False

    def unregister_many(self, names: List[str]) -> int:
        """Unregister multiple tools. Returns count of tools actually removed."""
        count = 0
        for name in names:
            if self.unregister(name):
                count += 1
        return count

    def restore_many(self, names: List[str]) -> int:
        """Restore multiple tools from backup. Returns count restored."""
        count = 0
        for name in names:
            if self.restore(name):
                count += 1
        return count

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def all_tools(self) -> List[BaseTool]:
        return list(self._tools.values())

    def get_schemas(self) -> List[dict]:
        return [tool.get_schema() for tool in self._tools.values()]

    def has_tool(self, name: str) -> bool:
        return name in self._tools


# Global registry
tool_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    return tool_registry
