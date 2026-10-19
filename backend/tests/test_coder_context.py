"""Coder continuity across real compaction boundaries; inference is deterministic here."""

import copy
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.coder import agent, provider
from app.agent.coder.context import CoderMemory, bounded_output, tokens
from app.agent.coder.handoff import project_handoff
from app.agent.coder.starters import BACKENDS, FRONTENDS
from app.agent.coder.working_state import RunState
from app.agent.protocol import assistant_call_message, tool_result_message
from app.config import settings
from app.services import agent_settings, coder_settings, context_compactor as cc


@pytest.fixture(autouse=True)
def budgets(monkeypatch):
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
            "handoff_tokens": 1800,
            "source_context_tokens": 3000,
            "recent_context_tokens": 2000,
            "tool_output_tokens": 700,
        },
    )


def read(state, path, content):
    digest = hashlib.sha256(content.encode()).hexdigest()
    state.record(
        "read_file",
        {"path": path},
        content,
        {
            "read": {
                "path": path,
                "content": content,
                "sha256": digest,
                "offset": 0,
                "start_line": 1,
                "total_chars": len(content),
                "next_line": None,
            }
        },
    )
    return digest


def exchange(identity, name, output, success=True):
    call = {"id": identity, "tool": name, "args": {"command": "pytest"}}
    response = tool_result_message(call, output, application_feedback=not success)
    response["metadata"] = {"coder_result": {"tool": name, "success": success}}
    return [assistant_call_message("", [call]), response]


@pytest.mark.parametrize(
    "content", ["const x = 1;\n", "// élève\r\nconst x = 1;\r\n", "print('مرحبا')\n"]
)
def test_recent_sources_preserve_exact_bytes_and_hashes(content):
    state = RunState()
    digest = read(state, "/workspace/ui.ts", content)
    entry = state.memory.source_context()[0]
    assert entry["content"] == content
    assert entry["sha256"] == digest
    assert entry["start_line"] == 1 and entry["partial"] is False


def test_sources_are_invalidated_on_shell_changes_and_replaced_after_edits():
    state = RunState()
    path = "/workspace/api.py"
    read(state, path, "old")
    state.record(
        "run_command", {}, "done", {"changed": True, "files": [path], "exit_code": 0}
    )
    assert not state.memory.sources
    read(state, path, "old")
    state.record(
        "edit_file",
        {"path": path},
        "done",
        {
            "changed": True,
            "file": path,
            "file_content": "new",
            "sha256": "new-hash",
        },
    )
    assert state.memory.source_context()[0]["content"] == "new"
    state.record("run_command", {}, "done", {"changes_unknown": True, "exit_code": 0})
    assert not state.memory.sources


def test_source_budget_evicts_older_entries_and_marks_partial_ranges(monkeypatch):
    monkeypatch.setitem(coder_settings._overrides, "source_context_tokens", 400)
    state = RunState()
    for index in range(10):
        read(state, f"/workspace/{index}.py", "line = 1\n" * 300)
    assert tokens(list(state.memory.sources.values())) <= 400
    entries = state.memory.source_context()
    assert entries and entries[0]["path"].endswith("9.py")
    assert entries[0]["partial"] is True
    assert tokens(entries) <= 400


def test_targeted_edit_retains_the_modified_region(monkeypatch):
    monkeypatch.setitem(coder_settings._overrides, "source_context_tokens", 500)
    state = RunState()
    source = "header\n" * 1000 + "return corrected_value\n" + "footer\n" * 1000
    state.record(
        "edit_file",
        {"new_text": "return corrected_value"},
        "done",
        {
            "file": "/workspace/main.py",
            "changed": True,
            "sha256": "current",
            "file_content": source,
        },
    )
    entry = state.memory.source_context()[0]
    assert "return corrected_value" in entry["content"]
    assert (
        source[entry["offset"] : entry["offset"] + len(entry["content"])]
        == entry["content"]
    )
    assert entry["start_line"] > 1 and entry["partial"]


def test_source_retention_can_be_disabled_and_budgets_scale_for_model_override(
    monkeypatch,
):
    state = RunState()
    read(state, "/workspace/main.py", "print('x')")
    monkeypatch.setitem(settings.AGENT_CONTEXT_WINDOWS, "small", 4096)
    assert state.memory.budgets("small")["handoff"] < 1800
    assert state.memory.budgets("small")["sources"] < 3000
    monkeypatch.setitem(coder_settings._overrides, "source_context_tokens", 0)
    assert state.memory.source_context() == []


def test_pruning_keeps_failed_feedback_and_latest_exchange_exact():
    memory = CoderMemory()
    messages = [
        *exchange("old", "run_command", "Installed packages\n" * 3000),
        *exchange(
            "failed", "run_tests", "AssertionError: expected 204, got 200", False
        ),
        *exchange("latest", "edit_file", "Latest exact tool feedback"),
    ]
    original = copy.deepcopy(messages)
    pruned = memory.prune(messages, recent_tokens=0, output_tokens=300)
    assert len(pruned[1]["content"]) < len(messages[1]["content"])
    assert pruned[3] == messages[3] and pruned[-2:] == messages[-2:]
    assert messages == original
    assert [m.get("tool_call_id") for m in pruned if m["role"] == "tool"] == [
        "old",
        "failed",
        "latest",
    ]


def test_duplicate_read_is_referenced_only_when_exact_source_is_retained():
    state = RunState()
    path = "/workspace/main.py"
    source = "original source\n" * 50
    digest = read(state, path, source)
    messages = [
        *exchange("read", "read_file", source),
        *exchange("next", "run_tests", "passed"),
    ]
    messages[1]["metadata"]["coder_result"].update(path=path, sha256=digest)
    pruned = state.memory.prune(messages, 0, 700)
    assert "retained active_sources" in pruned[1]["content"]
    state.memory.sources.clear()
    assert state.memory.prune(messages, 0, 700)[1] == messages[1]


def test_successful_skill_resources_survive_as_continuity_metadata():
    state = RunState()
    for _ in range(2):
        state.record(
            "load_skill",
            {"name": "builder", "resource": "references/api.md"},
            "instructions",
            None,
        )
    assert state.memory.checkpoint()["loaded_resources"] == [
        {"name": "builder", "resource": "references/api.md"}
    ]


def test_sources_yield_space_to_actual_fixed_context_and_latest_feedback():
    state = RunState()
    read(state, "/workspace/main.py", "print('retained')\n" * 1000)
    messages = [
        {"role": "system", "content": "instructions " * 1700},
        {
            "role": "user",
            "content": "Continue the current task",
            "metadata": {"agent_request": True},
        },
        *exchange("latest", "run_tests", "Exact failure\n" * 200, False),
    ]
    snapshot = state.memory.working_context(
        messages, "coder", None, True, state.context
    )
    assert (
        cc.estimate_request_tokens([*messages, snapshot])
        < cc.context_budget("coder").input_limit
    )
    assert tokens(state.memory.active_sources) < tokens(
        list(state.memory.sources.values())
    )
    assert "Exact failure" in messages[-1]["content"]
    # Budget pressure did not destroy the saved source cache.
    assert state.memory.sources


@pytest.mark.asyncio
async def test_disabled_compaction_neither_prunes_nor_summarizes(monkeypatch):
    monkeypatch.setitem(agent_settings._overrides, "history_compaction", False)
    summarize = AsyncMock()
    monkeypatch.setattr(cc, "_llm_summarize", summarize)
    messages = [
        {"role": "user", "content": "Continue", "metadata": {"agent_request": True}},
        *exchange("old", "run_command", "output\n" * 5000),
        *exchange("latest", "run_tests", "failed", False),
    ]
    original = copy.deepcopy(messages)
    with pytest.raises(cc.ContextBudgetExceeded):
        await CoderMemory().compact(
            messages,
            "coder",
            compactor=cc.compact_conversation,
            tools=None,
            native_tools=True,
        )
    assert messages == original
    summarize.assert_not_awaited()


@pytest.mark.asyncio
async def test_pruning_reclaims_output_before_spending_a_summary_call(monkeypatch):
    summarize = AsyncMock()
    monkeypatch.setattr(cc, "_llm_summarize", summarize)
    messages = [
        {"role": "user", "content": "Continue", "metadata": {"agent_request": True}},
        *exchange("old", "run_command", "Successful package log\n" * 2000),
        *exchange("latest", "run_tests", "failed", False),
    ]
    result, changed = await CoderMemory().compact(
        messages,
        "coder",
        compactor=cc.compact_conversation,
        tools=None,
        native_tools=True,
    )
    assert changed and result[-2:] == messages[-2:]
    assert cc.estimate_request_tokens(result) < cc.estimate_request_tokens(messages)
    summarize.assert_not_awaited()


@pytest.mark.asyncio
async def test_recent_token_budget_counts_exchanges_without_recounting_tool_schemas(
    monkeypatch,
):
    monkeypatch.setattr(
        cc,
        "_llm_summarize",
        AsyncMock(return_value="## Next actions\nContinue testing."),
    )
    recent = [
        *exchange("recent-1", "run_tests", "recent exact evidence " * 25, False),
        *exchange("recent-2", "read_file", "current exact source", False),
    ]
    messages = [
        {
            "role": "user",
            "content": "Keep working",
            "metadata": {"agent_request": True},
        },
        *exchange("old", "run_command", "older work " * 1800, False),
        *recent,
    ]
    tools = [
        {
            "type": "function",
            "function": {
                "name": "run_tests",
                "description": "stable schema instructions " * 200,
                "parameters": {"type": "object"},
            },
        }
    ]
    result, changed = await cc.compact_conversation(
        messages, "coder", tools=tools, recent_tokens=700
    )
    assert changed
    assert all(item in result for item in recent)


@pytest.mark.asyncio
async def test_real_compaction_keeps_app_source_pending_fix_and_structured_notes(
    monkeypatch,
):
    state = RunState()
    state.memory.project_handoff = project_handoff(
        {"backend": BACKENDS["fastapi"], "frontend": FRONTENDS["react-vite"]},
        {"generator": "crud", "resources": ["contacts"]},
    )
    state.memory.api_contract = {
        "source": "Runtime OpenAPI",
        "operations": [{"method": "PATCH", "path": "/api/items/{id}"}],
        "definitions": {},
    }
    state.record("run_tests", {}, "passed", {"command": "pytest", "exit_code": 0})
    read(
        state,
        "/workspace/server.py",
        "@app.patch('/api/items/{id}')\ndef update_item(id): ...\n",
    )
    read(
        state,
        "/workspace/ui.ts",
        "export const update = (id) => fetch(`/api/items/${id}`, {method: 'PATCH'});\n",
    )
    handoff = "## Architecture and interfaces\nPATCH /api/items/{id}. UI calls update().\n## Active work and unresolved failures\nExpected 204, got 200.\n## Next actions\nFix the response status, then rerun the failing test."
    summarize = AsyncMock(return_value=handoff)
    monkeypatch.setattr(cc, "_llm_summarize", summarize)
    latest = exchange(
        "failing-test", "run_tests", "AssertionError: expected 204, got 200", False
    )
    messages = [
        {"role": "system", "content": "Coder instructions"},
        {
            "role": "user",
            "content": "Build the app",
            "metadata": {"agent_request": True},
        },
        *exchange(
            "history", "run_command", "Architecture and old work\n" * 1600, False
        ),
        *latest,
        state.context(),
    ]
    result, changed = await state.memory.compact(
        messages,
        "coder",
        compactor=cc.compact_conversation,
        tools=None,
        native_tools=True,
    )
    assert changed and summarize.await_count > 0
    assert summarize.call_args.kwargs["prompt_name"] == "coder_compaction"
    assert summarize.call_args.kwargs["max_tokens"] > 512
    assert all(item in result for item in latest)
    serialized = json.dumps(result)
    assert "PATCH /api/items/{id}" in serialized and "export const update" in serialized
    assert state.memory.handoff == handoff and state.memory.compactions == 1
    assert "frontend_entrypoint" in serialized and "Runtime OpenAPI" in serialized
    assert "backend_tests" in state.validations
    checkpoint = state.memory.checkpoint()
    assert checkpoint["project_handoff"]["resources"] == ["contacts"]
    assert checkpoint["api_contract"]["operations"][0]["method"] == "PATCH"
    assert cc.estimate_request_tokens(result) <= cc.context_budget("coder").input_limit
    # A second compaction sees the first handoff and retains its relevant decisions.
    followup = [
        *result[:-1],
        *exchange("more-work", "run_command", "new evidence\n" * 2500, False),
        *exchange("latest-read", "read_file", "latest exact source"),
        state.context(),
    ]
    await state.memory.compact(
        followup,
        "coder",
        compactor=cc.compact_conversation,
        tools=None,
        native_tools=True,
    )
    assert any(
        "PATCH /api/items/{id}" in str(call.args)
        for call in summarize.call_args_list[1:]
    )
    assert state.memory.compactions == 2


@pytest.mark.asyncio
async def test_failed_compaction_retains_previous_handoff_and_context(monkeypatch):
    memory = CoderMemory(handoff="Previous decisions")
    monkeypatch.setattr(cc, "_llm_summarize", AsyncMock(return_value=None))
    messages = [
        {"role": "user", "content": "Continue", "metadata": {"agent_request": True}},
        *exchange("old", "run_tests", "failed\n" * 6000, False),
        *exchange("latest", "read_file", "current source"),
    ]
    original = copy.deepcopy(messages)
    with pytest.raises(cc.ContextBudgetExceeded):
        await memory.compact(
            messages,
            "coder",
            compactor=cc.compact_conversation,
            tools=None,
            native_tools=True,
        )
    assert messages == original and memory.handoff == "Previous decisions"


@pytest.mark.asyncio
async def test_task_progress_saves_continuity_checkpoint(monkeypatch):
    state = RunState()
    state.memory.handoff = "## Next actions\nVerify the UI."
    read(state, "/workspace/app.py", "source")
    record = SimpleNamespace()
    commit = AsyncMock()

    @asynccontextmanager
    async def session():
        yield SimpleNamespace(get=AsyncMock(return_value=record), commit=commit)

    monkeypatch.setattr(agent, "async_session_factory", session)
    await agent._save_progress("task", state)
    assert record.context_state == state.memory.checkpoint()
    assert record.context_state["handoff"].endswith("Verify the UI.")
    assert record.context_state["sources"][0]["path"] == "/workspace/app.py"
    commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_text_tool_fallback_uses_the_coder_compaction_hook(monkeypatch):
    messages = [{"role": "user", "content": "Continue the current coding task"}]
    prepared = [{"role": "user", "content": "Retained coder handoff"}]
    compact = AsyncMock(return_value=(prepared, True))
    inference = AsyncMock(return_value={"message": {"content": '{"final":"done"}'}})
    monkeypatch.setattr(provider.providers, "chat_once", inference)
    await provider._chat_attempt(
        "coder",
        messages,
        None,
        recovery=True,
        compact_context=compact,
    )
    assert compact.call_args.args[0][-1]["metadata"]["coder_protocol"] is True
    assert compact.call_args.kwargs["native_tools"] is False
    assert inference.call_args.args[1] == prepared
    assert len(messages) == 1


@pytest.mark.parametrize("text", ["x" * 20000, "أ" * 10000], ids=["ascii", "arabic"])
def test_output_budget_preserves_both_ends(text):
    value = bounded_output("START" + text + "FINAL ERROR", 300)
    assert value.startswith("START") and value.endswith("FINAL ERROR")
    assert tokens(value) <= 300
