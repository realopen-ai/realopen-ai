"""Deterministic, extensible project setup; no model-generated packaging."""

import hashlib
import json
from pathlib import Path

from app.services import sandbox_host
from app.agent.coder.styling import DEFAULT_STYLING, apply_styling, validate_styling
from app.agent.coder.handoff import project_handoff

ASSETS = Path(__file__).with_name("starters")
BACKENDS = {
    "fastapi": {
        "request_names": ["FastAPI"],
        "assets": "fastapi",
        "setup": "python",
        "dependencies": ["fastapi", "uvicorn"],
        "dev_dependencies": ["pytest", "httpx2"],
        "test": "uv run python -m pytest backend/tests -q",
        "preview": "uv run uvicorn backend.app:app --host 0.0.0.0 --port 6969",
        "readiness_path": "/api/health",
    }
}
FRONTENDS = {
    "react-vite": {
        "request_names": ["React", "React.js", "ReactJS"],
        "assets": "vite",
        "overlay": "react",
        "omit": [
            "frontend/src/main.ts",
            "frontend/src/status.ts",
            "frontend/src/status.test.ts",
        ],
        "entrypoint": "src/main.tsx",
        "framework": "react",
        "readiness_path": "/",
        "install": "bun install --cwd frontend",
        "test": "bun run --cwd frontend test",
        "typecheck": "bun run --cwd frontend typecheck",
        "build": "bun run --cwd frontend build",
        "preview": "bun run --cwd frontend dev -- --host 0.0.0.0 --port 6767",
    },
    "vite": {
        "request_names": ["vanilla TypeScript", "vanilla JavaScript"],
        "assets": "vite",
        "entrypoint": "src/main.ts",
        "framework": "vanilla",
        "readiness_path": "/",
        "install": "bun install --cwd frontend",
        "test": "bun run --cwd frontend test",
        "typecheck": "bun run --cwd frontend typecheck",
        "build": "bun run --cwd frontend build",
        "preview": "bun run --cwd frontend dev -- --host 0.0.0.0 --port 6767",
    },
}


def read_assets(name):
    root = ASSETS / name
    return {
        path.relative_to(root).as_posix(): path.read_text()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


def frontend_assets(name, *, minimal=False):
    entry = FRONTENDS[name]
    files = read_assets(entry["assets"])
    if entry.get("overlay"):
        files.update(read_assets(entry["overlay"]))
    for path in entry.get("omit", []):
        files.pop(path, None)
    if minimal:
        files = {
            path: content
            for path, content in files.items()
            if not path.startswith("frontend/src/")
            or path in {"frontend/src/vite-env.d.ts", "frontend/src/style.css"}
        }
        if entry["framework"] == "react":
            files.update(
                {
                    p: c
                    for p, c in read_assets("minimal").items()
                    if p.startswith("frontend/")
                }
            )
        else:
            files["frontend/src/main.ts"] = (
                'import "./style.css";\ndocument.querySelector("#status")!.textContent = "Ready";\n'
            )
    return files


async def scaffold(
    sandbox, args, setup_python, progress=None, *, generated=None, composition=None
):
    backend, frontend = args["backend"], args.get("frontend", "react-vite")
    styling = validate_styling(args.get("styling", DEFAULT_STYLING))
    if (
        backend not in {"none", *BACKENDS}
        or frontend not in {"none", *FRONTENDS}
        or (backend == frontend == "none")
    ):
        raise ValueError("Choose supported backend/frontend starters, not both none")
    files = {}
    minimal = composition is None
    if backend != "none":
        backend_files = read_assets(BACKENDS[backend]["assets"])
        if minimal and backend == "fastapi":
            backend_files = {
                p: c
                for p, c in backend_files.items()
                if p in {"backend/app.py", "backend/__init__.py"}
            }
        files.update(
            {"/workspace/" + path: content for path, content in backend_files.items()}
        )
    if frontend != "none":
        files.update(
            {
                "/workspace/" + path: content
                for path, content in frontend_assets(frontend, minimal=minimal).items()
            }
        )
    if frontend != "none" and backend == "none" and not minimal:
        if FRONTENDS[frontend]["framework"] == "react":
            files["/workspace/frontend/src/main.tsx"] = files[
                "/workspace/frontend/src/main.tsx"
            ].replace("connected={true}", "connected={false}")
        else:
            files["/workspace/frontend/src/main.ts"] = (
                'import "./style.css";\ndocument.querySelector<HTMLParagraphElement>("#status")!.textContent = "Ready";\n'
            )
    if generated:
        files.update(
            {"/workspace/" + path: content for path, content in generated.items()}
        )
    if frontend != "none" and not composition:
        apply_styling(files, styling, prefix="/workspace/", minimal=True)
    project = {
        "backend": dict(BACKENDS[backend]) if backend != "none" else None,
        "frontend": dict(FRONTENDS[frontend]) if frontend != "none" else None,
    }
    if minimal:
        for role in ("backend", "frontend"):
            if project[role]:
                project[role]["scaffold"] = "minimal"
    if project["frontend"]:
        project["frontend"]["styling"] = styling
    if composition:
        for role in ("backend", "frontend"):
            if project[role]:
                for key in ("dependencies", "dev_dependencies"):
                    if key in project[role] or composition.get(role, {}).get(key):
                        project[role][key] = list(
                            dict.fromkeys(
                                [
                                    *project[role].get(key, []),
                                    *composition.get(role, {}).get(key, []),
                                ]
                            )
                        )
    handoff = project_handoff(project, (composition or {}).get("metadata"))
    starter_test_hashes = {
        path: hashlib.sha256(content.encode()).hexdigest()
        for path, content in files.items()
        if "/tests/" in path
        or any(marker in Path(path).name for marker in (".test.", ".spec."))
    }
    starter_source_hashes = {
        path: hashlib.sha256(content.encode()).hexdigest()
        for path, content in files.items()
        if (
            path.startswith("/workspace/frontend/src/")
            and path not in starter_test_hashes
        )
        or (
            path.startswith("/workspace/backend/")
            and path.endswith(".py")
            and path not in starter_test_hashes
        )
    }
    files["/workspace/.realopen-project.json"] = json.dumps(
        {
            **project,
            "handoff": handoff,
            "starter": {
                "schema_version": 1,
                "source_hashes": starter_source_hashes,
                "test_hashes": starter_test_hashes,
                **({"application_generated": True} if composition else {}),
            },
            **({"scaffold": composition["metadata"]} if composition else {}),
        },
        indent=2,
    )
    python_backend = backend != "none" and BACKENDS[backend].get("setup") == "python"
    # Check every destination before publishing; checked writes independently
    # protect against files appearing between this preflight and publication.
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
                        key: project["backend"][key]
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
        {
            "created_files": created,
            "project": project,
            "setup": checks,
            "handoff": handoff,
            **({"scaffold": composition["metadata"]} if composition else {}),
        },
        ensure_ascii=False,
    ), {
        "files": created,
        "project": project,
        "project_handoff": handoff,
        "starter_source_hashes": starter_source_hashes,
        "starter_test_hashes": starter_test_hashes,
        "changed": True,
        "command": "\n".join(item["command"] for item in checks),
        "exit_code": 1 if any(item.get("exit_code") != 0 for item in checks) else 0,
    }
