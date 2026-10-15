"""Deterministic, extensible project setup; no model-generated packaging."""

import json
from pathlib import Path

from app.services import sandbox_host

ASSETS = Path(__file__).with_name("starters")
BACKENDS = {
    "fastapi": {
        "assets": "fastapi",
        "setup": "python",
        "dependencies": ["fastapi", "uvicorn"],
        "dev_dependencies": ["pytest", "httpx2"],
        "test": "uv run python -m pytest backend/tests -q",
        "preview": "uv run uvicorn backend.app:app --host 0.0.0.0 --port 6969",
    }
}
FRONTENDS = {
    "vite": {
        "assets": "vite",
        "install": "bun install --cwd frontend",
        "build": "bun run --cwd frontend build",
        "preview": "bun run --cwd frontend dev -- --host 0.0.0.0 --port 6767",
    }
}


async def scaffold(sandbox, args, setup_python, progress=None):
    backend, frontend = args["backend"], args["frontend"]
    if (
        backend not in {"none", *BACKENDS}
        or frontend not in {"none", *FRONTENDS}
        or (backend == frontend == "none")
    ):
        raise ValueError("Choose supported backend/frontend starters, not both none")
    files = {}
    for registry, selected in [(BACKENDS, backend), (FRONTENDS, frontend)]:
        if selected != "none":
            root = ASSETS / registry[selected]["assets"]
            for path in sorted(root.rglob("*")):
                if (
                    path.is_file()
                    and "__pycache__" not in path.parts
                    and path.suffix != ".pyc"
                ):
                    files["/workspace/" + path.relative_to(root).as_posix()] = (
                        path.read_text()
                    )
    if frontend != "none" and backend == "none":
        files["/workspace/frontend/src/main.ts"] = (
            'import "@picocss/pico/css/pico.min.css";\nimport "./style.css";\ndocument.querySelector<HTMLParagraphElement>("#status")!.textContent = "Ready";\n'
        )
    project = {"backend": BACKENDS.get(backend), "frontend": FRONTENDS.get(frontend)}
    files["/workspace/.realopen-project.json"] = json.dumps(project, indent=2)
    python_backend = backend != "none" and BACKENDS[backend].get("setup") == "python"
    targets = [*files, *(["/workspace/pyproject.toml"] if python_backend else [])]
    for target in targets:
        if (await sandbox_host.call("files/stat", sandbox, {"path": target})).get(
            "exists"
        ):
            raise ValueError(
                f"Starter would overwrite {target}; inspect and adapt the existing project instead"
            )
    usage = await sandbox_host.call("usage", sandbox)
    if (
        usage["usage_bytes"] + sum(len(content.encode()) for content in files.values())
        > sandbox.workspace_quota_bytes
    ):
        raise ValueError("Workspace quota cannot fit the starter; nothing was written")
    created = []
    try:
        for target, content in files.items():
            await sandbox_host.call(
                "files/write-checked",
                sandbox,
                {"path": target, "content": content, "expected_sha256": "missing"},
            )
            created.append(target)
    except Exception as error:
        return json.dumps(
            {
                "created_files": created,
                "error": str(error),
                "next_step": "Inspect the files kept and resume setup; do not resubmit scaffold_project.",
            }
        ), {
            "files": created,
            "project": project,
            "changed": bool(created),
            "exit_code": 1,
        }
    if progress:
        progress({"stage": "files_written", "created_files": list(created)})
    checks = []
    try:
        if python_backend:
            result, event = await setup_python(
                sandbox,
                {
                    "name": args.get("name", "workspace-app"),
                    **{
                        key: BACKENDS[backend][key]
                        for key in ["dependencies", "dev_dependencies"]
                    },
                },
                progress,
            )
            checks.append(
                {
                    "action": "python_setup",
                    "command": "uv sync --all-groups",
                    "result": result,
                    "exit_code": event.get("exit_code") if event else 1,
                }
            )
            if event and event.get("file"):
                created.append(event["file"])
        elif backend != "none" and BACKENDS[backend].get("install"):
            result = await sandbox_host.call(
                "exec",
                sandbox,
                {"command": BACKENDS[backend]["install"], "timeout": 300},
                timeout=320,
            )
            checks.append(
                {
                    "action": "backend_install",
                    "command": BACKENDS[backend]["install"],
                    **result,
                }
            )
        if frontend != "none":
            result = await sandbox_host.call(
                "exec",
                sandbox,
                {"command": FRONTENDS[frontend]["install"], "timeout": 300},
                timeout=320,
            )
            checks.append(
                {
                    "action": "frontend_install",
                    "command": FRONTENDS[frontend]["install"],
                    **result,
                }
            )
    except Exception as error:
        checks.append(
            {
                "action": "dependency_setup",
                "command": "",
                "error": str(error),
                "exit_code": 1,
            }
        )
    return json.dumps(
        {"created_files": created, "project": project, "setup": checks},
        ensure_ascii=False,
    ), {
        "files": created,
        "project": project,
        "changed": True,
        "command": "\n".join(item["command"] for item in checks),
        "exit_code": 1 if any(item.get("exit_code") != 0 for item in checks) else 0,
    }
