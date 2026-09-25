"""On-demand skill instruction loader."""

import time

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.services import skills


class LoadSkillTool(BaseTool):
    name = "load_skill"
    display_name = "Load skill"
    description = "Load the full instructions for one relevant local skill."
    tool_type = ToolType.SKILL

    def get_parameters(self) -> dict:
        return {
            "name": {"type": "string", "description": "Skill id to load"},
            "resource": {
                "type": "string",
                "description": "Optional relative text resource path listed by the skill",
            },
        }

    def get_required_params(self) -> list[str]:
        return ["name"]

    async def execute(
        self,
        *,
        name: str,
        resource: str | None = None,
        _skill_role: str = "general",
        **_kwargs,
    ):
        started = time.time()
        call = ToolCall(
            id=f"tc-skill-{int(started * 1000)}",
            type=self.tool_type,
            name=self.name,
            title=f"Loading skill {name}",
            started_at=started,
        )
        try:
            output = skills.load_for_agent(name, _skill_role, resource)
            call.status = "completed"
            call.completed_at = time.time()
            call.output = output
            return ToolResult(True, output, call)
        except (FileNotFoundError, ValueError) as exc:
            call.status = "error"
            call.completed_at = time.time()
            call.error = str(exc)
            return ToolResult(False, f"Could not load skill: {exc}", call)


tool_registry.register(LoadSkillTool())
