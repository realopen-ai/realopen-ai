"""Tool schemas and sandbox operations available to the coding agent."""

from __future__ import annotations

import difflib
import json
import posixpath
import re
import shlex
from collections.abc import Callable

from app.services import sandbox_host
from app.services import skills as skill_store

FRONTEND_PORT = 6767
BACKEND_PORT = 6969


def workspace_path(path: object, default: str = "/workspace") -> str:
    """Canonicalize model-provided paths into the sandbox workspace."""
    value = str(path or default).strip()
    if value in {"", "/", "/workspace"}:
        return "/workspace"
    relative = (
        value[len("/workspace/") :]
        if value.startswith("/workspace/")
        else value.lstrip("/")
    )
    # Treat traversal-looking model output as a workspace-relative intent;
    # containment is still independently enforced by the host runtime.
    parts = [part for part in relative.split("/") if part not in {"", ".", ".."}]
    return posixpath.join("/workspace", *parts)


def normalize_tool_args(name: str, args: dict) -> dict:
    """Normalize file-oriented tool arguments before events or API calls."""
    normalized = dict(args)
    if name in {"read_file", "write_file"}:
        normalized["path"] = workspace_path(normalized.get("path"))
    elif name in {"list_files", "search_files"}:
        normalized["path"] = workspace_path(normalized.get("path", "/workspace"))
    return normalized


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
        "load_skill",
        "Load the full instructions for one relevant skill from the catalog.",
        {"name": {"type": "string"}, "resource": {"type": "string"}},
        ("name",),
    ),
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
            "port": {"type": "integer", "enum": [FRONTEND_PORT, BACKEND_PORT]},
        },
        ("command", "port"),
    ),
]


async def dispatch_tool(
    sandbox, name: str, args: dict, progress: Callable[[dict], None] | None = None
) -> tuple[str, dict | None]:
    if name == "load_skill":
        return (
            skill_store.load_for_agent(
                str(args["name"]),
                "coder",
                str(args["resource"]) if args.get("resource") else None,
            ),
            None,
        )
    if name == "setup_python_project":
        return await _setup_python_project(sandbox, args, progress)
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


async def _setup_python_project(
    sandbox, args: dict, progress: Callable[[dict], None] | None = None
) -> tuple[str, dict | None]:
    project_name = re.sub(r"[^a-zA-Z0-9._-]", "-", str(args["name"]))[:80]
    dependencies = [str(item) for item in args.get("dependencies", [])]
    dev_dependencies = [str(item) for item in args.get("dev_dependencies", [])]
    # Starlette 1.7's TestClient uses the httpx2 package. Small coder models
    # frequently remember pytest but omit this transitive test dependency.
    def requirement_name(item: str) -> str:
        return re.split(r"[\[<>=!~]", item, maxsplit=1)[0].lower()

    if any(requirement_name(item) == "fastapi" for item in dependencies):
        has_httpx2 = any(
            requirement_name(item) == "httpx2" for item in dev_dependencies
        )
        if not has_httpx2:
            dev_dependencies.append("httpx2")
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
    if progress:
        progress(
            {
                "stage": "project_written",
                "file": "/workspace/pyproject.toml",
                "file_content": content,
            }
        )
    result = await sandbox_host.call(
        "exec",
        sandbox,
        {"command": "uv sync --all-groups", "timeout": 300},
        timeout=320,
    )
    if progress:
        progress({"stage": "sync_completed"})
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
    if str(args.get("path", "")).endswith(".py"):
        content = _repair_python_indentation(content)
        try:
            compile(content, str(args["path"]), "exec")
        except SyntaxError as exc:
            location = f"line {exc.lineno}" if exc.lineno else "unknown line"
            return (
                f"Python write rejected before replacing the file: "
                f"{exc.__class__.__name__} at {location}: {exc.msg}. "
                "Correct the source and call write_file again.",
                {
                    "file": args["path"],
                    "exit_code": 1,
                    "corrective": True,
                },
            )
    previous = ""
    try:
        previous = (
            await sandbox_host.call("files/read", sandbox, {"path": args["path"]})
        ).get("content", "")
    except Exception:
        pass
    usage = await sandbox_host.call("usage", sandbox)
    current = await sandbox_host.call("files/stat", sandbox, {"path": args["path"]})
    projected_usage = max(0, usage["usage_bytes"] - current["size"]) + len(
        content.encode()
    )
    if projected_usage > sandbox.workspace_quota_bytes:
        return "Workspace quota exceeded; stop and report this.", None
    await sandbox_host.call(
        "files/write", sandbox, {"path": args["path"], "content": content}
    )
    written_content = content
    if str(args["path"]).endswith(".py"):
        formatted = await sandbox_host.call(
            "exec",
            sandbox,
            {
                "command": (
                    "autopep8 --in-place --aggressive -- "
                    + shlex.quote(str(args["path"]))
                ),
                "timeout": 30,
            },
            timeout=50,
        )
        if formatted.get("exit_code") != 0:
            raise RuntimeError(
                "Python formatting failed: "
                + (
                    formatted.get("stderr")
                    or formatted.get("stdout")
                    or "unknown error"
                )
            )
        validation = await sandbox_host.call(
            "exec",
            sandbox,
            {
                "command": "python3 -m py_compile " + shlex.quote(str(args["path"])),
                "timeout": 30,
            },
            timeout=50,
        )
        if validation.get("exit_code") != 0:
            raise RuntimeError(
                "Python validation failed after formatting: "
                + (
                    validation.get("stderr")
                    or validation.get("stdout")
                    or "unknown error"
                )
            )
        written_content = (
            await sandbox_host.call("files/read", sandbox, {"path": args["path"]})
        ).get("content", content)
    diff = difflib.unified_diff(
        previous.splitlines(),
        written_content.splitlines(),
        fromfile=f"a/{args['path']}",
        tofile=f"b/{args['path']}",
        lineterm="",
        n=1,
    )
    return f"Wrote {len(written_content.encode())} bytes to {args['path']}", {
        "file": args["path"],
        "diff": "\n".join(list(diff)[:5]),
        "file_content": written_content,
    }


def _repair_python_indentation(content: str) -> str:
    """Repair off-grid indentation only when the source cannot be parsed."""
    try:
        compile(content, "<coder-write>", "exec")
        return content
    except IndentationError:
        repaired = []
        for line in content.splitlines(keepends=True):
            match = re.match(r"^( +)(?=\S)", line)
            if match and len(match.group(1)) % 4:
                width = max(4, (len(match.group(1)) // 4) * 4)
                line = " " * width + line[len(match.group(1)) :]
            repaired.append(line)
        candidate = "".join(repaired)
        try:
            compile(candidate, "<coder-write>", "exec")
        except (IndentationError, SyntaxError):
            return content
        return candidate
    except SyntaxError:
        return content


async def _run_command(sandbox, args: dict) -> tuple[str, dict | None]:
    command = str(args["command"])
    server_launch = re.search(
        r"(?:^|[;&|]\s*|\s)(?:uvicorn|fastapi\s+(?:dev|run)|"
        r"(?:bun|npm|pnpm|yarn)\s+(?:run\s+)?dev|vite|next\s+dev)\b",
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
