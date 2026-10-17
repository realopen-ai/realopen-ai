"""Verification evidence and lean test starters, independent of a live model."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.coder import agent, tooling
from app.agent.coder.project import PROJECT_SNAPSHOT, project_snapshot
from app.agent.coder.starters import ASSETS, BACKENDS, FRONTENDS
from app.agent.coder.working_state import RunState, validation_categories
from app.agent.results import compact_result

ROOT = Path(__file__).resolve().parents[2]


def test_project_facts_are_current_not_starter_defaults(tmp_path):
    def snapshot():
        result = subprocess.run(
            [sys.executable, "-c", PROJECT_SNAPSHOT, str(tmp_path)],
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies=["fastapi"]\n[dependency-groups]\ndev=["pytest", "httpx2"]'
    )
    assert (
        "httpx2"
        in snapshot()["manifests"]["pyproject.toml"]["dependency_groups"]["dev"]
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies=["fastapi"]\n[dependency-groups]\ndev=["pytest", "httpx"]'
    )
    facts = snapshot()
    assert facts["manifests"]["pyproject.toml"]["dependency_groups"]["dev"] == [
        "pytest",
        "httpx",
    ]
    state = RunState(
        project={"backend": BACKENDS["fastapi"], "frontend": FRONTENDS["vite"]},
        project_facts=facts,
    )
    assert "httpx2" not in state.context()["content"]
    assert "starter_commands_not_current_dependencies" in state.context()["content"]
    state.record("install_dependencies", {}, "unavailable", {"project_facts": None})
    assert state.project_facts is None


def test_snapshot_reports_installed_node_version_and_malformed_manifests(tmp_path):
    package = tmp_path / "frontend"
    package.mkdir()
    (package / "package.json").write_text(
        json.dumps(
            {"dependencies": {"example": "^1"}, "scripts": {"test": "vitest run"}}
        )
    )
    installed = package / "node_modules" / "example"
    installed.mkdir(parents=True)
    (installed / "package.json").write_text('{"version":"1.2.3"}')
    (tmp_path / "pyproject.toml").write_text("invalid toml {")
    result = subprocess.run(
        [sys.executable, "-c", PROJECT_SNAPSHOT, str(tmp_path)],
        capture_output=True,
        text=True,
        check=True,
    )
    facts = json.loads(result.stdout)
    assert facts["installed_node"]["frontend"]["example"] == "1.2.3"
    assert "error" in facts["manifests"]["pyproject.toml"]


@pytest.mark.asyncio
async def test_failed_snapshot_never_invents_facts(monkeypatch):
    monkeypatch.setattr(
        tooling.sandbox_host, "call", AsyncMock(side_effect=RuntimeError("offline"))
    )
    assert await project_snapshot(object()) is None


@pytest.mark.parametrize(
    "command",
    [
        "echo pytest",
        "printf 'vitest run'",
        "python -c 'print(\"pytest\")'",
        "pytest; true",
        "pytest\ntrue",
        "pytest || true",
        "bun run build &",
        "ls build",
    ],
)
def test_non_checks_do_not_become_verification(command):
    assert validation_categories(command) == set()


def test_types_build_and_dom_evidence_use_actual_manifest_script():
    facts = {
        "manifests": {
            "frontend/package.json": {
                "scripts": {
                    "test": "vitest run",
                    "build": "tsc --noEmit && vite build",
                    "check": "tsc --noEmit",
                }
            }
        }
    }
    assert validation_categories("bun run --cwd frontend test", facts) == {
        "frontend_tests"
    }
    assert validation_categories("cd frontend && bun run build", facts) == {
        "typecheck",
        "build",
    }
    assert validation_categories("bun run --cwd frontend check", facts) == {"typecheck"}
    assert validation_categories(
        "uv run python -m pytest backend/tests -q | tail -20"
    ) == {"backend_tests"}
    assert validation_categories(
        "uv run python -m pytest backend/tests -q 2>&1 | tail -20"
    ) == {"backend_tests"}
    assert validation_categories("bun run --cwd frontend build; true", facts) == set()


@pytest.mark.asyncio
async def test_batched_checks_keep_all_exit_codes_and_do_not_abort_siblings(
    monkeypatch,
):
    events = []
    monkeypatch.setattr(tooling, "source_fingerprint", AsyncMock(return_value={}))

    async def host(action, sandbox, payload=None, **kwargs):
        if action == "usage":
            return {"usage_bytes": 0}
        assert action == "exec"
        code = 1 if payload["command"] == "pytest" else 0
        return {"exit_code": code, "stdout": "test output", "stderr": ""}

    monkeypatch.setattr(tooling.sandbox_host, "call", host)
    _, event = await tooling.dispatch_tool(
        SimpleNamespace(workspace_quota_bytes=1000),
        "run_checks",
        {"commands": ["pytest", "vitest run", "tsc --noEmit", "vite build"]},
        events.append,
    )
    assert event["exit_code"] == 1
    assert [check["exit_code"] for check in event["checks"]] == [1, 0, 0, 0]
    assert len(events) == 4
    state = RunState(files_changed=["source"])
    state.record("run_checks", {}, "checks", event)
    assert "backend_tests" not in state.validations
    assert {"frontend_tests", "typecheck", "build"} <= state.validations.keys()
    assert len(state.tests_run) == 4
    assert "Browser behavior was not verified" in state.verification_summary()


def test_failed_reruns_and_edits_remove_stale_success():
    state = RunState(files_changed=["source"])
    state.record("run_tests", {}, "passed", {"command": "pytest", "exit_code": 0})
    assert "backend_tests" in state.validations
    state.record("run_tests", {}, "failed", {"command": "pytest", "exit_code": 1})
    assert not state.successful_validation
    state.record("run_command", {}, "passed", {"command": "vitest run", "exit_code": 0})
    state.record("edit_file", {}, "changed", {"file": "app.ts", "changed": True})
    assert not state.validations
    assert all(
        check["status"] == "not_verified"
        for check in state.verification_report()["checks"].values()
    )


def test_generated_app_does_not_treat_untouched_starter_tests_as_behavior_evidence():
    state = RunState()
    state.record(
        "scaffold_project",
        {},
        "created",
        {
            "files": [
                "/workspace/frontend/src/main.ts",
                "/workspace/frontend/src/api.test.ts",
            ],
            "project": {"backend": None, "frontend": FRONTENDS["vite"]},
            "starter_test_hashes": {"/workspace/frontend/src/api.test.ts": "starter"},
            "changed": True,
        },
    )
    state.record(
        "edit_file",
        {},
        "implemented",
        {"file": "/workspace/frontend/src/app.ts", "changed": True},
    )
    state.record("run_tests", {}, "passed", {"command": "vitest run", "exit_code": 0})

    assert "frontend_tests" in state.validations
    assert "frontend_behavior_tests" not in state.validations
    assert "frontend application DOM tests" in state.completion_missing()

    state.record(
        "write_file",
        {},
        "app tests",
        {"file": "/workspace/frontend/src/app.test.ts", "changed": True},
    )
    state.record("run_tests", {}, "passed", {"command": "vitest run", "exit_code": 0})
    assert "frontend_behavior_tests" in state.validations


def test_generated_app_test_scope_survives_later_coder_executions():
    state = RunState(
        project={"backend": BACKENDS["fastapi"], "frontend": FRONTENDS["vite"]}
    )
    starter = {
        "schema_version": 1,
        "source_hashes": {
            "/workspace/backend/app.py": "starter-backend",
            "/workspace/frontend/src/main.ts": "starter-frontend",
        },
        "test_hashes": {
            "/workspace/backend/tests/test_api.py": "starter-test",
            "/workspace/frontend/src/api.test.ts": "starter-test",
        },
    }
    state.restore_scaffold_metadata(
        starter,
        {
            "/workspace/backend/app.py": "implemented-backend",
            "/workspace/frontend/src/main.ts": "starter-frontend",
            "/workspace/frontend/src/app.ts": "implemented-frontend",
            "/workspace/backend/tests/test_api.py": "starter-test",
            "/workspace/frontend/src/api.test.ts": "starter-test",
            "/workspace/frontend/src/app.test.ts": "application-test",
        },
    )

    assert state.scaffolded_roles == {"backend", "frontend"}
    assert state.application_roles_changed == {"backend", "frontend"}
    assert state.application_test_roles_changed == {"frontend"}


@pytest.mark.asyncio
async def test_start_preview_is_persisted_for_future_sandbox_restarts(monkeypatch):
    sandbox = SimpleNamespace(id="sandbox-id", preview_commands={})
    monkeypatch.setattr(
        tooling.sandbox_host,
        "call",
        AsyncMock(return_value={"command_id": "preview-6767", "started": True}),
    )
    remember = AsyncMock()
    monkeypatch.setattr(tooling, "remember_preview_command", remember)

    output, event = await tooling.dispatch_tool(
        sandbox,
        "start_preview",
        {"command": "bun run --cwd frontend dev", "port": 6767},
    )

    remember.assert_awaited_once_with(sandbox, 6767, "bun run --cwd frontend dev")
    assert json.loads(output)["persistent"] is True
    assert event["port"] == 6767


def test_general_agent_receives_verification_even_when_coder_logs_are_compacted():
    report = RunState().verification_report()
    output = json.dumps(
        {
            "task_id": "task-123",
            "summary": "summary" * 2000,
            "commands": ["verbose log" * 4000],
            "verification": report,
        }
    )
    view = compact_result("delegate_to_coder", output, result_ref="result-123")
    data = json.loads(view.content)
    assert view.shortened and data["verification"] == report
    assert data["metadata"]["task_id"] == "task-123"
    assert len(view.content) < 6000


@pytest.mark.asyncio
async def test_browser_evidence_requires_actual_successful_receipt(monkeypatch):
    result = {"exit_code": 1, "stdout": "", "stderr": "not installed"}
    monkeypatch.setattr(tooling, "source_fingerprint", AsyncMock(return_value={}))

    async def host(action, sandbox, payload=None, **kwargs):
        return {"usage_bytes": 0} if action == "usage" else result

    monkeypatch.setattr(tooling.sandbox_host, "call", host)
    sandbox = SimpleNamespace(workspace_quota_bytes=10000)
    args = {"paths": ["tests/smoke.mjs"], "port": 6767}
    state = RunState()
    output, event = await tooling.dispatch_tool(sandbox, "run_browser_tests", args)
    state.record("run_browser_tests", args, output, event)
    assert "browser" not in state.validations
    receipt = {
        "workers": 1,
        "results": [{"file": "/workspace/tests/smoke.mjs", "status": "passed"}],
    }
    result.update(exit_code=0, stdout="ROAI_BROWSER_RESULT=" + json.dumps(receipt))
    output, event = await tooling.dispatch_tool(sandbox, "run_browser_tests", args)
    state.record("run_browser_tests", args, output, event)
    assert "browser" in state.validations
    assert "real-browser tests" in state.verification_summary()
    result.update(exit_code=1, stdout="", stderr="failed")
    output, event = await tooling.dispatch_tool(sandbox, "run_browser_tests", args)
    state.record("run_browser_tests", args, output, event)
    assert "browser" not in state.validations


def test_browser_runner_never_downloads_and_cleans_up():
    code = (ROOT / "sandbox/helpers/roai-browser-test").read_text()
    assert "install(" not in code and "child_process" not in code
    assert "finally {\n    await browser.close();" in code
    assert "workers: 1" in code and "SIGTERM" in code
    dockerfile = (ROOT / "sandbox/Dockerfile").read_text()
    assert "ARG INSTALL_BROWSER_TESTS=false" in dockerfile
    assert "--only-shell chromium" in dockerfile


@pytest.mark.parametrize(
    "name,args",
    [
        ("run_checks", {}),
        ("run_checks", {"commands": None}),
        ("run_checks", {"commands": [123]}),
        ("run_browser_tests", {"paths": 123}),
    ],
)
def test_invalid_verification_arguments_can_be_shown_without_crashing(name, args):
    queue = asyncio.Queue()
    agent._rejected_step(
        RunState(), name, args, "Invalid arguments", queue, "parent", "task", "sandbox"
    )
    event = queue.get_nowait()
    assert event["status"] == "error" and event["error"] == "Invalid arguments"


@pytest.mark.asyncio
async def test_batch_updates_are_live_and_reported(monkeypatch):
    async def dispatch(sandbox, name, args, progress):
        progress(
            {
                "stage": "check_completed",
                "index": 0,
                "check": {"command": "pytest", "exit_code": 1, "output": "failed"},
            }
        )
        await asyncio.sleep(0)
        return "finished", {"command": "pytest", "exit_code": 1}

    monkeypatch.setattr(agent, "dispatch_tool", dispatch)
    monkeypatch.setattr(agent, "_record_command", AsyncMock())
    queue = asyncio.Queue()
    await agent._run_step(
        sandbox=SimpleNamespace(id="sandbox"),
        conversation=SimpleNamespace(id="conversation"),
        task_id="task",
        name="run_checks",
        args={"commands": ["pytest"]},
        state=RunState(),
        parent_id="parent",
        event_queue=queue,
    )
    updates = [queue.get_nowait() for _ in range(queue.qsize())]
    assert updates[1]["status"] == "running" and "failed" in updates[1]["output"]
    assert updates[-1]["status"] == "error"


def test_starter_backend_fixture_preserves_real_database_and_isolates_tests(tmp_path):
    shutil.copytree(ASSETS / "fastapi", tmp_path, dirs_exist_ok=True)
    database = tmp_path / "backend/app.sqlite3"
    database.write_bytes(b"existing user data must remain untouched")
    (tmp_path / "backend/app.py").write_text(
        """
from fastapi import FastAPI
from backend.db import connection
with connection() as db:
    db.execute("create table import_time_data(value text)")
app = FastAPI()
@app.get("/api/health")
def health():
    return {"status": "ok"}
"""
    )
    (tmp_path / "backend/tests/test_isolation.py").write_text(
        """
from backend import db
def test_first(isolated_database):
    assert db.DATABASE.name == "test.sqlite3"
    assert not db.DATABASE.exists()
    with db.connection() as connection:
        connection.execute("create table example(value text)")
        connection.execute("insert into example values ('persisted')")
    with db.connection() as connection:
        assert connection.execute("select value from example").fetchone()[0] == "persisted"
def test_second(isolated_database):
    assert not db.DATABASE.exists()
"""
    )
    env = {
        **os.environ,
        "PYTHONPATH": str(tmp_path),
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "backend/tests", "-q"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert database.read_bytes() == b"existing user data must remain untouched"
