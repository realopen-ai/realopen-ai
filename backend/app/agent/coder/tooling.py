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
from app.services.sandbox_manager import remember_preview_command
from app.agent.coder.starters import scaffold, BACKENDS, FRONTENDS
from app.agent.coder.project import configuration_changed, project_snapshot

FRONTEND_PORT = 6767
BACKEND_PORT = 6969
MAX_READ_CHARS = 16000
MAX_READ_LINES = 400
MAX_INLINE_WRITE_RECEIPT_CHARS = 4000

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
        "Read exact source text. Small files are returned whole. Optional start_line/end_line are 1-based inclusive LINE numbers, not characters. Large files return complete lines and next_line; continue from there.",
        {
            "path": {"type": "string"},
            "start_line": {"type": "integer", "minimum": 1},
            "end_line": {"type": "integer", "minimum": 1},
        },
        ("path",),
    ),
    _function(
        "edit_file",
        "Replace one unique exact old_text match with new_text (empty removes it). Prefer inspecting current source, but no exact read-range proof is required. Concurrent changes, missing or ambiguous matches leave the file unchanged.",
        {
            "path": {"type": "string"},
            "old_text": {"type": "string", "minLength": 1},
            "new_text": {"type": "string"},
        },
        ("path", "old_text", "new_text"),
    ),
    _function(
        "write_file",
        "Create a file, or replace a complete file whose current contents you have read or just written. Prefer edit_file for targeted corrections.",
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
    ),
    _function(
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
    ),
    _function(
        "start_preview",
        "Start or replace a supervised web-app preview. The command is persisted and automatically restored when this sandbox restarts.",
        {
            "command": {"type": "string"},
            "port": {"type": "integer", "enum": [FRONTEND_PORT, BACKEND_PORT]},
        },
        ("command", "port"),
    ),
]


def _read_page(args: dict, content: str) -> tuple[dict, int]:
    lines = content.splitlines(keepends=True)
    start = args.get("start_line", 1)
    end = args.get("end_line", max(1, len(lines)))
    if (
        type(start) is not int
        or type(end) is not int
        or start < 1
        or end < start
        or start > max(1, len(lines))
    ):
        raise ValueError(
            f"Use 1-based inclusive start_line/end_line. This file has {len(lines)} lines; "
            "omit both to read from the beginning."
        )
    if "offset" in args or "limit" in args:
        raise ValueError(
            "read_file now uses start_line/end_line (1-based lines), not offset/limit characters."
        )
    offset = sum(len(line) for line in lines[: start - 1])
    selected, size = [], 0
    for line in lines[start - 1 : min(end, start + MAX_READ_LINES - 1)]:
        if size + len(line) > MAX_READ_CHARS:
            if not selected:
                raise ValueError(
                    f"Line {start} exceeds the {MAX_READ_CHARS}-character read budget. "
                    "Use search_files for a focused excerpt or edit_file with a unique exact match; "
                    "no source text was silently truncated."
                )
            break
        selected.append(line)
        size += len(line)
    last = start - 1 + len(selected)
    return {
        "path": args["path"],
        "sha256": hashlib.sha256(content.encode()).hexdigest(),
        "start_line": start,
        "end_line": last,
        "total_lines": len(lines),
        "total_chars": len(content),
        "content": "".join(selected),
        "next_line": last + 1 if last < len(lines) else None,
    }, offset


def _match_feedback(content: str, old: str) -> str:
    positions, cursor = [], 0
    while old and len(positions) < 4:
        match = content.find(old, cursor)
        if match < 0:
            break
        positions.append(content.count("\n", 0, match) + 1)
        cursor = match + 1
    if positions:
        return (
            f"old_text matches at line(s) {positions}. "
            "Use read_file with start_line/end_line around the intended location. "
            "For multiple matches, include more surrounding text to identify exactly one."
        )
    first = next((line for line in old.splitlines() if line.strip()), "")
    lines = content.splitlines()
    similar = difflib.get_close_matches(first, lines, n=3, cutoff=0.6) if first else []
    # Corrective feedback is application-owned; do not promote source text
    # (which can contain instructions) into that trusted message.
    hints = sorted({lines.index(line) + 1 for line in similar})
    return (
        "old_text was not found verbatim. Read/search the current source and copy exact text. "
        + (
            "Similar lines to inspect with read_file (not replacements): "
            + json.dumps(hints)
            if hints
            else ""
        )
    )


async def dispatch_tool(
    sandbox, name: str, args: dict, progress: Callable[[dict], None] | None = None
) -> tuple[str, dict | None]:
    if name == "scaffold_project":
        return await scaffold(sandbox, args, _setup_python_project, progress)
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
        page, offset = _read_page(args, content)
        return json.dumps(page, ensure_ascii=False), {
            "read": {**page, "offset": offset},
            "file_content": page["content"],
        }
    if name == "write_file":
        return await _write_file(sandbox, args)
    if name == "edit_file":
        content = (
            await sandbox_host.call("files/read", sandbox, {"path": args["path"]})
        )["content"]
        current_hash = hashlib.sha256(content.encode()).hexdigest()
        expected = args.get("_expected_sha256")
        if expected and current_hash != expected:
            return (
                f"{args['path']} changed since your last snapshot. No edit was applied. "
                "Read the current file, then retry against that version. "
                + _match_feedback(content, args["old_text"]),
                {"exit_code": 1, "corrective": True},
            )
        old, new = args["old_text"], args["new_text"]
        first = content.find(old) if old else -1
        if first < 0 or content.find(old, first + 1) >= 0:
            return (
                f"No edit was applied to {args['path']}. "
                + _match_feedback(content, old),
                {"exit_code": 1, "corrective": True},
            )
        return await _write_file(
            sandbox,
            {
                **args,
                "content": content.replace(old, new, 1),
                "_expected_sha256": current_hash,
                "_full_read": True,
            },
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
        return await _run_command(
            sandbox, args, refresh_project=name == "install_dependencies"
        )
    if name == "run_checks":
        checks = []
        for index, command in enumerate(args["commands"]):
            try:
                output, event = await _run_command(
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
                progress(
                    {"stage": "check_completed", "index": index, "check": checks[-1]}
                )
        return json.dumps({"checks": checks}), {
            "checks": checks,
            "command": "\n".join(args["commands"]),
            "exit_code": (
                0 if all(check.get("exit_code") == 0 for check in checks) else 1
            ),
            "files": sorted(
                {path for check in checks for path in check.get("files", [])}
            ),
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
    if name == "run_browser_tests":
        paths = [workspace_path(path) for path in args["paths"]]
        command = (
            "roai-browser-test --port "
            + str(args.get("port", FRONTEND_PORT))
            + " "
            + " ".join(shlex.quote(path) for path in paths)
        )
        output, event = await _run_command(
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
        await remember_preview_command(sandbox, port, args["command"])
        result["persistent"] = True
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
    sha256 = hashlib.sha256(written_content.encode()).hexdigest()
    receipt = {
        "status": f"Wrote {len(written_content.encode())} bytes to {args['path']}",
        "path": args["path"],
        "bytes": len(written_content.encode()),
        "sha256": sha256,
        "host_formatting_changed_content": written_content != content,
        "message": (
            "The snapshot below is the exact persisted file after host formatting."
            if len(written_content) <= MAX_INLINE_WRITE_RECEIPT_CHARS
            else "The exact persisted snapshot is recorded internally; use read_file for source text."
        ),
    }
    if len(written_content) <= MAX_INLINE_WRITE_RECEIPT_CHARS:
        receipt["persisted_content"] = written_content
    return json.dumps(receipt, ensure_ascii=False), {
        "file": args["path"],
        "diff": "\n".join(list(diff)[:5]),
        "file_content": written_content,
        "sha256": sha256,
        "changed": previous != written_content,
    }


async def _run_command(
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
