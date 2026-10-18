"""install dependencies tool: schema and execution."""

from app.agent.coder.tools import commands
from app.agent.coder.tools.schema import _function
from app.agent.coder.tools.schema import COMMAND_PROPERTIES

SCHEMA = _function(
    "install_dependencies",
    "Install dependencies with the repository package manager.",
    COMMAND_PROPERTIES,
    ("command",),
)


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    return await commands.execute(sandbox, args, refresh_project=True)
