"""Select scaffold capability from the configured coder runtime context."""

from app.config import settings
from app.services import coder_settings

ADVANCED_SCAFFOLD_CONTEXT = 100_000


def scaffold_tools(model, tools):
    window = settings.AGENT_CONTEXT_WINDOWS.get(
        model, coder_settings.effective().context_window
    )
    return [
        tool
        for tool in tools
        if tool["function"]["name"] != "scaffold_crud_app"
        or window >= ADVANCED_SCAFFOLD_CONTEXT
    ]
