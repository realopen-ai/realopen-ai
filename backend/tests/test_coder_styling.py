"""Styling choices share a registry across plain and feature-based scaffolds."""

import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.agent.coder import starters, tooling
from app.agent.coder.scaffolding.composition import build
from app.agent.coder.scaffolding.spec import AppSpec
from app.agent.coder.styling import DEFAULT_STYLING, STYLING


def spec(**overrides):
    return AppSpec.model_validate(
        {
            "name": "Records",
            "backend": "fastapi",
            "frontend": "vite",
            "entities": [{"name": "records", "fields": [{"name": "title"}]}],
            **overrides,
        }
    )


@pytest.mark.parametrize("styling", STYLING)
@pytest.mark.parametrize("frontend", ["react-vite", "vite"])
def test_crud_styling_has_local_dependencies_proxy_and_usable_theme(styling, frontend):
    plan = build(spec(styling=styling, frontend=frontend))
    files = plan.files
    package = json.loads(files["frontend/package.json"])
    assert json.loads(files[".realopen-app.json"])["spec"]["styling"] == styling
    entrypoint = "frontend/" + starters.FRONTENDS[frontend]["entrypoint"]
    assert 'import "./theme.css";' in files[entrypoint]
    assert 'import "./style.css";' in files[entrypoint]
    assert "127.0.0.1:6969" in files["frontend/vite.config.ts"]
    assert "STYLING_" not in files["frontend/vite.config.ts"]
    assert "tailwindcss" in package["devDependencies"]
    assert "@tailwindcss/vite" in package["devDependencies"]
    assert "tailwindcss()" in files["frontend/vite.config.ts"]
    assert '@import "tailwindcss";' in files["frontend/src/theme.css"]
    assert "@layer base" in files["frontend/src/theme.css"]


def test_backend_only_does_not_gain_frontend_dependencies():
    plan = build(spec(frontend="none", styling="tailwind"))
    assert not any(path.startswith("frontend/") for path in plan.files)
    assert not plan.frontend["dependencies"] and not plan.frontend["dev_dependencies"]


def test_styling_is_validated_and_exposed_by_both_tools():
    with pytest.raises(ValidationError):
        spec(styling="invented")
    for tool in tooling.CODER_TOOLS:
        function = tool["function"]
        if function["name"] in {"scaffold_project", "scaffold_crud_app"}:
            assert function["parameters"]["properties"]["styling"]["enum"] == list(
                STYLING
            )
            assert (
                function["parameters"]["properties"]["styling"]["default"]
                == DEFAULT_STYLING
                == "tailwind"
            )


def test_new_scaffolds_only_add_supported_styling_dependencies():
    assert list(STYLING) == ["tailwind"]
    for frontend in starters.FRONTENDS:
        plan = build(spec(frontend=frontend))
        baseline = json.loads(
            starters.frontend_assets(frontend)["frontend/package.json"]
        )
        package = json.loads(plan.files["frontend/package.json"])
        assert package["dependencies"] == (
            {"react": "^19.3.0", "react-dom": "^19.3.0"}
            if frontend == "react-vite"
            else {}
        )
        assert package["devDependencies"] == {
            **baseline["devDependencies"],
            **STYLING[DEFAULT_STYLING]["dev_dependencies"],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize("styling", ["../tailwind", "unsupported"])
async def test_invalid_plain_styling_never_touches_the_workspace(monkeypatch, styling):
    async def forbidden(*args, **kwargs):
        raise AssertionError("Invalid styling must fail before host calls")

    monkeypatch.setattr(starters.sandbox_host, "call", forbidden)
    with pytest.raises(ValueError, match="styling"):
        await starters.scaffold(
            SimpleNamespace(),
            {
                "name": "test",
                "backend": "none",
                "frontend": "vite",
                "styling": styling,
            },
            forbidden,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("styling", STYLING)
@pytest.mark.parametrize("backend", ["fastapi", "none"])
@pytest.mark.parametrize("frontend", ["react-vite", "vite"])
async def test_plain_starter_styles_before_publication_and_remembers_choice(
    monkeypatch, styling, backend, frontend, tmp_path
):
    files = {}

    async def host(action, sandbox, payload=None, **kwargs):
        payload = payload or {}
        if action == "files/stat":
            return {"exists": False}
        if action == "usage":
            return {"usage_bytes": 0}
        if action == "files/write-checked":
            files[payload["path"]] = payload["content"]
            return {"written": len(payload["content"])}
        if action == "exec":
            return {"exit_code": 0}
        raise AssertionError(action)

    async def setup(*args):
        return "installed", {"exit_code": 0}

    monkeypatch.setattr(starters.sandbox_host, "call", host)
    output, event = await starters.scaffold(
        SimpleNamespace(workspace_quota_bytes=1_000_000),
        {
            "name": "Records",
            "backend": backend,
            "frontend": frontend,
            "styling": styling,
        },
        setup,
    )
    assert event["exit_code"] == 0
    assert json.loads(output)["project"]["frontend"]["styling"] == styling
    manifest = json.loads(files["/workspace/.realopen-project.json"])
    assert manifest["frontend"]["styling"] == styling
    assert "STYLING_" not in files["/workspace/frontend/vite.config.ts"]
    assert "tailwindcss()" in files["/workspace/frontend/vite.config.ts"]
    modules = os.environ.get("SCAFFOLD_TEST_NODE_MODULES")
    if modules:
        for path, content in files.items():
            target = tmp_path / path.removeprefix("/workspace/")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        (tmp_path / "frontend/node_modules").symlink_to(
            Path(modules), target_is_directory=True
        )
        for executable, args in (
            ("tsc", ["--noEmit"]),
            ("vitest", ["run"]),
            ("vite", ["build"]),
        ):
            result = subprocess.run(
                [str(Path(modules) / ".bin" / executable), *args],
                cwd=tmp_path / "frontend",
                capture_output=True,
                text=True,
                timeout=120,
            )
            assert result.returncode == 0, result.stdout + result.stderr
