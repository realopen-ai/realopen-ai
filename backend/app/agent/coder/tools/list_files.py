"""list files tool: schema and execution."""

import json
from app.services import sandbox_host
from app.agent.coder.tools.schema import _function

SCHEMA = _function(
    "list_files", "List the workspace tree.", {"path": {"type": "string"}}
)


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    result = await sandbox_host.call(
        "files", sandbox, {"path": args.get("path", "/workspace")}
    )
    return json.dumps(result)[:40_000], None
