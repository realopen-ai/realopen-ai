"""run browser tests tool: schema and execution."""

import json
import shlex
from app.agent.coder.tools import commands
from app.agent.coder.tools.workspace import workspace_path
from app.agent.coder.tools.common import FRONTEND_PORT
from app.agent.coder.tools.common import BACKEND_PORT
from app.agent.coder.tools.schema import _function

SCHEMA = _function(
    "run_browser_tests",
    "Optional real-browser verification, only when requested or appropriate for existing browser tests. Requires the opt-in browser sandbox image; never installs anything. Runs .mjs Playwright scripts sequentially, closing Chromium afterward. Not required for default DOM/API verification.",
    {
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "minItems": 1,
            "maxItems": 10,
        },
        "port": {"type": "integer", "enum": [FRONTEND_PORT, BACKEND_PORT]},
    },
    ("paths",),
)


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    paths = [workspace_path(path) for path in args["paths"]]
    command = (
        "roai-browser-test --port "
        + str(args.get("port", FRONTEND_PORT))
        + " "
        + " ".join(shlex.quote(path) for path in paths)
    )
    output, event = await commands.execute(
        sandbox, {"command": command, "timeout": 600}
    )
    # A successful runner must actually complete at least one script. A
    # missing optional installation is a failure, never browser evidence.
    try:
        stdout = json.loads(output).get("stdout", "")
        receipt = json.loads(
            stdout.rsplit("ROAI_BROWSER_RESULT=", 1)[1].splitlines()[0]
        )
        if receipt.get("results") and all(
            item.get("status") == "passed" for item in receipt["results"]
        ):
            event["browser_result"] = receipt
    except (ValueError, IndexError, TypeError, AttributeError):
        pass
    return output, event
