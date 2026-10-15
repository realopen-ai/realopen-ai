"""Coder safety and deterministic progress; no model or Docker required."""

import hashlib
import json
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.coder import agent, starters, tooling
from app.config import load_profiles
from app.agent.coder.working_state import RunState, compact_output, validation_kind
from app.services import skills
from app.services import agent_settings, context_compactor


def test_hardware_profiles_default_to_official_ornith_coder_tags():
    profiles = load_profiles(str(Path(__file__).resolve().parents[2] / "profiles.yml"))
    large = {"nvidia_xlarge", "apple_large", "apple_xlarge"}
    for name, profile in profiles.items():
        coder = profile.get_model_by_role("default_coder")
        assert coder.id == ("ornith-1.5:35b" if name in large else "ornith-1.5:9b")


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
    files[path] = "abcdefghij\n" * 1500
    state = RunState()
    for first, last in [(1, 400), (801, 1200), (401, 800), (1201, 1500)]:
        output, event = await tooling.dispatch_tool(
            sandbox, "read_file", {"path": path, "start_line": first, "end_line": last}
        )
        data = json.loads(output)
        assert data["content"] == "".join(
            files[path].splitlines(keepends=True)[first - 1 : last]
        )
        assert data["next_line"] == (last + 1 if last < 1500 else None)
        assert data["sha256"] == sha(files[path])
        state.record("read_file", {"path": path}, output, event)
        assert state.read_arguments(path)["_full_read"] == (first == 1201)
    files[path] += "changed"
    _, event = await tooling.dispatch_tool(sandbox, "read_file", {"path": path})
    state.record("read_file", {"path": path}, "read", event)
    assert not state.read_arguments(path)["_full_read"]


@pytest.mark.asyncio
async def test_targeted_edits_do_not_require_read_ranges_but_reject_stale_or_ambiguous_text(
    workspace,
):
    sandbox, files, _ = workspace
    path = "/workspace/main.ts"
    files[path] = "const a = 1;\nconst b = 1;\n"
    args = {
        "path": path,
        "old_text": "const a = 1;",
        "new_text": "const a = 2;",
    }
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
async def test_small_files_are_read_whole_and_line_ranges_preserve_exact_source(
    workspace,
):
    sandbox, files, _ = workspace
    path = "/workspace/example.ts"
    files[path] = "// élève\r\nconst a = 1;\r\nconst b = 2;\r\n"
    output, event = await tooling.dispatch_tool(sandbox, "read_file", {"path": path})
    page = json.loads(output)
    assert page["content"] == files[path]
    assert page["total_lines"] == 3 and page["next_line"] is None
    output, event = await tooling.dispatch_tool(
        sandbox,
        "read_file",
        {
            "path": path,
            "start_line": 2,
            "end_line": 2,
        },
    )
    assert json.loads(output)["content"] == "const a = 1;\r\n"
    assert event["read"]["offset"] == len("// élève\r\n")
    output, _ = await tooling.dispatch_tool(
        sandbox,
        "edit_file",
        {
            "path": path,
            "old_text": "const b = 2;",
            "new_text": "const b = 3;",
        },
    )
    assert "Wrote" in output  # Exact match outside the read range is allowed.
    for bad in [
        {"start_line": 0},
        {"start_line": 3, "end_line": 2},
        {"offset": 15, "limit": 6},
    ]:
        with pytest.raises(ValueError, match="line"):
            await tooling.dispatch_tool(sandbox, "read_file", {"path": path, **bad})


@pytest.mark.asyncio
async def test_empty_and_oversized_lines_are_explicit_not_silently_truncated(workspace):
    sandbox, files, _ = workspace
    path = "/workspace/data.txt"
    files[path] = ""
    output, event = await tooling.dispatch_tool(sandbox, "read_file", {"path": path})
    state = RunState()
    state.record("read_file", {"path": path}, output, event)
    assert (
        json.loads(output)["content"] == "" and state.read_arguments(path)["_full_read"]
    )
    files[path] = "x" * 17000
    with pytest.raises(ValueError, match="exceeds.*read budget"):
        await tooling.dispatch_tool(sandbox, "read_file", {"path": path})


@pytest.mark.asyncio
async def test_write_fail_test_targeted_fix_and_rerun_without_redundant_reads(
    workspace, monkeypatch
):
    sandbox, files, _ = workspace
    path = "/workspace/calculate.py"
    state = RunState()
    original_call = tooling.sandbox_host.call

    monkeypatch.setattr(
        tooling,
        "source_fingerprint",
        AsyncMock(side_effect=lambda _: {path: sha(files[path])}),
    )

    async def host(action, target, payload=None, **kwargs):
        if action == "exec" and payload["command"] == "python calculate.py":
            try:
                exec(compile(files[path], path, "exec"), {})
                return {"exit_code": 0, "stdout": "passed", "stderr": ""}
            except NameError as error:
                return {"exit_code": 1, "stdout": "", "stderr": str(error)}
        return await original_call(action, target, payload, **kwargs)

    monkeypatch.setattr(tooling.sandbox_host, "call", host)

    async def step(name, args):
        if name in {"edit_file", "write_file"}:
            args = {**args, **state.read_arguments(args["path"])}
        output, event = await tooling.dispatch_tool(sandbox, name, args)
        state.record(name, args, output, event)
        return output, event

    await step(
        "write_file",
        {"path": path, "content": "result = ceil(1.2)\nassert result == 2\n"},
    )
    assert state.read_arguments(path)["_full_read"]
    _, failure = await step("run_tests", {"command": "python calculate.py"})
    assert failure["exit_code"] == 1
    _, edit = await step(
        "edit_file",
        {
            "path": path,
            "old_text": "result = ceil(1.2)",
            "new_text": "from math import ceil\nresult = ceil(1.2)",
        },
    )
    assert edit["changed"] and state.read_arguments(path)["_full_read"]
    _, success = await step("run_tests", {"command": "python calculate.py"})
    assert success["exit_code"] == 0
    # Complete replacement can also use the known, successfully published snapshot.
    _, replacement = await step(
        "write_file", {"path": path, "content": files[path] + "assert ceil(2.1) == 3\n"}
    )
    assert replacement["changed"]


@pytest.mark.asyncio
async def test_edit_rejections_identify_missing_and_ambiguous_matches(workspace):
    sandbox, files, _ = workspace
    path = "/workspace/example.ts"
    files[path] = "const a = 1;\nconst a = 1;\n"
    original = files[path]
    for old, expected in [
        ("const a = 1;", "[1, 2]"),
        ("const a = 2;", "not found verbatim"),
    ]:
        output, event = await tooling.dispatch_tool(
            sandbox, "edit_file", {"path": path, "old_text": old, "new_text": "new"}
        )
        assert expected in output and event["corrective"] and files[path] == original
    files[path] = "aaaa"
    _, event = await tooling.dispatch_tool(
        sandbox,
        "edit_file",
        {
            "path": path,
            "old_text": "aaa",
            "new_text": "b",
        },
    )
    assert (
        event["corrective"] and files[path] == "aaaa"
    )  # Overlapping matches are ambiguous too.


@pytest.mark.asyncio
async def test_atomic_edit_rejects_concurrent_change_during_publication(
    workspace, monkeypatch
):
    sandbox, files, _ = workspace
    path = "/workspace/entry.ts"
    files[path] = "const value = 1;\n"
    original_call = tooling.sandbox_host.call

    async def host(action, target, payload=None, **kwargs):
        if action == "files/write-checked":
            files[path] = "const value = 99; // user's concurrent change\n"
        return await original_call(action, target, payload, **kwargs)

    monkeypatch.setattr(tooling.sandbox_host, "call", host)
    with pytest.raises(RuntimeError, match="409"):
        await tooling.dispatch_tool(
            sandbox,
            "edit_file",
            {"path": path, "old_text": "value = 1", "new_text": "value = 2"},
        )
    assert "99" in files[path]


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
async def test_scaffold_publishes_tree_refresh_before_dependency_setup_finishes(
    workspace, monkeypatch
):
    sandbox, files, _ = workspace
    sandbox.id = "scaffold-workspace"
    original_call = tooling.sandbox_host.call
    installing, release = asyncio.Event(), asyncio.Event()

    async def host(action, target, payload=None, **kwargs):
        if action == "exec" and payload.get("command") == "uv sync --all-groups":
            installing.set()
            await release.wait()
        return await original_call(action, target, payload, **kwargs)

    monkeypatch.setattr(tooling.sandbox_host, "call", host)
    monkeypatch.setattr(agent, "dispatch_tool", tooling.dispatch_tool)
    monkeypatch.setattr(agent, "_record_command", AsyncMock())
    queue = asyncio.Queue()
    running = asyncio.create_task(
        agent._run_step(
            sandbox=sandbox,
            conversation=SimpleNamespace(id="conversation"),
            task_id="task",
            name="scaffold_project",
            args={"name": "app", "backend": "fastapi", "frontend": "none"},
            state=RunState(),
            parent_id="delegate",
            event_queue=queue,
        )
    )
    try:
        await asyncio.wait_for(installing.wait(), 1)
        assert not running.done()
        updates = []
        while not queue.empty():
            updates.append(queue.get_nowait())
        refreshes = [update for update in updates if update.get("refreshFiles")]
        assert len(refreshes) == 2  # Starter batch, then Python project configuration.
        assert all(
            update["sandboxId"] == sandbox.id and "status" not in update
            for update in refreshes
        )
        assert (
            "/workspace/backend/app.py" in files
            and "/workspace/pyproject.toml" in files
        )
    finally:
        release.set()
        await running
    completed = queue.get_nowait()
    assert completed["status"] == "completed" and completed["refreshFiles"] is True


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
async def test_edit_can_target_unique_text_outside_the_last_read_range(workspace):
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
    assert event["changed"] and files[path] == "visible\ngone"


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
