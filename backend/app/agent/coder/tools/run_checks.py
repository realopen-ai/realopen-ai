"""run checks tool: schema and execution."""

import json
from app.agent.coder.tools import commands
from app.agent.coder.tools.schema import _function

SCHEMA = _function(
    "run_checks",
    "Run a small batch of independent test/typecheck/build commands sequentially. Each exit code is retained, even when another check fails. Reuse passing evidence until source changes; do not use watch mode.",
    {
        "commands": {
            "type": "array",
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
            "maxItems": 8,
        },
        "timeout": {"type": "integer", "minimum": 1, "maximum": 600},
    },
    ("commands",),
)


async def execute(sandbox, args: dict, progress=None) -> tuple[str, dict | None]:
    checks = []
    for index, command in enumerate(args["commands"]):
        try:
            output, event = await commands.execute(
                sandbox, {"command": command, "timeout": args.get("timeout", 120)}
            )
        except Exception as error:
            output, event = f"Check could not complete: {error}", {
                "command": command,
                "exit_code": 1,
                "changes_unknown": True,
            }
        checks.append({**event, "output": output})
        if progress:
            progress({"stage": "check_completed", "index": index, "check": checks[-1]})
    return json.dumps({"checks": checks}), {
        "checks": checks,
        "command": "\n".join(args["commands"]),
        "exit_code": (0 if all(check.get("exit_code") == 0 for check in checks) else 1),
        "files": sorted({path for check in checks for path in check.get("files", [])}),
        "changed": any(check.get("changed") for check in checks),
        "changes_unknown": any(check.get("changes_unknown") for check in checks),
        **next(
            (
                {"project_facts": check["project_facts"]}
                for check in reversed(checks)
                if "project_facts" in check
            ),
            {},
        ),
    }
