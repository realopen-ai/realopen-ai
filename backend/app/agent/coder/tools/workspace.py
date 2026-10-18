"""Workspace paths and source-change fingerprints."""

import json
import posixpath
import shlex

from app.services import sandbox_host

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
