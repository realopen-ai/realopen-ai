"""
Base tool interface and tool registry for the AI agent.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


class ToolType(str, Enum):
    WEB_SEARCH = "websearch"
    VISION = "vision"
    CODE_EXEC = "code_exec"
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"


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
    results: Optional[List[Dict]] = None  # web search results
    language: Optional[str] = None  # code exec
    code: Optional[str] = None  # code exec
    output: Optional[str] = None  # code exec / file ops
    exit_code: Optional[int] = None  # code exec
    file_path: Optional[str] = None  # file ops
    file_content: Optional[str] = None  # file ops
    image_description: Optional[str] = None  # vision
    error: Optional[str] = None  # any tool


@dataclass
class ToolResult:
    """The result of executing a tool."""

    success: bool
    output: str  # Text to feed back to the LLM
    tool_call: Optional[ToolCall] = None  # Updated tool call for SSE events


class BaseTool(ABC):
    """Abstract base class for all agent tools."""

    name: str = ""
    description: str = ""
    tool_type: ToolType = ToolType.WEB_SEARCH

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


class ToolRegistry:
    """Registry that holds all available tools."""

    def __init__(self):
        self._tools: Dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[BaseTool]:
        return self._tools.get(name)

    def all_tools(self) -> List[BaseTool]:
        return list(self._tools.values())

    def get_schemas(self) -> List[dict]:
        return [tool.get_schema() for tool in self._tools.values()]


# Global registry
tool_registry = ToolRegistry()


def get_tool_registry() -> ToolRegistry:
    return tool_registry
