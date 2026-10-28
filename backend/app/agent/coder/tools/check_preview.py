"""check preview tool: schema and execution."""

import json
import shlex
from app.services import sandbox_host
from app.agent.coder.handoff import openapi_contract
from app.agent.coder.tools.common import FRONTEND_PORT
from app.agent.coder.tools.common import BACKEND_PORT
from app.agent.coder.tools.schema import _function

SCHEMA = _function(
    "check_preview",
    "Smoke-test a running preview. Read /openapi.json on backend port 6969 for the current API contract, optionally filtering by api_path. For full-stack integration, also request an /api path through frontend port 6767 after starting both services.",
    {
        "port": {"type": "integer", "enum": [FRONTEND_PORT, BACKEND_PORT]},
        "path": {"type": "string"},
        "api_path": {
            "type": "string",
            "description": "Optional API path prefix to select relevant OpenAPI operations, e.g. /api/posts. Only for /openapi.json.",
        },
    },
    ("port", "path"),
)


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    path, port = args["path"], args["port"]
    api_path = args.get("api_path")
    if api_path is not None and (
        path != "/openapi.json"
        or not isinstance(api_path, str)
        or not api_path.startswith("/")
    ):
        raise ValueError(
            "api_path selects an absolute API path prefix when reading /openapi.json"
        )
    if (
        not isinstance(path, str)
        or not path.startswith("/")
        or path.startswith("//")
        or port not in {FRONTEND_PORT, BACKEND_PORT}
    ):
        raise ValueError("Use a relative preview path and a published sandbox port")
    command = (
        "curl --fail-with-body --silent --show-error --max-time 15 --retry 5 --retry-connrefused --retry-delay 1 --retry-max-time 10 "
        + shlex.quote(f"http://127.0.0.1:{port}{path}")
    )
    result = await sandbox_host.call(
        "exec", sandbox, {"command": command, "timeout": 20}, timeout=40
    )
    event = {
        "command": command,
        "exit_code": result.get("exit_code"),
        "port": port,
    }
    if path == "/openapi.json" and result.get("exit_code") == 0:
        try:
            contract = openapi_contract(
                json.loads(result.get("stdout", "")), args.get("api_path")
            )
        except (ValueError, TypeError) as error:
            return json.dumps({**result, "exit_code": 1, "error": str(error)}), {
                **event,
                "exit_code": 1,
            }
        return json.dumps({**result, "api_contract": contract}), {
            **event,
            "api_contract": contract,
        }
    return json.dumps(result), event
