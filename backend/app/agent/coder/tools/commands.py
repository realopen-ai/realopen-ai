"""Shared command guards, source tracking and project receipts."""

import json
import re

from app.services import sandbox_host
from app.agent.coder.project import configuration_changed, project_snapshot
from app.agent.coder.tools import workspace


async def execute(
    sandbox, args: dict, *, refresh_project=False
) -> tuple[str, dict | None]:
    command = str(args["command"])
    server_launch = re.search(
        r"(?:^|[;&|]\s*|\s)(?:uvicorn|fastapi\s+(?:dev|run)|"
        r"(?:bun|npm|pnpm|yarn)\s+(?:run\s+)?dev|"
        r"vite(?:\s+(?:dev|preview)\b|(?=\s*(?:--|$|[;&|])))|next\s+dev)\b",
        command,
        re.IGNORECASE,
    )
    if server_launch:
        return (
            "Web servers must be started with start_preview. Use port 6969 for "
            "a backend/API or 6767 for a frontend.",
            {"command": command, "exit_code": 2},
        )
    prohibited = (
        re.search(r"(^|\s)(pip|pip3)\s+install\b", command)
        or re.search(r"\b(npm\s+(install|i)\s+-g\s+bun|install.*\buv\b)", command)
        or (
            "pyproject.toml" in command
            and re.search(r"\b(rm|sed|echo|cat|printf)\b", command)
        )
    )
    if prohibited:
        dependency_hint = (
            " Run `uv add --dev httpx2` for FastAPI TestClient support, or "
            "`uv add <package>` for another missing dependency, then rerun the test."
            if re.search(r"(^|\s)(pip|pip3)\s+install\b", command)
            else ""
        )
        return (
            "Command refused. uv and bun are preinstalled; use setup_python_project, "
            "`uv add`, or the existing project helpers." + dependency_hint,
            {"command": command, "exit_code": 2, "corrective": True},
        )
    timeout = int(args.get("timeout", 120))
    before = await workspace.source_fingerprint(sandbox)
    result = await sandbox_host.call(
        "exec", sandbox, {"command": command, "timeout": timeout}, timeout=timeout + 20
    )
    after = await workspace.source_fingerprint(sandbox)
    known = before is not None and after is not None
    changed_files = (
        [
            path
            for path in before.keys() | after.keys()
            if before.get(path) != after.get(path)
        ]
        if known
        else []
    )
    if not known:
        result["validation_warning"] = (
            "Source-change tracking was unavailable. This command alone cannot verify the current application; rerun validation once tracking is available."
        )
    usage = await sandbox_host.call("usage", sandbox)
    if usage["usage_bytes"] > sandbox.workspace_quota_bytes:
        return "Workspace quota exceeded after this command; stop and report it.", {
            "command": command,
            "exit_code": 1,
            "files": changed_files,
            "changed": bool(changed_files),
            "changes_unknown": not known,
        }
    event = {
        "command": command,
        "exit_code": result.get("exit_code"),
        "files": changed_files,
        "changed": bool(changed_files),
        "changes_unknown": not known,
    }
    if refresh_project or not known or configuration_changed(event):
        event["project_facts"] = await project_snapshot(sandbox)
    return json.dumps(result)[:80_000], event
