"""Regression cases from read-only coder runs; no task-specific scaffolding rules."""

import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.coder import agent, tooling
from app.agent.coder.execution_policy import (
    explicit_choices,
    scaffold_constraint_error,
    verification_allowed,
    baseline_commands,
)
from app.agent.coder.context import CoderMemory
from app.agent.coder.working_state import RunState
from app.agent.protocol import (
    assistant_call_message,
    tool_result_message,
    model_messages,
)


@pytest.mark.parametrize(
    "user_request,expected",
    [
        (
            "Build a full-stack publishing app.\nBackend: FastAPI + SQLite.\nFrontend: Vite React.\nUse TailwindCSS.",
            {"backend": "fastapi", "frontend": "react-vite", "styling": "tailwind"},
        ),
        ("Build a contact directory with vanilla TypeScript.", {"frontend": "vite"}),
        ("Use React.js for this frontend", {"frontend": "react-vite"}),
        ("Choose React or vanilla TypeScript", {}),
        ("Do not use React", {}),
        ("Don't use React", {}),
        ("Maybe use React", {}),
        ("Improve this React Native application", {}),
        ("Explain this snippet:\n```\nReact\n```", {}),
        ("Fix the form", {}),
    ],
)
def test_only_explicit_unambiguous_stack_mentions_become_constraints(
    user_request, expected
):
    assert {
        key: value["value"] for key, value in explicit_choices(user_request).items()
    } == expected


def test_aliases_are_extensible_registry_metadata_not_task_manager_rules(monkeypatch):
    monkeypatch.setitem(tooling.FRONTENDS, "other-web", {"request_names": ["OtherWeb"]})
    assert (
        explicit_choices("Use OtherWeb for the storefront")["frontend"]["value"]
        == "other-web"
    )


def test_mismatched_stack_returns_exact_repair_without_silently_changing_arguments():
    args = {"frontend": "vite", "backend": "fastapi"}
    before = copy.deepcopy(args)
    error = scaffold_constraint_error(
        "scaffold_crud_app", args, explicit_choices("Frontend: Vite React")
    )
    assert "NOT executed" in error and "react-vite" in error
    assert args == before
    assert not scaffold_constraint_error(
        "read_file", args, explicit_choices("Use React")
    )
    assert not scaffold_constraint_error(
        "scaffold_project", {"frontend": "react-vite"}, explicit_choices("Use React")
    )


@pytest.mark.parametrize(
    "user_request",
    [
        "Do not run tests",
        "Don't run any checks",
        "Skip verification",
        "Never execute the tests",
        "Don't run the backend tests",
        "Skip frontend checks",
        "Build without running tests",
    ],
)
def test_verification_opt_out_is_honored(user_request):
    assert not verification_allowed(user_request)
    assert verification_allowed("Add tests and build the frontend")
    assert verification_allowed("Never skip tests")
    assert verification_allowed("Do not skip the backend tests")


def generated_event():
    return {
        "exit_code": 0,
        "project_handoff": {"application_generated": True},
        "project": {
            "backend": {"test": "pytest"},
            "frontend": {
                "test": "vitest run",
                "typecheck": "tsc --noEmit",
                "build": "vite build",
            },
        },
    }


def test_only_successful_generated_apps_get_baseline_commands():
    event = generated_event()
    assert baseline_commands(event) == [
        "pytest",
        "vitest run",
        "tsc --noEmit",
        "vite build",
    ]
    assert not baseline_commands({**event, "exit_code": 1})
    assert not baseline_commands(
        {**event, "project_handoff": {"application_generated": False}}
    )


@pytest.mark.asyncio
async def test_automatic_verification_runs_real_tool_pipeline_and_preserves_protocol(
    monkeypatch,
):
    checks = [
        {
            "command": command,
            "exit_code": 0 if index != 1 else 1,
            "output": "DOM assertion failed" if index == 1 else "passed",
        }
        for index, command in enumerate(baseline_commands(generated_event()))
    ]
    dispatch = AsyncMock(
        return_value=(
            json.dumps({"checks": checks}),
            {"checks": checks, "exit_code": 1},
        )
    )
    monkeypatch.setattr(agent, "dispatch_tool", dispatch)
    monkeypatch.setattr(agent, "_check_cancellation", AsyncMock())
    monkeypatch.setattr(agent, "_record_command", AsyncMock())
    previous = {"id": "scaffold-call", "tool": "scaffold_crud_app", "args": {}}
    messages = [
        assistant_call_message("", [previous]),
        tool_result_message(previous, "created"),
    ]
    queue = asyncio.Queue()
    state = RunState()
    await agent._baseline_verification(
        generated=generated_event(),
        allowed=True,
        sandbox=SimpleNamespace(id="sandbox"),
        conversation=SimpleNamespace(id="conversation"),
        task_id="task",
        state=state,
        parent_id="delegate",
        event_queue=queue,
        messages=messages,
    )
    assert dispatch.call_args.args[1] == "run_checks"
    assert dispatch.call_args.args[2]["commands"] == baseline_commands(
        generated_event()
    )
    assert (
        "backend_tests" in state.validations
        and "frontend_tests" not in state.validations
    )
    assert "DOM assertion failed" in messages[-1]["content"]
    for native in (True, False):
        encoded = model_messages(messages, native=native)
        assert len(encoded) == 4
        if native:
            assert encoded[-1]["tool_call_id"] == encoded[-2]["tool_calls"][0]["id"]
    events = [queue.get_nowait() for _ in range(queue.qsize())]
    assert events[0]["origin"] == "application" and events[-1]["status"] == "error"
    dispatch.reset_mock()
    await agent._baseline_verification(
        generated=generated_event(),
        allowed=False,
        sandbox=None,
        conversation=None,
        task_id=None,
        state=state,
        parent_id=None,
        event_queue=None,
        messages=[],
    )
    dispatch.assert_not_awaited()


def inspect(state, path, digest="unchanged"):
    return state.record(
        "read_file",
        {"path": path},
        "source:" + path,
        {
            "read": {
                "path": path,
                "sha256": digest,
                "content": "source",
                "offset": 0,
                "total_chars": 6,
                "start_line": 1,
            }
        },
    )


def test_read_only_recovery_does_not_limit_new_evidence_or_changed_files():
    state = RunState()
    for index in range(12):
        marker = state.round_marker()
        inspect(state, f"/workspace/source{index}.py")
        assert not state.finish_round(marker)
    assert state.read_only_rounds == 12 and state.repeated_read_rounds == 0
    state.recovery_sent = True
    marker = state.round_marker()
    inspect(state, "/workspace/source0.py", "changed-on-disk")
    assert not state.finish_round(marker)
    marker = state.round_marker()
    state.record("run_tests", {}, "failed test", {"command": "pytest", "exit_code": 1})
    assert not state.finish_round(marker) and state.read_only_rounds == 0


def test_repeated_read_only_rounds_stop_only_after_recovery_and_no_new_evidence():
    state = RunState()
    for name in ("a.py", "b.py", "c.py", "d.py"):
        inspect(state, "/workspace/" + name)
    state.recovery_sent = True
    for names in (("a.py", "b.py"), ("c.py", "d.py")):
        marker = state.round_marker()
        for name in names:
            inspect(state, "/workspace/" + name)
        reason = state.finish_round(marker)
    assert "unchanged evidence" in reason


def test_deduplication_keeps_later_exact_range_and_never_merges_distinct_ranges():
    def exchange(identity, offset):
        call = {
            "id": identity,
            "tool": "read_file",
            "args": {"path": "/workspace/app.py"},
        }
        message = tool_result_message(call, "exact source " * 500)
        message["metadata"] = {
            "coder_result": {
                "tool": "read_file",
                "success": True,
                "path": "/workspace/app.py",
                "sha256": "hash",
                "range": [offset, 1000],
            }
        }
        return [assistant_call_message("", [call]), message]

    messages = [
        *exchange("first", 0),
        *exchange("different", 1000),
        *exchange("latest", 0),
    ]
    original = copy.deepcopy(messages)
    pruned = CoderMemory().prune(messages, 0, 1000)
    assert "latest" in pruned[1]["content"] and len(pruned[1]["content"]) < len(
        messages[1]["content"]
    )
    assert pruned[3] == messages[3] and pruned[-2:] == messages[-2:]
    assert messages == original
