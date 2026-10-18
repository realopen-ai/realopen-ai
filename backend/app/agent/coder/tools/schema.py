"""Shared schema helpers and registered scaffold options."""

from app.agent.coder.starters import FRONTENDS
from app.agent.coder.styling import DEFAULT_STYLING, STYLING


def _function(name: str, description: str, properties: dict, required=()) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                **({"required": list(required)} if required else {}),
            },
        },
    }


COMMAND_PROPERTIES = {
    "command": {"type": "string"},
    "timeout": {"type": "integer"},
}
STYLING_PROPERTY = {
    "type": "string",
    "enum": list(STYLING),
    "default": DEFAULT_STYLING,
    "description": "Local Tailwind CSS styling for new scaffolds. Preserve existing projects' styling frameworks.",
}
FRONTEND_PROPERTY = {
    "type": "string",
    "enum": [*FRONTENDS, "none"],
    "default": "react-vite",
    "description": "react-vite (default React + TypeScript), vite (vanilla TypeScript only when requested), or none.",
}
