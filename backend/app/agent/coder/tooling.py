"""Tool schemas and sandbox operations available to the coding agent."""

from __future__ import annotations

import difflib
import json
import hashlib
import posixpath
import re
import shlex
from collections.abc import Callable

from app.services import sandbox_host
from app.services import skills as skill_store
from app.agent.coder.starters import scaffold, BACKENDS, FRONTENDS

FRONTEND_PORT = 6767
BACKEND_PORT = 6969

# Only application source/configuration participates in progress. Dependency
# trees, generated builds, test caches and runtime databases do not invalidate
# a successful check merely because it produced normal byproducts.
SOURCE_FINGERPRINT = """
import hashlib, json, os
from pathlib import Path
root = Path('/workspace')
skip = {'.git', '.venv', 'venv', 'node_modules', 'dist', 'build', '__pycache__', '.pytest_cache', '.ruff_cache', '.mypy_cache', '.cache', 'coverage'}
runtime_suffixes = {'.sqlite', '.sqlite3', '.db', '.log', '.pid', '.pyc', '.tsbuildinfo'}
result = {}
for directory, dirs, files in os.walk(root, followlinks=False):
    dirs[:] = [name for name in dirs if name not in skip and not Path(directory, name).is_symlink()]
    for name in files:
        path = Path(directory, name)
        if path.is_symlink() or path.suffix in runtime_suffixes or name in {'.coverage'} or name.endswith(('-wal', '-shm', '-journal')):
            continue
        with path.open('rb') as source:
            result[str(path)] = hashlib.file_digest(source, 'sha256').hexdigest()
print(json.dumps(result))
"""


async def source_fingerprint(sandbox):
    try:
        result = await sandbox_host.call(
            "exec",
            sandbox,
            {"command": "python3 -c " + shlex.quote(SOURCE_FINGERPRINT), "timeout": 20},
            timeout=30,
        )
        if result.get("exit_code") == 0:
            data = json.loads(result.get("stdout", ""))
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return None


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
    if name in {"read_file", "write_file", "edit_file"}:
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
        "scaffold_project",
        "Create a new FastAPI/SQLite and/or Vite/TypeScript starter with local Pico CSS. Never overwrites existing files. Implement the requested app and rerun validation.",
        {
            "name": {"type": "string"},
            "backend": {"type": "string", "enum": ["none", *BACKENDS]},
            "frontend": {"type": "string", "enum": ["none", *FRONTENDS]},
        },
        ("name", "backend", "frontend"),
    ),
    _function(
        "check_preview",
        "Smoke-test a running preview with a real HTTP request. For a full-stack app, request an /api path through frontend port 6767 after starting both services.",
        {
            "port": {"type": "integer", "enum": [FRONTEND_PORT, BACKEND_PORT]},
            "path": {"type": "string"},
        },
        ("port", "path"),
    ),
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
        "Read exact UTF-8 content with character-offset pagination. Read before editing. Follow next_offset to read more.",
        {
            "path": {"type": "string"},
            "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 16000},
        },
        ("path",),
    ),
    _function(
        "edit_file",
        "Make one targeted edit to a file you have read. old_text must match exactly once; new_text replaces it (empty removes it). Stale reads or ambiguous matches leave the file unchanged.",
        {
            "path": {"type": "string"},
            "old_text": {"type": "string", "minLength": 1},
            "new_text": {"type": "string"},
        },
        ("path", "old_text", "new_text"),
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
    if name == "scaffold_project":
        return await scaffold(sandbox, args, _setup_python_project)
    if name == "check_preview":
        path, port = args["path"], args["port"]
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
        return json.dumps(result), {
            "command": command,
            "exit_code": result.get("exit_code"),
            "port": port,
        }
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
        content = result.get("content", "")
        offset, limit = args.get("offset", 0), args.get("limit", 6000)
        if (
            type(offset) is not int
            or offset < 0
            or type(limit) is not int
            or not 1 <= limit <= 16000
        ):
            raise ValueError(
                "Read offset/limit must be nonnegative character counts within bounds"
            )
        digest = hashlib.sha256(content.encode()).hexdigest()
        page = {
            "path": args["path"],
            "sha256": digest,
            "offset": offset,
            "total_chars": len(content),
            "content": content[offset : offset + limit],
            "next_offset": offset + limit if offset + limit < len(content) else None,
        }
        return json.dumps(page, ensure_ascii=False), {
            "read": page,
            "file_content": page["content"],
        }
    if name == "write_file":
        return await _write_file(sandbox, args)
    if name == "edit_file":
        expected = args.get("_expected_sha256")
        if not expected:
            return (
                "Read this file with read_file before editing it; nothing was changed.",
                {"exit_code": 1, "corrective": True},
            )
        content = (
            await sandbox_host.call("files/read", sandbox, {"path": args["path"]})
        )["content"]
        if hashlib.sha256(content.encode()).hexdigest() != expected:
            return (
                "File changed; read_file again before editing. Nothing was changed.",
                {"exit_code": 1, "corrective": True},
            )
        old, new = args["old_text"], args["new_text"]
        if not old or content.count(old) != 1:
            return (
                "old_text must match exactly once. Read/search for a larger unique context; nothing was changed.",
                {"exit_code": 1, "corrective": True},
            )
        start = content.index(old)
        covered = start
        for left, right in sorted(args.get("_read_ranges", [])):
            if left > covered:
                break
            covered = max(covered, right)
        if covered < start + len(old):
            return (
                "Read the exact target text with read_file before editing it. Nothing was changed.",
                {"exit_code": 1, "corrective": True},
            )
        return await _write_file(
            sandbox,
            {**args, "content": content.replace(old, new, 1), "_full_read": True},
        )
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
        return "Invalid Python project name or dependency requirement.", {
            "exit_code": 1,
            "corrective": True,
        }
    content = (
        "[project]\n"
        f"name = {json.dumps(project_name)}\n"
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        f"dependencies = [{', '.join(json.dumps(item) for item in dependencies)}]\n\n"
        "[dependency-groups]\n"
        f"dev = [{', '.join(json.dumps(item) for item in dev_dependencies)}]\n"
    )
    if (
        await sandbox_host.call(
            "files/stat", sandbox, {"path": "/workspace/pyproject.toml"}
        )
    ).get("exists"):
        return (
            "Python project already exists; use uv add/sync instead of replacing its configuration.",
            {"exit_code": 1, "corrective": True},
        )
    await sandbox_host.call(
        "files/write-checked",
        sandbox,
        {
            "path": "/workspace/pyproject.toml",
            "content": content,
            "expected_sha256": "missing",
        },
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
        "changed": True,
    }


async def _write_file(sandbox, args: dict) -> tuple[str, dict | None]:
    if str(args.get("path", "")).endswith("pyproject.toml"):
        return (
            "Direct pyproject.toml writes are disabled. Use setup_python_project "
            "for a new project or `uv add`/`uv remove` for an existing project.",
            {"exit_code": 1, "corrective": True},
        )
    content = str(args.get("content", ""))
    if str(args.get("path", "")).endswith(".py"):
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
    current = await sandbox_host.call("files/stat", sandbox, {"path": args["path"]})
    if current.get("exists"):
        if not args.get("_expected_sha256") or not args.get("_full_read"):
            return (
                "Read the complete current file before replacing it, or use edit_file after reading the relevant section. Nothing was changed.",
                {"exit_code": 1, "corrective": True},
            )
        previous = (
            await sandbox_host.call("files/read", sandbox, {"path": args["path"]})
        ).get("content", "")
        if hashlib.sha256(previous.encode()).hexdigest() != args["_expected_sha256"]:
            return (
                "File changed; read_file again before editing. Nothing was changed.",
                {"exit_code": 1, "corrective": True},
            )
        if previous == content:
            return "File already contains this content; nothing changed.", {
                "changed": False
            }
    usage = await sandbox_host.call("usage", sandbox)
    projected_usage = max(0, usage["usage_bytes"] - current["size"]) + len(
        content.encode()
    )
    if projected_usage > sandbox.workspace_quota_bytes:
        return "Workspace quota exceeded; stop and report this.", {"exit_code": 1}
    await sandbox_host.call(
        "files/write-checked",
        sandbox,
        {
            "path": args["path"],
            "content": content,
            "expected_sha256": (
                args.get("_expected_sha256") if current.get("exists") else "missing"
            ),
        },
    )
    written_content = content
    if str(args["path"]).endswith(".py"):
        # Checked host writes format/compile staging content before publication.
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
        "sha256": hashlib.sha256(written_content.encode()).hexdigest(),
        "changed": previous != written_content,
    }


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
    before = await source_fingerprint(sandbox)
    result = await sandbox_host.call(
        "exec", sandbox, {"command": command, "timeout": timeout}, timeout=timeout + 20
    )
    after = await source_fingerprint(sandbox)
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
    return json.dumps(result)[:80_000], {
        "command": command,
        "exit_code": result.get("exit_code"),
        "files": changed_files,
        "changed": bool(changed_files),
        "changes_unknown": not known,
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
