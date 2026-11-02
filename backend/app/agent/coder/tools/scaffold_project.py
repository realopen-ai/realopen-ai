"""scaffold project tool: schema and execution."""

from app.agent.coder.starters import scaffold
from app.agent.coder.starters import BACKENDS
from app.agent.coder.tools import setup_python_project
from app.agent.coder.tools.schema import _function
from app.agent.coder.tools.schema import FRONTEND_PROPERTY
from app.agent.coder.tools.schema import STYLING_PROPERTY

SCHEMA = _function(
    "scaffold_project",
    "Create a minimal FastAPI and/or React+Vite+TypeScript starter with local Tailwind CSS and an /api proxy. No CRUD, database layer, auth, ownership or application-specific tests are generated. Implement the requested app yourself. Vanilla TypeScript is optional. Never overwrites existing files.",
    {
        "name": {"type": "string"},
        "backend": {"type": "string", "enum": ["none", *BACKENDS]},
        "frontend": FRONTEND_PROPERTY,
        "styling": STYLING_PROPERTY,
    },
    ("name", "backend", "frontend"),
)


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    return await scaffold(sandbox, args, setup_python_project.execute, progress)
