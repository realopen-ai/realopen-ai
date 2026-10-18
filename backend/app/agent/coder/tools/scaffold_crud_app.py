"""scaffold crud app tool: schema and execution."""

from app.agent.coder.starters import BACKENDS
from app.agent.coder.scaffolding import scaffold_crud
from app.agent.coder.scaffolding.composition import FEATURES as SCAFFOLD_FEATURES
from app.agent.coder.tools import setup_python_project
from app.agent.coder.tools.schema import _function
from app.agent.coder.tools.schema import FRONTEND_PROPERTY
from app.agent.coder.tools.schema import STYLING_PROPERTY

SCHEMA = _function(
    "scaffold_crud_app",
    "Create a new working CRUD app from entities/fields: FastAPI+SQLite and/or React+Vite+TypeScript with local styling and behavior tests. Vanilla TypeScript is optional. For independent resources only; preserve explicit requirements and extend the generated source. Never overwrites existing work. Frontend-only uses browser storage. Auth/relationships/WebSockets are not generated.",
    {
        "name": {"type": "string", "minLength": 1, "maxLength": 100},
        "backend": {"type": "string", "enum": ["none", *BACKENDS]},
        "frontend": FRONTEND_PROPERTY,
        "styling": STYLING_PROPERTY,
        "features": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "description": "Optional; defaults to CRUD. Only registered features are supported.",
            "items": {"type": "string", "enum": list(SCAFFOLD_FEATURES)},
        },
        "entities": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "fields"],
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Plural snake_case resource, e.g. products; id is automatic.",
                    },
                    "label": {"type": "string"},
                    "fields": {
                        "type": "array",
                        "minItems": 1,
                        "maxItems": 16,
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["name", "type"],
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "snake_case field, not id.",
                                },
                                "type": {
                                    "type": "string",
                                    "enum": [
                                        "string",
                                        "integer",
                                        "number",
                                        "boolean",
                                    ],
                                },
                                "label": {"type": "string"},
                                "required": {
                                    "type": "boolean",
                                    "description": "Default true; false permits null.",
                                },
                                "default": {
                                    "type": ["string", "number", "boolean", "null"]
                                },
                            },
                        },
                    },
                },
            },
        },
    },
    ("name", "backend", "frontend", "entities"),
)


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    return await scaffold_crud(sandbox, args, setup_python_project.execute, progress)
