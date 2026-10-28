"""search files tool: schema and execution."""

import json
import shlex
from app.services import sandbox_host
from app.agent.coder.tools.schema import _function

SCHEMA = _function(
    "search_files",
    "Search workspace text with ripgrep.",
    {"query": {"type": "string"}, "path": {"type": "string"}},
    ("query",),
)


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    query = shlex.quote(str(args["query"]))
    path = shlex.quote(str(args.get("path", "/workspace")))
    result = await sandbox_host.call(
        "exec",
        sandbox,
        {
            "command": (
                f"rg -n --hidden --glob '!node_modules' --glob '!.git' -- {query} {path}"
            ),
            "timeout": 30,
        },
        timeout=50,
    )
    return json.dumps(result)[:80_000], None
