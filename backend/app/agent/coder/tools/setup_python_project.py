"""setup python project tool: schema and execution."""

import json
import re
from collections.abc import Callable
from app.services import sandbox_host
from app.agent.coder.tools.schema import _function

SCHEMA = _function(
    "setup_python_project",
    "Create a valid uv project and install all dependencies once.",
    {
        "name": {"type": "string"},
        "dependencies": {"type": "array", "items": {"type": "string"}},
        "dev_dependencies": {"type": "array", "items": {"type": "string"}},
    },
    ("name", "dependencies", "dev_dependencies"),
)


async def execute(
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
