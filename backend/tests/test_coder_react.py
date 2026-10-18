"""React is the default for new apps; explicit vanilla and existing work survive."""

import json
from types import SimpleNamespace

import pytest

from app.agent.coder import starters, tooling
from app.agent.coder.scaffolding.composition import build
from app.agent.coder.scaffolding.spec import AppSpec


def test_crud_defaults_to_editable_react_components_and_shared_transport():
    spec = AppSpec.model_validate(
        {
            "name": "Directory",
            "entities": [{"name": "contacts", "fields": [{"name": "name"}]}],
        }
    )
    assert spec.frontend == "react-vite"
    assert spec.styling == "tailwind"
    plan = build(spec)
    for path in (
        "App.tsx",
        "App.test.tsx",
        "main.tsx",
        "components/RecordForm.tsx",
        "components/RecordList.tsx",
        "components/Filters.tsx",
        "hooks/useCrud.ts",
        "data.ts",
        "types.ts",
    ):
        assert "frontend/src/" + path in plan.files
    for obsolete in ("main.ts", "crud.ts", "status.ts", "status.test.ts"):
        assert "frontend/src/" + obsolete not in plan.files
    package = json.loads(plan.files["frontend/package.json"])
    assert "react" in package["dependencies"] and "react-dom" in package["dependencies"]
    assert "tailwindcss" in package["devDependencies"]
    assert "@testing-library/react" in package["devDependencies"]
    assert (
        json.loads(plan.files["frontend/tsconfig.json"])["compilerOptions"]["jsx"]
        == "react-jsx"
    )
    assert "react()" in plan.files["frontend/vite.config.ts"]
    assert "createElement" not in plan.files["frontend/src/App.tsx"]
    assert "createRoot" in plan.files["frontend/src/main.tsx"]


def test_explicit_vanilla_still_generates_vanilla_without_react_dependencies():
    spec = AppSpec.model_validate(
        {
            "name": "Directory",
            "frontend": "vite",
            "entities": [{"name": "contacts", "fields": [{"name": "name"}]}],
        }
    )
    plan = build(spec)
    assert "frontend/src/crud.ts" in plan.files
    assert "frontend/src/main.tsx" not in plan.files
    assert (
        "react" not in json.loads(plan.files["frontend/package.json"])["dependencies"]
    )


def test_react_tool_choice_is_explicit_and_defaults_consistently():
    for tool in tooling.CODER_TOOLS:
        if tool["function"]["name"] in {"scaffold_project", "scaffold_crud_app"}:
            schema = tool["function"]["parameters"]["properties"]["frontend"]
            assert schema["default"] == "react-vite"
            assert {"react-vite", "vite", "none"} <= set(schema["enum"])


@pytest.mark.asyncio
async def test_plain_default_is_react_and_refuses_to_overwrite_existing_work(
    monkeypatch,
):
    files = {}

    async def host(action, sandbox, payload=None, **kwargs):
        payload = payload or {}
        if action == "files/stat":
            return {"exists": payload["path"] in files}
        if action == "usage":
            return {"usage_bytes": 0}
        if action == "exec":
            return {"exit_code": 0}
        if action == "files/write-checked":
            assert payload["path"] not in files
            files[payload["path"]] = payload["content"]
            return {"written": len(payload["content"])}
        raise AssertionError(action)

    async def setup(*args):
        raise AssertionError("No Python for frontend-only")

    monkeypatch.setattr(starters.sandbox_host, "call", host)
    sandbox = SimpleNamespace(workspace_quota_bytes=1_000_000)
    output, event = await starters.scaffold(
        sandbox, {"name": "Directory", "backend": "none"}, setup
    )
    assert event["exit_code"] == 0
    assert json.loads(output)["project"]["frontend"]["framework"] == "react"
    assert json.loads(output)["project"]["frontend"]["styling"] == "tailwind"
    assert "tailwindcss()" in files["/workspace/frontend/vite.config.ts"]
    assert "connected={false}" in files["/workspace/frontend/src/main.tsx"]
    assert "/workspace/frontend/src/main.ts" not in files
    assert "jsx" in files["/workspace/frontend/tsconfig.json"]
    files["/workspace/frontend/src/App.tsx"] = "CUSTOMIZED REACT APP"
    before = dict(files)
    with pytest.raises(ValueError, match="overwrite"):
        await starters.scaffold(
            sandbox, {"name": "Directory", "backend": "none"}, setup
        )
    assert files == before
