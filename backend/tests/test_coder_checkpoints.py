"""Restricted, bounded planning before deterministic history archival."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx

from app.agent.coder import agent, provider
from app.agent.coder.checkpoints import (
    PLAN_TOOLS,
    PlanCheckpoints,
    PlanCheckpointRequired,
    fit_checkpoint,
)
from app.agent.coder.context import CoderMemory
from app.agent.coder.tools.update_plan import execute as save_plan
from app.agent.coder.working_state import RunState
from app.agent.protocol import assistant_call_message, tool_result_message
from app.services import agent_settings, coder_settings, context_compactor as cc
from app.config import settings


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
            "handoff_tokens": 1000,
            "recent_context_tokens": 1000,
        },
    )


def history():
    call = {"id": "old", "tool": "run_tests", "args": {"command": "pytest"}}
    latest = {
        "id": "latest",
        "tool": "edit_file",
        "args": {"path": "/workspace/api.py"},
    }
    return [
        {"role": "system", "content": "stable instructions"},
        {
            "role": "user",
            "content": "Build the requested app",
            "metadata": {"agent_request": True},
        },
        assistant_call_message("Old work", [call]),
        tool_result_message(
            call, "failure details\n" * 3500, application_feedback=True
        ),
        assistant_call_message("Current repair", [latest]),
        tool_result_message(
            latest,
            "409: source changed. Read the current source.",
            application_feedback=True,
        ),
    ]


@pytest.mark.asyncio
async def test_initial_and_prearchive_checkpoint_are_single_attempts_without_mutating_history():
    phases = PlanCheckpoints()
    memory = CoderMemory()
    messages = history()
    kwargs = (memory, messages, "coder", None, True, cc.compact_conversation)
    assert await phases.phase(*kwargs) == "initial"
    phases.attempted()  # A failed attempt also proceeds, without a retry.
    assert await phases.phase(*kwargs) is None
    phases.compacted()
    assert await phases.phase(*kwargs) == "before_compaction"
    assert memory.compactions == 0 and memory.handoff == ""
    assert "failure details" in messages[3]["content"]
    phases.attempted()
    assert await phases.phase(*kwargs) is None
    compacted, changed = await memory.compact(
        messages,
        "coder",
        compactor=cc.compact_conversation,
        tools=None,
        native_tools=True,
    )
    assert changed and compacted[-2:] == messages[-2:]
    phases.compacted()
    assert (
        await phases.phase(
            memory, compacted, "coder", None, True, cc.compact_conversation
        )
        is None
    )


@pytest.mark.asyncio
async def test_no_prearchive_checkpoint_for_disabled_or_small_history(monkeypatch):
    phases = PlanCheckpoints(initial_attempted=True)
    memory = CoderMemory()
    assert (
        await phases.phase(
            memory, history()[:2], "coder", None, True, cc.compact_conversation
        )
        is None
    )
    monkeypatch.setitem(agent_settings._overrides, "history_compaction", False)
    assert (
        await phases.phase(
            memory, history(), "coder", None, True, cc.compact_conversation
        )
        is None
    )


@pytest.mark.asyncio
async def test_recovery_can_request_one_checkpoint_when_protocol_adds_pressure():
    phases = PlanCheckpoints(initial_attempted=True, requested=True)
    assert (
        await phases.phase(
            CoderMemory(), [], "coder", None, False, cc.compact_conversation
        )
        == "before_compaction"
    )
    phases.attempted()
    assert not phases.requested
    assert (
        await phases.phase(
            CoderMemory(), history(), "coder", None, False, cc.compact_conversation
        )
        is None
    )


@pytest.mark.asyncio
async def test_native_to_text_recovery_preserves_checkpoint_pause_signal(monkeypatch):
    monkeypatch.setattr(provider.tool_capabilities, "should_try_native", lambda _: True)
    error = httpx.HTTPStatusError(
        "Server error",
        request=httpx.Request("POST", "http://local"),
        response=httpx.Response(500, text="temporary server failure"),
    )
    chat = AsyncMock(side_effect=error)
    monkeypatch.setattr(provider.providers, "chat_once", chat)
    checkpoint = AsyncMock(side_effect=PlanCheckpointRequired())
    with pytest.raises(PlanCheckpointRequired):
        await provider.chat_with_tool_recovery(
            "coder", history()[:2], compact_context=checkpoint
        )
    chat.assert_awaited_once()  # No text inference or unsafe archival ran.


@pytest.mark.asyncio
async def test_checkpoint_never_archives_its_input_to_fit():
    messages = history()
    with pytest.raises(cc.ContextBudgetExceeded):
        await fit_checkpoint(messages, "coder", max_output_tokens=1000)
    short = messages[:2]
    assert await fit_checkpoint(
        short, "coder", tools=PLAN_TOOLS, max_output_tokens=1000
    ) == (short, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback", [False, True])
async def test_provider_uses_only_plan_schema_and_small_output_in_both_protocols(
    monkeypatch, fallback
):
    monkeypatch.setattr(
        provider.tool_capabilities, "should_try_native", lambda _: not fallback
    )
    requests = []

    async def chat(model, messages, tools=None, **kwargs):
        requests.append((messages, tools, kwargs))
        return {
            "message": {
                "content": json.dumps(
                    {
                        "tool": "update_plan",
                        "args": {
                            "steps": [
                                {"task": "Fix validation", "status": "in_progress"}
                            ],
                            "next_action": "Run the focused test",
                        },
                    }
                )
            }
        }

    monkeypatch.setattr(provider.providers, "chat_once", chat)
    await provider.chat_with_tool_recovery(
        "coder",
        history()[:2],
        tools=PLAN_TOOLS,
        output_limit=1000,
        thinking=False,
        compact_context=fit_checkpoint,
    )
    messages, tools, options = requests[0]
    assert options["think"] is False
    assert options["options"]["num_predict"] == 1000
    assert options["options"]["num_ctx"] == 12000
    if fallback:
        assert tools is None
        assert "update_plan" in json.dumps(messages)
        assert "scaffold_crud_app" not in json.dumps(messages)
    else:
        assert tools == PLAN_TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["valid", "wrong_tool", "siblings", "empty", "malformed", "truncated"]
)
async def test_checkpoint_accepts_one_valid_plan_only(monkeypatch, case):
    state = RunState()
    old = {
        "steps": [{"task": "Current work", "status": "in_progress"}],
        "next_action": "Repair the failing check",
    }
    state.memory.plan = old
    args = {
        **old,
        "next_action": "Verify the API contract",
        "notes": "Keep cookie sessions; no bearer-token migration was requested.",
    }

    def native(name, values):
        return {"function": {"name": name, "arguments": values}}

    calls = [native("update_plan", args)]
    response = {}
    if case == "wrong_tool":
        calls = [native("write_file", {"path": "/workspace/api.py", "content": "bad"})]
    if case == "siblings":
        calls.append(native("run_command", {"command": "rm api.py"}))
    if case == "empty":
        calls = []
    if case == "malformed":
        calls = [native("update_plan", '{"steps":')]
    if case == "truncated":
        response["done_reason"] = "length"

    async def run(**kwargs):
        return await save_plan(None, kwargs["args"])

    step = AsyncMock(side_effect=run)
    monkeypatch.setattr(agent, "_run_step", step)
    await agent._apply_plan_checkpoint(
        response,
        calls,
        state=state,
        sandbox=SimpleNamespace(id="sandbox"),
        conversation=None,
        task_id="task",
        parent_id=None,
        event_queue=None,
    )
    if case == "valid":
        step.assert_awaited_once()
        assert state.memory.plan == args
        assert not state.files_changed and not state.validations
    else:
        step.assert_not_awaited()
        assert state.memory.plan == old


@pytest.mark.asyncio
async def test_plan_accepts_modest_extra_detail_without_dropping_it_but_enforces_token_budget(
    monkeypatch,
):
    # Regression shape from Ornith's real pre-archive checkpoint: useful seven
    # steps and >600 characters of decisions, not a malformed/oversized call.
    plan = {
        "steps": [
            {"task": f"Retain observed phase {i}", "status": "completed"}
            for i in range(7)
        ],
        "next_action": "Verify the remaining integration behavior",
        "notes": "Keep cookie sessions and per-user scope. " * 18,
    }
    monkeypatch.setitem(coder_settings._overrides, "handoff_tokens", 2048)
    _, event = await save_plan(None, plan)
    assert event["working_plan"] == plan
    monkeypatch.setitem(coder_settings._overrides, "handoff_tokens", 128)
    with pytest.raises(ValueError, match="configured token budget"):
        await save_plan(None, plan)
    with pytest.raises(ValueError, match="maxItems"):
        await save_plan(None, {**plan, "steps": plan["steps"] * 2})
