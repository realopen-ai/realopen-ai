"""Coder safety and deterministic progress; no model or Docker required."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.coder import agent, starters, tooling
from app.agent.coder.working_state import RunState, compact_output, validation_kind
from app.services import skills
from app.services import agent_settings, context_compactor


def sha(content):
    return hashlib.sha256(content.encode()).hexdigest()


@pytest.fixture
def workspace(monkeypatch):
    files = {}
    calls = []

    async def call(action, sandbox, payload=None, **kwargs):
        payload = payload or {}
        calls.append((action, payload))
        path = payload.get("path")
        if action == "files/stat":
            return {"exists": path in files, "size": len(files.get(path, "").encode())}
        if action == "files/read":
            return {"content": files[path]}
        if action == "files/write-checked":
            actual = sha(files[path]) if path in files else "missing"
            if actual != payload["expected_sha256"]:
                raise RuntimeError("409: File changed")
            files[path] = payload["content"]
            return {"written": len(files[path])}
        if action == "usage":
            return {"usage_bytes": sum(len(value.encode()) for value in files.values())}
        if action == "exec":
            return {"exit_code": 0, "stdout": "passed", "stderr": ""}
        raise AssertionError(action)

    monkeypatch.setattr(tooling.sandbox_host, "call", call)
    return SimpleNamespace(workspace_quota_bytes=1_000_000), files, calls


@pytest.mark.asyncio
async def test_exact_paginated_reads_require_complete_coverage_for_replacement(
    workspace,
):
    sandbox, files, _ = workspace
    path = "/workspace/large.txt"
    files[path] = "a" * 18000
    state = RunState()
    for offset in [0, 12000, 6000]:
        output, event = await tooling.dispatch_tool(
            sandbox, "read_file", {"path": path, "offset": offset}
        )
        data = json.loads(output)
        assert data["content"] == files[path][offset : offset + 6000]
        assert data["sha256"] == sha(files[path])
        state.record("read_file", {"path": path}, output, event)
        assert state.read_arguments(path)["_full_read"] == (offset == 6000)
    files[path] += "changed"
    _, event = await tooling.dispatch_tool(sandbox, "read_file", {"path": path})
    state.record("read_file", {"path": path}, "read", event)
    assert not state.read_arguments(path)["_full_read"]


@pytest.mark.asyncio
async def test_edit_and_replacement_require_read_and_reject_stale_or_ambiguous_text(
    workspace,
):
    sandbox, files, _ = workspace
    path = "/workspace/main.ts"
    files[path] = "const a = 1;\nconst b = 1;\n"
    args = {
        "path": path,
        "old_text": "const a = 1;",
        "new_text": "const a = 2;",
        "_read_ranges": [(0, len(files[path]))],
    }
    output, event = await tooling.dispatch_tool(sandbox, "edit_file", args)
    assert event["exit_code"] == 1 and "Read this file" in output
    digest = sha(files[path])
    _, event = await tooling.dispatch_tool(
        sandbox, "edit_file", {**args, "old_text": " = 1;", "_expected_sha256": digest}
    )
    assert event["exit_code"] == 1
    original = files[path]
    _, event = await tooling.dispatch_tool(
        sandbox,
        "write_file",
        {
            "path": path,
            "content": "replacement",
            "_expected_sha256": digest,
            "_full_read": False,
        },
    )
    assert event["exit_code"] == 1 and files[path] == original
    _, event = await tooling.dispatch_tool(
        sandbox, "edit_file", {**args, "_expected_sha256": digest}
    )
    assert event["changed"] and files[path] == "const a = 2;\nconst b = 1;\n"
    after = files[path]
    _, event = await tooling.dispatch_tool(
        sandbox, "edit_file", {**args, "_expected_sha256": digest}
    )
    assert event["exit_code"] == 1 and files[path] == after


@pytest.mark.asyncio
async def test_identical_write_does_not_publish_or_invalidate_validation(workspace):
    sandbox, files, calls = workspace
    path = "/workspace/a.txt"
    files[path] = "unchanged"
    _, event = await tooling.dispatch_tool(
        sandbox,
        "write_file",
        {
            "path": path,
            "content": files[path],
            "_expected_sha256": sha(files[path]),
            "_full_read": True,
        },
    )
    assert event == {"changed": False}
    assert not any(action == "files/write-checked" for action, _ in calls)


@pytest.mark.asyncio
async def test_starter_has_local_styling_sqlite_tests_and_connected_proxy(workspace):
    sandbox, files, calls = workspace
    output, event = await tooling.dispatch_tool(
        sandbox,
        "scaffold_project",
        {"name": "test-app", "backend": "fastapi", "frontend": "vite"},
    )
    assert event["exit_code"] == 0
    project = json.loads(output)["project"]
    assert project["backend"]["setup"] == "python"
    assert "sqlite3" in files["/workspace/backend/db.py"]
    assert "TestClient" in files["/workspace/backend/tests/test_api.py"]
    assert "@picocss/pico" in files["/workspace/frontend/package.json"]
    assert 'types="vite/client"' in files["/workspace/frontend/src/vite-env.d.ts"]
    assert '"/api"' in files["/workspace/frontend/vite.config.ts"]
    assert "6969" in files["/workspace/frontend/vite.config.ts"]
    assert not any("cdn." in value for value in files.values())
    assert {payload["command"] for action, payload in calls if action == "exec"} == {
        "uv sync --all-groups",
        "bun install --cwd frontend",
    }
    snapshot = dict(files)
    with pytest.raises(ValueError, match="overwrite"):
        await tooling.dispatch_tool(
            sandbox,
            "scaffold_project",
            {"name": "test-app", "backend": "fastapi", "frontend": "vite"},
        )
    assert files == snapshot


@pytest.mark.asyncio
async def test_frontend_only_starter_needs_no_api_or_python(workspace):
    sandbox, files, _ = workspace
    await tooling.dispatch_tool(
        sandbox,
        "scaffold_project",
        {"name": "ui", "backend": "none", "frontend": "vite"},
    )
    assert "/workspace/pyproject.toml" not in files
    assert "Ready" in files["/workspace/frontend/src/main.ts"]
    assert "/api/health" not in files["/workspace/frontend/src/main.ts"]


@pytest.mark.asyncio
async def test_starter_dependency_failure_retains_and_reports_created_files(
    workspace, monkeypatch
):
    sandbox, files, _ = workspace
    call = tooling.sandbox_host.call

    async def unavailable(action, *args, **kwargs):
        if action == "exec":
            raise RuntimeError("package registry unavailable")
        return await call(action, *args, **kwargs)

    monkeypatch.setattr(tooling.sandbox_host, "call", unavailable)
    output, event = await tooling.dispatch_tool(
        sandbox,
        "scaffold_project",
        {"name": "ui", "backend": "none", "frontend": "vite"},
    )
    assert event["exit_code"] == 1 and event["changed"]
    assert set(event["files"]) == set(files)
    assert "package registry unavailable" in output


@pytest.mark.asyncio
async def test_quota_rejects_starter_before_any_publication(workspace):
    sandbox, files, _ = workspace
    sandbox.workspace_quota_bytes = 1
    with pytest.raises(ValueError, match="quota"):
        await tooling.dispatch_tool(
            sandbox,
            "scaffold_project",
            {"name": "ui", "backend": "none", "frontend": "vite"},
        )
    assert not files


def test_progress_allows_more_than_two_real_edits_but_stops_identical_results():
    state = RunState()
    for value in range(4):
        assert not state.record(
            "write_file",
            {"path": "app.py", "content": f"n = {value}"},
            "written",
            {"file": "app.py", "changed": True},
        )
    assert state.revision == 4
    for count in range(3):
        reason = state.record(
            "run_tests",
            {"command": "pytest"},
            "same failure",
            {"command": "pytest", "exit_code": 1},
        )
        assert bool(reason) == (count == 2)


@pytest.mark.asyncio
async def test_shell_source_changes_invalidate_prior_checks(workspace, monkeypatch):
    sandbox, files, _ = workspace
    path = "/workspace/app.py"
    files[path] = "value = 1\n"
    snapshots = iter([{path: sha(files[path])}, {path: sha("value = 2\n")}])
    monkeypatch.setattr(
        tooling, "source_fingerprint", AsyncMock(side_effect=lambda _: next(snapshots))
    )
    state = RunState(validations={"tests": {"revision": 0}}, successful_validation=True)
    output, event = await tooling.dispatch_tool(
        sandbox, "run_command", {"command": "format-project"}
    )
    assert event["changed"] and event["files"] == [path]
    state.record("run_command", {}, output, event)
    assert not state.validations and not state.successful_validation


@pytest.mark.asyncio
async def test_shell_with_unknown_file_changes_cannot_claim_verified_tests(
    workspace, monkeypatch
):
    sandbox, _, _ = workspace
    monkeypatch.setattr(tooling, "source_fingerprint", AsyncMock(return_value=None))
    state = RunState()
    output, event = await tooling.dispatch_tool(
        sandbox, "run_tests", {"command": "pytest"}
    )
    state.record("run_tests", {}, output, event)
    assert event["changes_unknown"] and not state.successful_validation


@pytest.mark.asyncio
async def test_edit_must_target_text_actually_read(workspace):
    sandbox, files, _ = workspace
    path = "/workspace/a.txt"
    files[path] = "visible\nhidden"
    _, event = await tooling.dispatch_tool(
        sandbox,
        "edit_file",
        {
            "path": path,
            "old_text": "hidden",
            "new_text": "gone",
            "_expected_sha256": sha(files[path]),
            "_read_ranges": [(0, 7)],
        },
    )
    assert event["exit_code"] == 1 and files[path] == "visible\nhidden"


def test_full_stack_validation_requires_latest_build_tests_and_proxy_request():
    state = RunState()
    state.record(
        "scaffold_project",
        {},
        "created",
        {
            "files": ["app.py"],
            "project": {"backend": {}, "frontend": {}},
            "changed": True,
        },
    )
    # Real registry entries are nonempty.
    state.project = {
        "backend": starters.BACKENDS["fastapi"],
        "frontend": starters.FRONTENDS["vite"],
    }
    state.record("run_tests", {}, "passed", {"command": "pytest", "exit_code": 0})
    state.record(
        "run_command", {}, "built", {"command": "bun run build", "exit_code": 0}
    )
    for port in [6767, 6969]:
        state.record(
            "start_preview", {}, "started", {"preview_url": "local", "port": port}
        )
    state.record(
        "check_preview", {"port": 6969, "path": "/api/health"}, "ok", {"exit_code": 0}
    )
    assert state.completion_missing() == ["live preview smoke check"]
    state.record("check_preview", {"port": 6767, "path": "/"}, "html", {"exit_code": 0})
    assert state.completion_missing()
    state.record(
        "check_preview", {"port": 6767, "path": "/api/health"}, "ok", {"exit_code": 0}
    )
    assert state.completion_missing() == []
    state.record("edit_file", {}, "edited", {"file": "app.py", "changed": True})
    assert len(state.completion_missing()) == 3
    context = state.context()
    assert (
        context["metadata"]["agent_coder_state"]
        and context["metadata"]["agent_context"]
    )
    assert "app.py" in context["content"]


def test_compact_outputs_are_bounded_and_retain_command_errors():
    output = json.dumps(
        {
            "exit_code": 1,
            "stderr": "first error" + "x" * 30000 + "last error",
            "stdout": "x" * 30000,
        }
    )
    compact = compact_output(output)
    assert len(compact) < 6000
    data = json.loads(compact)
    assert data["exit_code"] == 1
    assert "first error" in data["stderr"] and "last error" in data["stderr"]
    assert len(compact_output(json.dumps({"items": list(range(20000))}))) < 6100


@pytest.mark.parametrize(
    "command,expected",
    [
        ("uv run python -m pytest backend/tests -q", "tests"),
        ("bun run --cwd frontend build", "build"),
        ("python -m py_compile main.py", "syntax"),
        ("echo build", None),
        ("pytest || true", None),
        ("ls build", None),
    ],
)
def test_checks_are_classified_by_command_not_tool_name(command, expected):
    assert validation_kind("run_command", command) == expected


@pytest.mark.asyncio
async def test_compaction_prunes_only_superseded_working_snapshots_without_llm(
    monkeypatch,
):
    settings = agent_settings.AgentPreferences.model_construct(
        **{
            **agent_settings.defaults(),
            "context_window": 8192,
            "generation_limit": 2048,
            "history_compaction": True,
            "compaction_threshold": 0.8,
        }
    )
    stale = RunState().context()
    stale["content"] += "x" * 30000
    stale["metadata"].update(agent_context=False, historical_state=True)
    latest = RunState(files_changed=["/workspace/app.py"], revision=1).context()
    request = {
        "role": "user",
        "content": "Keep my task",
        "metadata": {"agent_request": True},
    }
    summarize = AsyncMock()
    monkeypatch.setattr(context_compactor, "_llm_summarize", summarize)
    with agent_settings.preference_snapshot(settings):
        messages, compacted = await context_compactor.compact_conversation(
            [request, stale, latest], "local"
        )
    assert compacted and messages == [request, latest]
    summarize.assert_not_awaited()


def test_builtin_skills_seed_only_missing_and_preserve_customized_or_disabled(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(skills, "skills_root", lambda: tmp_path)
    skills.install_builtin_skills()
    target = tmp_path / "full-stack-app-builder" / "SKILL.md"
    assert "scaffold_project" in target.read_text()
    target.write_text("custom disabled skill")
    skills.install_builtin_skills()
    assert target.read_text() == "custom disabled skill"


@pytest.mark.asyncio
async def test_run_step_injects_application_owned_hash_not_model_supplied(
    workspace, monkeypatch
):
    sandbox, files, _ = workspace
    sandbox.id = "sandbox"
    path = "/workspace/x.txt"
    files[path] = "abc"
    state = RunState(
        reads={path: {"sha256": sha("abc"), "total": 3, "ranges": [(0, 3)]}}
    )
    monkeypatch.setattr(agent, "_record_command", AsyncMock())
    _, event = await agent._run_step(
        sandbox=sandbox,
        conversation=SimpleNamespace(id="conv"),
        task_id="task",
        name="edit_file",
        args={
            "path": path,
            "old_text": "abc",
            "new_text": "def",
            "_expected_sha256": "forged",
        },
        state=state,
        parent_id=None,
        event_queue=None,
    )
    assert event["changed"] and files[path] == "def"
