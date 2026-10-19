"""Focused scaffold navigation, real API contracts and staged verification."""

import copy
import json
import os
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.coder import starters, tooling
from app.agent.coder.context import tokens
from app.agent.coder.handoff import (
    agent_scaffold_result,
    bounded_contract,
    openapi_contract,
    project_handoff,
)
from app.agent.coder.scaffolding.composition import build
from app.agent.coder.scaffolding.spec import AppSpec
from app.agent.coder.working_state import RunState


def project(backend="fastapi", frontend="react-vite"):
    return {
        "backend": starters.BACKENDS[backend] if backend != "none" else None,
        "frontend": starters.FRONTENDS[frontend] if frontend != "none" else None,
    }


def document():
    return {
        "openapi": "3.1.0",
        "paths": {
            "/api/posts": {
                "parameters": [
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {"type": "integer", "maximum": 100},
                    }
                ],
                "post": {
                    "description": "Long documentation " * 300,
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/Post"}
                            }
                        },
                    },
                    "responses": {
                        "201": {
                            "description": "Created",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Post"}
                                }
                            },
                        },
                        "422": {"description": "Invalid"},
                    },
                },
            },
            "/api/posts/{post_id}": {
                "delete": {
                    "parameters": [
                        {
                            "name": "post_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "integer"},
                        }
                    ],
                    "responses": {"204": {"description": "Deleted"}},
                }
            },
            "/api/postscript": {
                "get": {"responses": {"200": {"description": "Other"}}}
            },
        },
        "components": {
            "schemas": {
                "Post": {
                    "type": "object",
                    "required": ["description"],
                    "properties": {
                        "description": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Help",
                        },
                        "examples": {"type": "boolean", "default": False},
                        "quantity": {"type": "integer", "default": 0},
                        "parent": {
                            "anyOf": [
                                {"$ref": "#/components/schemas/Post"},
                                {"type": "null"},
                            ]
                        },
                        "metadata": {
                            "type": "object",
                            "default": {"description": "Literal data"},
                        },
                    },
                },
                "Unrelated": {"description": "Must not be injected"},
            }
        },
    }


@pytest.mark.parametrize(
    "backend,frontend",
    [
        ("fastapi", "react-vite"),
        ("fastapi", "vite"),
        ("fastapi", "none"),
        ("none", "react-vite"),
        ("none", "vite"),
    ],
)
def test_compact_map_matches_requested_layers_and_is_not_verification(
    backend, frontend
):
    handoff = project_handoff(
        project(backend, frontend),
        {"generator": "crud", "resources": ["posts", "stories"]},
    )
    assert handoff["resources"] == ["posts", "stories"]
    assert handoff["application_generated"]
    assert "Run generated checks first" in handoff["next_step"]
    assert ("api" in handoff["edit_targets"]) == (backend != "none")
    assert ("ui" in handoff["edit_targets"]) == (frontend != "none")
    assert "source" not in handoff and "passed" not in handoff
    if backend != "none":
        assert [item["path"] for item in handoff["api_contract"]["resources"]] == [
            "/api/posts",
            "/api/stories",
        ]
    else:
        assert handoff["persistence"] == "Browser localStorage"
    if frontend != "none":
        assert handoff["frontend_entrypoint"].endswith(
            ".tsx" if frontend == "react-vite" else ".ts"
        )
    starter = project_handoff(project(backend, frontend))
    assert not starter["application_generated"]
    assert "still needs implementation" in starter["implemented"][0]


@pytest.mark.asyncio
async def test_scaffold_returns_and_persists_map_without_dumping_source(monkeypatch):
    files = {}

    async def host(action, sandbox, payload=None, **kwargs):
        if action == "files/stat":
            return {"exists": False}
        if action == "usage":
            return {"usage_bytes": 0}
        if action == "files/write-checked":
            files[payload["path"]] = payload["content"]
            return {}
        if action == "exec":
            return {"exit_code": 0}
        raise AssertionError(action)

    monkeypatch.setattr(starters.sandbox_host, "call", host)
    spec = AppSpec.model_validate(
        {
            "name": "Publishing",
            "entities": [
                {"name": name, "fields": [{"name": "title"}]}
                for name in ("posts", "stories")
            ],
        }
    )
    plan = build(spec)
    output, event = await starters.scaffold(
        SimpleNamespace(workspace_quota_bytes=10_000_000),
        spec.model_dump(),
        AsyncMock(return_value=("installed", {"exit_code": 0})),
        generated=plan.files,
        composition=plan.manifest(),
    )
    handoff = event["project_handoff"]
    assert json.loads(files["/workspace/.realopen-project.json"])["handoff"] == handoff
    assert json.loads(output)["handoff"] == handoff
    brief = json.loads(agent_scaffold_result(output, event))
    assert brief["handoff"] == handoff
    assert brief["created_file_count"] == len(json.loads(output)["created_files"])
    assert "created_files" not in brief and "files" not in brief
    state = RunState()
    state.record("scaffold_crud_app", spec.model_dump(), output, event)
    assert state.memory.checkpoint()["project_handoff"] == handoff
    assert "Verify generated CRUD" in state.workflow_context()["next_focus"]
    assert not state.validations


def test_dependency_failure_details_are_not_lost_in_compact_scaffold_result():
    output = json.dumps(
        {
            "created_files": ["a.py"],
            "setup": [
                {
                    "action": "install",
                    "command": "uv sync",
                    "exit_code": 1,
                    "result": "Package resolution failed",
                }
            ],
        }
    )
    result = json.loads(
        agent_scaffold_result(
            output, {"project_handoff": {"application_generated": True}}
        )
    )
    assert (
        "Package resolution failed" in result["setup"][0]["failure_details"]["result"]
    )


def test_api_contract_preserves_wire_semantics_and_resolves_cycles_without_prose():
    data = document()
    original = copy.deepcopy(data)
    contract = openapi_contract(data, "/api/posts")
    assert data == original
    assert [(op["method"], op["path"]) for op in contract["operations"]] == [
        ("POST", "/api/posts"),
        ("DELETE", "/api/posts/{post_id}"),
    ]
    definition = contract["definitions"]["#/components/schemas/Post"]
    assert definition["required"] == ["description"]
    assert definition["properties"]["description"] == {"type": "string", "minLength": 1}
    assert definition["properties"]["examples"]["default"] is False
    assert definition["properties"]["quantity"]["default"] == 0
    assert definition["properties"]["metadata"]["default"] == {
        "description": "Literal data"
    }
    assert definition["properties"]["parent"]["anyOf"][1] == {"type": "null"}
    assert len(contract["definitions"]) == 1
    assert contract["operations"][0]["parameters"][0]["schema"]["maximum"] == 100
    assert "201" in contract["operations"][0]["responses"]
    assert contract["operations"][1]["responses"]["204"] == {}
    assert "Long documentation" not in json.dumps(contract)


def test_contract_budget_reports_omissions_without_corrupting_json_or_source():
    contract = openapi_contract(document())
    original = copy.deepcopy(contract)
    reduced = bounded_contract(contract, 150, tokens)
    assert tokens(reduced) <= 150
    assert "notice" in reduced
    assert json.loads(json.dumps(reduced)) == reduced
    assert contract == original


@pytest.mark.asyncio
async def test_openapi_preview_uses_live_schema_and_keeps_full_output(monkeypatch):
    raw = json.dumps(document())
    host = AsyncMock(return_value={"stdout": raw, "stderr": "", "exit_code": 0})
    monkeypatch.setattr(tooling.sandbox_host, "call", host)
    output, event = await tooling.dispatch_tool(
        object(),
        "check_preview",
        {"port": 6969, "path": "/openapi.json", "api_path": "/api/posts"},
    )
    assert json.loads(output)["stdout"] == raw
    assert event["api_contract"] == openapi_contract(document(), "/api/posts")
    assert "127.0.0.1:6969/openapi.json" in host.call_args.args[2]["command"]
    state = RunState(project=project("fastapi", "none"))
    state.record(
        "check_preview", {"port": 6969, "path": "/openapi.json"}, output, event
    )
    assert state.memory.api_contract == event["api_contract"]
    assert (
        "integration" not in state.validations
    )  # Documentation is not a behavior smoke test.


@pytest.mark.asyncio
async def test_invalid_contract_is_a_visible_failure_not_an_invented_contract(
    monkeypatch,
):
    monkeypatch.setattr(
        tooling.sandbox_host,
        "call",
        AsyncMock(return_value={"stdout": "<html>not API docs</html>", "exit_code": 0}),
    )
    output, event = await tooling.dispatch_tool(
        object(), "check_preview", {"port": 6969, "path": "/openapi.json"}
    )
    assert event["exit_code"] == 1 and "error" in json.loads(output)
    assert not event.get("api_contract")
    state = RunState()
    state.memory.api_contract = openapi_contract(document())
    state.record(
        "check_preview", {"path": "/openapi.json", "port": 6969}, output, event
    )
    assert state.memory.api_contract is None


def test_local_frontend_edits_retain_backend_evidence_but_shared_edits_do_not():
    state = RunState(project=project())
    state.memory.api_contract = openapi_contract(document())
    for command in ("pytest", "vitest run", "tsc --noEmit", "vite build"):
        state.record("run_tests", {}, "passed", {"command": command, "exit_code": 0})
    state.validations["integration"] = {"revision": state.revision}
    state.record(
        "edit_file",
        {},
        "changed",
        {"file": "/workspace/frontend/src/App.tsx", "changed": True},
    )
    assert "backend_tests" in state.validations and state.memory.api_contract
    assert not (
        {"frontend_tests", "typecheck", "build", "integration"}
        & state.validations.keys()
    )
    assert "Backend tested" in state.workflow_context()["next_focus"]
    state.record(
        "edit_file",
        {},
        "changed",
        {"file": "/workspace/pyproject.toml", "changed": True},
    )
    assert not state.validations and state.memory.api_contract is None


def test_backend_edits_invalidate_contract_but_preserve_frontend_test_evidence():
    state = RunState()
    state.memory.api_contract = openapi_contract(document())
    state.record("run_tests", {}, "passed", {"command": "vitest run", "exit_code": 0})
    state.record(
        "edit_file",
        {},
        "changed",
        {"file": "/workspace/backend/resources/posts.py", "changed": True},
    )
    assert "frontend_tests" in state.validations
    assert state.memory.api_contract is None


def test_exploration_reminder_is_soft_and_resets_after_checks_or_changes():
    state = RunState()
    for index in range(9):
        assert (
            state.record(
                "read_file", {"path": f"/workspace/{index}.py"}, f"source {index}", {}
            )
            == ""
        )
    assert state.workflow_context()["exploration_reminder"]
    assert (
        state.record(
            "run_tests", {}, "failed assertion", {"command": "pytest", "exit_code": 1}
        )
        == ""
    )
    assert not state.workflow_context()["exploration_reminder"]
    for index in range(6):
        state.record("search_files", {"query": f"symbol{index}"}, "match", {})
    assert state.exploration_steps == 6
    state.record(
        "edit_file", {}, "changed", {"file": "/workspace/main.py", "changed": True}
    )
    assert state.exploration_steps == 0
    for index in range(6):
        state.record(
            "run_command",
            {"command": f"head /workspace/file{index}.py"},
            "read",
            {"command": f"head /workspace/file{index}.py", "exit_code": 0},
        )
    assert state.workflow_context()["exploration_reminder"]


def test_actual_generated_openapi_is_consumable_without_importing_code_in_backend_process(
    tmp_path,
):
    spec = AppSpec.model_validate(
        {
            "name": "Publishing",
            "frontend": "none",
            "entities": [
                {
                    "name": name,
                    "fields": [
                        {"name": "title"},
                        {"name": "published", "type": "boolean", "default": False},
                    ],
                }
                for name in ("posts", "stories")
            ],
        }
    )
    plan = build(spec)
    files = starters.read_assets("fastapi") | plan.files
    for path, content in files.items():
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import json; from backend.app import app; print(json.dumps(app.openapi()))",
        ],
        cwd=tmp_path,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    contract = openapi_contract(json.loads(result.stdout), "/api/posts")
    assert {op["method"] for op in contract["operations"]} == {
        "GET",
        "POST",
        "PATCH",
        "DELETE",
    }
    assert len(contract["operations"]) == 5
    assert all(op["path"].startswith("/api/posts") for op in contract["operations"])
    assert any(
        schema.get("properties", {}).get("published", {}).get("default") is False
        for schema in contract["definitions"].values()
    )
