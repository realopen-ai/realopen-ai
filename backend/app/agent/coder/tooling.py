"""Tool schemas and sandbox operations available to the coding agent."""

from __future__ import annotations

import difflib
import json
import re
import shlex

from app.services import sandbox_host


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

CODER_TOOLS = [
    _function(
        "setup_python_project",
        "Create a valid uv project and install all dependencies once.",
        {
            "name": {"type": "string"},
            "dependencies": {"type": "array", "items": {"type": "string"}},
            "dev_dependencies": {"type": "array", "items": {"type": "string"}},
        },
        ("name", "dependencies", "dev_dependencies"),
    ),
    _function("list_files", "List the workspace tree.", {"path": {"type": "string"}}),
    _function(
        "read_file",
        "Read a UTF-8 workspace file.",
        {"path": {"type": "string"}},
        ("path",),
    ),
    _function(
        "write_file",
        "Create or replace a workspace file.",
        {"path": {"type": "string"}, "content": {"type": "string"}},
        ("path", "content"),
    ),
    _function(
        "search_files",
        "Search workspace text with ripgrep.",
        {"query": {"type": "string"}, "path": {"type": "string"}},
        ("query",),
    ),
    _function(
        "run_command",
        "Run a shell command from /workspace.",
        COMMAND_PROPERTIES,
        ("command",),
    ),
    _function(
        "run_tests",
        "Run the repository's relevant test command.",
        COMMAND_PROPERTIES,
        ("command",),
    ),
    _function(
        "install_dependencies",
        "Install dependencies with the repository package manager.",
        COMMAND_PROPERTIES,
        ("command",),
    ),
    _function(
        "start_preview",
        "Start the tested web app and expose it in the Preview panel.",
        {
            "command": {"type": "string"},
            "port": {"type": "integer", "enum": [3000, 5173, 8000, 8080]},
        },
        ("command", "port"),
    ),
]


async def dispatch_tool(sandbox, name: str, args: dict) -> tuple[str, dict | None]:
    if name == "setup_python_project":
        return await _setup_python_project(sandbox, args)
    if name == "list_files":
        result = await sandbox_host.call(
            "files", sandbox, {"path": args.get("path", "/workspace")}
        )
        return json.dumps(result)[:40_000], None
    if name == "read_file":
        result = await sandbox_host.call("files/read", sandbox, {"path": args["path"]})
        return result.get("content", "")[:80_000], None
    if name == "write_file":
        return await _write_file(sandbox, args)
    if name == "search_files":
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
    if name in {"run_command", "run_tests", "install_dependencies"}:
        return await _run_command(sandbox, args)
    if name == "start_preview":
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
        return json.dumps(result), {
            "command": args["command"],
            "preview_url": f"/api/sandboxes/{sandbox.id}/preview/{port}/",
            "port": port,
        }
    return f"Unknown coder tool: {name}", None


async def _setup_python_project(sandbox, args: dict) -> tuple[str, dict | None]:
    project_name = re.sub(r"[^a-zA-Z0-9._-]", "-", str(args["name"]))[:80]
    dependencies = [str(item) for item in args.get("dependencies", [])]
    dev_dependencies = [str(item) for item in args.get("dev_dependencies", [])]
    requirement = re.compile(
        r"^[a-zA-Z0-9._-]+(?:\[[a-zA-Z0-9,._-]+\])?(?:[<>=!~].+)?$"
    )
    if not project_name or not all(
        requirement.fullmatch(item) for item in dependencies + dev_dependencies
    ):
        return "Invalid Python project name or dependency requirement.", None
    content = (
        "[project]\n"
        f"name = {json.dumps(project_name)}\n"
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        f"dependencies = [{', '.join(json.dumps(item) for item in dependencies)}]\n\n"
        "[dependency-groups]\n"
        f"dev = [{', '.join(json.dumps(item) for item in dev_dependencies)}]\n"
    )
    await sandbox_host.call(
        "files/write",
        sandbox,
        {"path": "/workspace/pyproject.toml", "content": content},
    )
    result = await sandbox_host.call(
        "exec",
        sandbox,
        {"command": "uv sync --all-groups", "timeout": 300},
        timeout=320,
    )
    return json.dumps(result)[:80_000], {
        "file": "/workspace/pyproject.toml",
        "file_content": content,
        "command": "uv sync --all-groups",
        "exit_code": result.get("exit_code"),
    }


async def _write_file(sandbox, args: dict) -> tuple[str, dict | None]:
    if str(args.get("path", "")).endswith("pyproject.toml"):
        return (
            "Direct pyproject.toml writes are disabled. Use setup_python_project "
            "for a new project or `uv add`/`uv remove` for an existing project.",
            None,
        )
    content = str(args.get("content", ""))
    previous = ""
    try:
        previous = (
            await sandbox_host.call("files/read", sandbox, {"path": args["path"]})
        ).get("content", "")
    except Exception:
        pass
    usage = await sandbox_host.call("usage", sandbox)
    if usage["usage_bytes"] + len(content.encode()) > sandbox.workspace_quota_bytes:
        return "Workspace quota exceeded; stop and report this.", None
    await sandbox_host.call(
        "files/write", sandbox, {"path": args["path"], "content": content}
    )
    diff = difflib.unified_diff(
        previous.splitlines(),
        content.splitlines(),
        fromfile=f"a/{args['path']}",
        tofile=f"b/{args['path']}",
        lineterm="",
        n=1,
    )
    return f"Wrote {len(content.encode())} bytes to {args['path']}", {
        "file": args["path"],
        "diff": "\n".join(list(diff)[:5]),
        "file_content": content,
    }


async def _run_command(sandbox, args: dict) -> tuple[str, dict | None]:
    command = str(args["command"])
    prohibited = (
        re.search(r"(^|\s)(pip|pip3)\s+install\b", command)
        or re.search(r"\b(npm\s+(install|i)\s+-g\s+bun|install.*\buv\b)", command)
        or (
            "pyproject.toml" in command
            and re.search(r"\b(rm|sed|echo|cat|printf)\b", command)
        )
    )
    if prohibited:
        return (
            "Command refused. uv and bun are preinstalled; use setup_python_project, "
            "`uv add`, or the existing project helpers.",
            {"command": command, "exit_code": 2},
        )
    timeout = int(args.get("timeout", 120))
    result = await sandbox_host.call(
        "exec", sandbox, {"command": command, "timeout": timeout}, timeout=timeout + 20
    )
    usage = await sandbox_host.call("usage", sandbox)
    if usage["usage_bytes"] > sandbox.workspace_quota_bytes:
        return "Workspace quota exceeded after this command; stop and report it.", {
            "command": command,
            "exit_code": result.get("exit_code"),
        }
    return json.dumps(result)[:80_000], {
        "command": command,
        "exit_code": result.get("exit_code"),
    }


async def infer_preview(sandbox) -> tuple[str, int] | None:
    for path, module in (
        ("/workspace/app/main.py", "app.main:app"),
        ("/workspace/main.py", "main:app"),
    ):
        try:
            result = await sandbox_host.call("files/read", sandbox, {"path": path})
        except Exception:
            continue
        if "FastAPI(" in result.get("content", ""):
            return f"uv run uvicorn {module} --host 0.0.0.0 --port 8000", 8000
    try:
        package = json.loads(
            (
                await sandbox_host.call(
                    "files/read", sandbox, {"path": "/workspace/package.json"}
                )
            ).get("content", "{}")
        )
        if package.get("scripts", {}).get("dev"):
            return "bun run dev --host 0.0.0.0 --port 5173", 5173
    except Exception:
        pass
    return None
