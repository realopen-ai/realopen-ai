"""start preview tool: schema and execution."""

import json
from app.services import sandbox_host
from app.services.sandbox_manager import remember_preview_command
from app.agent.coder.tools.common import FRONTEND_PORT
from app.agent.coder.tools.common import BACKEND_PORT
from app.agent.coder.tools.schema import _function

SCHEMA = _function(
    "start_preview",
    "Start or replace a supervised web-app preview. The command is persisted and automatically restored when this sandbox restarts.",
    {
        "command": {"type": "string"},
        "port": {"type": "integer", "enum": [FRONTEND_PORT, BACKEND_PORT]},
    },
    ("command", "port"),
)


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    port = int(args["port"])
    result = await sandbox_host.call(
        "exec/detached",
        sandbox,
        {
            "command": args["command"],
            "command_id": f"preview-{port}",
            "timeout": 30,
        },
    )
    await remember_preview_command(sandbox, port, args["command"])
    result["persistent"] = True
    return json.dumps(result), {
        "command": args["command"],
        "preview_url": f"/api/sandboxes/{sandbox.id}/preview/{port}/",
        "port": port,
    }


async def infer_preview(sandbox) -> tuple[str, int] | None:
    # Prefer a frontend when a full-stack workspace exposes both services.
    try:
        package = json.loads(
            (
                await sandbox_host.call(
                    "files/read", sandbox, {"path": "/workspace/package.json"}
                )
            ).get("content", "{}")
        )
        if package.get("scripts", {}).get("dev"):
            return (
                f"bun run dev --host 0.0.0.0 --port {FRONTEND_PORT}",
                FRONTEND_PORT,
            )
    except Exception:
        pass
    for path, module in (
        ("/workspace/backend/app.py", "backend.app:app"),
        ("/workspace/app/main.py", "app.main:app"),
        ("/workspace/main.py", "main:app"),
    ):
        try:
            result = await sandbox_host.call("files/read", sandbox, {"path": path})
        except Exception:
            continue
        if "FastAPI(" in result.get("content", ""):
            return (
                f"uv run uvicorn {module} --host 0.0.0.0 --port {BACKEND_PORT}",
                BACKEND_PORT,
            )
    return None
