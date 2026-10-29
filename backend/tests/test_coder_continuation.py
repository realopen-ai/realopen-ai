"""Continuation and bounded exploration, independent of any generated app type."""

import copy
import json
from unittest.mock import AsyncMock

import pytest

from app.agent.coder import provider
from app.agent.coder.context import tokens
from app.agent.coder.tools import dispatch_tool
from app.agent.coder.working_state import RunState, inspection_step
from app.agent.protocol import assistant_call_message, tool_result_message
from app.config import settings
from app.services import agent_settings, coder_settings, context_compactor as cc


@pytest.fixture(autouse=True)
def preferences(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_CONTEXT_WINDOWS", {})
    monkeypatch.setattr(settings, "AGENT_CONTEXT_MARGIN_TOKENS", 256)
    monkeypatch.setattr(
        agent_settings,
        "_overrides",
        {
            "context_window": 12000,
            "generation_limit": 2000,
            "history_compaction": True,
            "compaction_threshold": 0.65,
        },
    )
    monkeypatch.setattr(
        coder_settings,
        "_overrides",
        {
            "context_window": 12000,
            "generation_limit": 2000,
            "handoff_tokens": 1500,
            "source_context_tokens": 1000,
            "recent_context_tokens": 700,
            "tool_output_tokens": 400,
        },
    )


def inspection(state, index, content="source\n" * 500):
    path = f"/workspace/module{index}.py"
    args = {"path": path}
    page = {
        "path": path,
        "sha256": f"hash-{index}",
        "content": content,
        "offset": 0,
        "start_line": 1,
        "end_line": content.count("\n"),
        "total_chars": len(content),
        "next_line": None,
    }
    state.record("read_file", args, json.dumps(page), {"read": page})
    call = {"id": f"read-{index}", "tool": "read_file", "args": args}
    result = tool_result_message(call, json.dumps(page))
    result["metadata"] = {
        "coder_result": {
            "tool": "read_file",
            "success": True,
            "path": path,
            "sha256": page["sha256"],
            "range": [0, len(content)],
        }
    }
    return [assistant_call_message("", [call]), result]


@pytest.mark.asyncio
async def test_plan_is_saved_without_claiming_file_or_verification_progress():
    state = RunState()
    plan = {
        "steps": [{"task": "Implement requested filtering", "status": "in_progress"}],
        "next_action": "Edit the filter handler, then run its focused test",
        "notes": "Keep the existing backend; no new dependency is needed",
    }
    output, event = await dispatch_tool(None, "update_plan", plan)
    state.record("update_plan", plan, output, event)
    assert state.memory.plan == plan
    assert state.checkpoint()["plan"] == plan
    assert not state.files_changed and not state.validations
    assert state.verification_attempts == 0 and state.revision == 0
    assert "model_authored_plan_not_verification" in state.context()["content"]
    with pytest.raises(ValueError, match="Invalid continuation plan"):
        await dispatch_tool(None, "update_plan", {**plan, "notes": "x" * 1201})
    noisy = {**plan, "steps": [{**plan["steps"][0], "source": "x" * 100000}]}
    _, event = await dispatch_tool(None, "update_plan", noisy)
    assert event["working_plan"] == plan


def test_refresh_replaces_only_derived_snapshots_preserving_tool_chronology():
    state = RunState()
    request = {
        "role": "user",
        "content": "Build the requested app",
        "metadata": {"agent_request": True},
    }
    messages = [{"role": "system", "content": "Stable prefix"}, request]
    for i in range(15):
        messages.extend(inspection(state, i, "line\n"))
        original = copy.deepcopy(messages)
        messages = state.memory.refresh_context(
            messages, "coder", None, True, state.context
        )
        snapshots = [
            m for m in messages if (m.get("metadata") or {}).get("agent_coder_state")
        ]
        assert len(snapshots) == 1
        assert [m for m in messages if m not in snapshots] == [
            m
            for m in original
            if not (m.get("metadata") or {}).get("agent_coder_state")
        ]
        assert original[0] == messages[0] and messages[1] == request


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_repeated_source_pressure_keeps_plan_receipts_and_failed_checks(
    monkeypatch, native
):
    state = RunState()
    state.memory.plan = {"next_action": "Fix response validation", "steps": []}
    state.memory.project_handoff = {
        "resources": ["records"],
        "application_generated": True,
    }
    state.record(
        "run_tests",
        {},
        "ValidationError: expected 422, got 200",
        {"command": "pytest", "exit_code": 1},
    )
    # A summarizer could invent a transcript like the failed live run. It must
    # never be called or have its fabricated execution history replayed.
    summarizer = AsyncMock(
        return_value="ASSISTANT: Replaced cookie auth with OAuth2. TOOL: fake sha256 receipt"
    )
    monkeypatch.setattr(cc, "_llm_summarize", summarizer)
    messages = [
        {"role": "system", "content": "Stable instructions"},
        {
            "role": "user",
            "content": "Fix validation",
            "metadata": {"agent_request": True},
        },
    ]
    for cycle in range(3):
        for index in range(8):
            messages.extend(inspection(state, cycle * 8 + index))
        # Preserve the latest failure exchange exactly, even with evicted source.
        call = {
            "id": f"failed-{cycle}",
            "tool": "run_tests",
            "args": {"command": "pytest"},
        }
        latest = [
            assistant_call_message("", [call]),
            tool_result_message(
                call,
                "ValidationError: expected 422, got 200",
                application_feedback=True,
            ),
        ]
        messages.extend(latest)
        messages = state.memory.refresh_context(
            messages, "coder", None, native, state.context
        )
        messages, changed = await state.memory.compact(
            messages,
            "coder",
            compactor=cc.compact_conversation,
            tools=None,
            native_tools=native,
        )
        assert changed
        assert all(item in messages for item in latest)
        assert (
            cc.estimate_request_tokens(cc.model_messages(messages, native))
            <= cc.context_budget("coder").input_limit
        )
        assert "Fix response validation" in json.dumps(messages)
        assert "expected 422, got 200" in state.context()["content"]
        assert (
            tokens(list(state.memory.observations.values()))
            <= state.memory.budgets()["handoff"]
        )
        assert (
            len(
                [
                    m
                    for m in messages
                    if (m.get("metadata") or {}).get("agent_coder_state")
                ]
            )
            == 1
        )
        assert "Replaced cookie auth" not in json.dumps(messages)
        summarizer.assert_not_awaited()
    assert state.memory.plan["next_action"] == "Fix response validation"
    assert state.memory.observations
    assert len(state.memory.sources) < len(state.memory.observations)
    checkpoint = state.checkpoint()
    assert checkpoint["execution"]["checks"]["backend_tests"]["status"] == "failed"
    assert checkpoint["observations"] and checkpoint["project_handoff"]


def test_novel_files_do_not_allow_endless_app_exploration_after_recovery():
    state = RunState(project={"backend": {}})
    for index in range(6):
        marker = state.round_marker()
        inspection(state, index, "new source\n")
        state.recovery_sent = index >= 2
        reason = state.finish_round(marker)
    assert "sustained source exploration" in reason
    assert state.repeated_read_rounds == 0  # Every read was new, unlike old guard.
    assert state.checkpoint()["execution"]["read_only_rounds"] == 6


def test_targeted_reads_and_a_real_check_reset_exploration_not_plan_updates():
    state = RunState(project={"frontend": {}})
    state.read_only_rounds = 3
    state.recovery_sent = True
    marker = state.round_marker()
    inspection(state, 1, "targeted source")
    assert not state.finish_round(marker)
    marker = state.round_marker()
    state.record(
        "update_plan", {}, "saved", {"working_plan": {"next_action": "Run a check"}}
    )
    assert not state.finish_round(marker)
    assert state.read_only_rounds == 4
    marker = state.round_marker()
    state.record("run_tests", {}, "failed", {"command": "pytest", "exit_code": 1})
    assert not state.finish_round(marker)
    assert state.read_only_rounds == 0 and not state.recovery_sent


def test_verified_full_stack_moves_to_integration_not_frontend_rediscovery():
    state = RunState(
        project={"backend": {"test": "pytest"}, "frontend": {"build": "vite build"}},
        files_changed=["/workspace/backend/app.py"],
    )
    for category in ("backend_tests", "frontend_tests", "typecheck", "build"):
        state.validations[category] = {"revision": 0}
    assert state.completion_missing() == ["live preview smoke check"]
    assert "outstanding HTTP/API smoke check" in state.workflow_context()["next_focus"]
    assert "frontend work" not in state.workflow_context()["next_focus"]


def test_failed_integration_feedback_survives_without_semantic_summary():
    state = RunState()
    state.record(
        "check_preview",
        {"port": 6767, "path": "/api/health"},
        "curl: (7) Failed to connect to port 6767",
        {"exit_code": 7},
    )
    receipt = state.checkpoint()["execution"]["checks"]["integration"]
    assert receipt["status"] == "failed" and receipt["port"] == 6767
    assert "Failed to connect" in state.context()["content"]


def test_arbitrary_script_failures_are_retained_and_successful_retry_retires_them():
    state = RunState()
    args = {"command": "uv run python tmp/verify.py"}
    state.record(
        "run_command",
        args,
        "AssertionError: expected 201, got 404",
        {**args, "exit_code": 1},
    )
    assert not state.validations  # Not mislabelled as a known test runner.
    assert "expected 201, got 404" in state.context()["content"]
    assert (
        state.checkpoint()["execution"]["recent_failures"][0]["observed_revision"] == 0
    )
    state.record("run_command", args, "HTTP checks passed", {**args, "exit_code": 0})
    assert not state.failures and not state.checkpoint()["execution"]["recent_failures"]


def test_failed_attempts_remain_bounded_with_large_outputs_and_edit_arguments():
    state = RunState()
    for index in range(10):
        state.record(
            "edit_file",
            {"path": f"/workspace/{index}.py", "old_text": "x" * 50000},
            "Error details\n" * 10000,
            {"exit_code": 1},
        )
    failures = state.checkpoint()["execution"]["recent_failures"]
    assert len(failures) == 3
    assert tokens(failures) < state.memory.budgets()["output"] + 300
    assert "old_text" not in json.dumps(failures)


def test_contract_invalidation_explains_the_change_instead_of_looking_like_amnesia():
    state = RunState()
    state.memory.api_contract = {"source": "Runtime OpenAPI", "operations": []}
    state.invalidate_checks(["/workspace/frontend/src/App.tsx"])
    assert state.memory.api_contract
    state.invalidate_checks(["/workspace/shared.py"])
    assert state.memory.api_contract is None
    status = state.checkpoint()["api_contract_status"]
    assert status["status"] == "invalidated"
    assert status["changed_paths"] == ["/workspace/shared.py"]
    assert "refresh the relevant /openapi.json" in state.context()["content"]


@pytest.mark.parametrize(
    "command,expected",
    [
        ("cd /workspace && cat backend/app.py", True),
        ("cd /workspace; rg route backend | head -20", True),
        ("git status && git diff", True),
        ("cd /workspace && sed -n '1,30p' backend/app.py", True),
        ("cd /workspace && uv run pytest", False),
        ("cat backend/app.py && python modify.py", False),
        ("echo changed > backend/app.py", False),
    ],
)
def test_shell_wrappers_do_not_hide_inspection_or_misclassify_mutation(
    command, expected
):
    assert inspection_step("run_command", {"command": command}) is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_oversized_parallel_read_batch_keeps_pairing_latest_source_and_errors(
    monkeypatch, native
):
    state = RunState()
    calls, results = [], []
    for index in range(12):
        _, result = inspection(state, index)
        calls.append(
            {
                "id": f"read-{index}",
                "tool": "read_file",
                "args": {"path": f"/workspace/module{index}.py"},
            }
        )
        results.append(result)
    failed = {
        "id": "failed-edit",
        "tool": "edit_file",
        "args": {"path": "/workspace/module11.py"},
    }
    calls.append(failed)
    feedback = tool_result_message(
        failed, "old_text not found; inspect lines 12–18", application_feedback=True
    )
    feedback["metadata"] = {"coder_result": {"tool": "edit_file", "success": False}}
    results.append(feedback)
    messages = [
        {"role": "system", "content": "Stable instructions"},
        {
            "role": "user",
            "content": "Fix the application",
            "metadata": {"agent_request": True},
        },
        assistant_call_message("", calls),
        *results,
        state.context(),
    ]
    assert (
        cc.estimate_request_tokens(cc.model_messages(messages, native))
        > cc.context_budget("coder").input_limit
    )
    summary = AsyncMock(return_value=None)
    monkeypatch.setattr(cc, "_llm_summarize", summary)
    result, changed = await state.memory.compact(
        messages,
        "coder",
        compactor=cc.compact_conversation,
        tools=None,
        native_tools=native,
    )
    assert changed and results[-2] in result and feedback in result
    assert len([item for item in result if item["role"] == "tool"]) == len(calls)
    assert len(
        next(item for item in result if item.get("tool_calls"))["tool_calls"]
    ) == len(calls)
    assert (
        cc.estimate_request_tokens(cc.model_messages(result, native))
        <= cc.context_budget("coder").input_limit
    )
    assert any(
        (item.get("metadata", {}).get("coder_result") or {}).get("archived")
        for item in result
    )
    summary.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("recovery", [False, True])
async def test_coder_db_preferences_reach_provider_context_not_general_defaults(
    monkeypatch, recovery
):
    monkeypatch.setitem(coder_settings._overrides, "context_window", 40000)
    inference = AsyncMock(return_value={"message": {"content": "done"}})
    monkeypatch.setattr(provider.providers, "chat_once", inference)

    @coder_settings.with_snapshot
    async def run():
        return await provider._chat_attempt(
            "ornith-1.5:9b",
            [{"role": "user", "content": "Hello"}],
            None,
            recovery=recovery,
        )

    await run()
    assert inference.call_args.kwargs["options"]["num_ctx"] == 40000
    assert inference.call_args.kwargs["options"]["num_predict"] == 2000
    assert agent_settings.effective().context_window == 12000
