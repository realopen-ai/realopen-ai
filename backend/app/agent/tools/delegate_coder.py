"""General-agent tool adapter for the dedicated workspace coder."""

from app.agent.base import tool_registry
from app.agent.coder import CoderAgent

tool_registry.register(CoderAgent())
