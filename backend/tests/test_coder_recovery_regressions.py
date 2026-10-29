"""Regression shapes from a coder stopped while restoring evicted edit sources."""

import json

from app.agent.coder.working_state import RunState
from app.agent.coder.context import tokens
from app.agent.coder.outcome import execution_outcome
from app.agent.execution_state import ExecutionState
from app.agent.results import compact_result
from test_coder_context import budgets, read  # noqa: F401


def test_evicted_or_archived_source_reread_is_not_repeated_available_evidence():
    state = RunState(project={"frontend": {}})
    content = "export const value = 1;\n" * 30
    path = "/workspace/ui.ts"
    digest = read(state, path, content)
    state.recovery_sent = True
    for _ in range(3):
        # The historical receipt survived but no exact source was supplied.
        state.memory.active_sources = []
        state.prepare_round(
            [
                {
                    "role": "tool",
                    "metadata": {
                        "coder_result": {
                            "tool": "read_file",
                            "success": True,
                            "archived": True,
                            "path": path,
                            "sha256": digest,
                            "range": [0, len(content)],
                        }
                    },
                }
            ]
        )
        marker = state.round_marker()
        assert read(state, path, content) is not None
        assert not state.finish_round(marker)
    assert state.recovered_source_reads == 3
    assert state.repeated_read_rounds == 0
    # Restoring evicted context is not unlimited implementation progress.
    for _ in range(3):
        state.memory.active_sources = []
        state.prepare_round([])
        marker = state.round_marker()
        read(state, path, content)
        reason = state.finish_round(marker)
    assert reason and state.stop_reason == "sustained_exploration"


def test_exact_available_source_rereads_still_stop_after_recovery():
    state = RunState()
    path, content = "/workspace/api.py", "def endpoint(): return 1\n"
    read(state, path, content)
    state.recovery_sent = True
    for _ in range(2):
        state.context()  # Actual retained source snapshot supplied this round.
        state.prepare_round([])
        marker = state.round_marker()
        read(state, path, content)
        reason = state.finish_round(marker)
    assert reason and state.stop_reason == "repeated_available_evidence"


def test_current_edit_sources_share_budget_and_plan_files_have_priority():
    state = RunState()
    paths = ["/workspace/ui.tsx", "/workspace/filter.tsx", "/workspace/data.ts"]
    for path in paths:
        read(state, path, "small component\n" * 12)
    state.memory.plan = {"files": paths}
    for i in range(8):
        read(state, f"/workspace/large{i}.py", "large repository source\n" * 1500)
    active = state.memory.source_context()
    assert [s["path"] for s in active[:3]] == paths
    assert all(s["content"] for s in active[:3])
    assert tokens(active) <= state.memory.budgets()["sources"]


def test_available_adjacent_ranges_cover_a_read_but_gaps_do_not():
    state = RunState()
    state.available_reads = [("file", "hash", 0, 10), ("file", "hash", 10, 20)]
    assert state.source_available("file", "hash", 0, 20)
    assert not state.source_available("file", "changed", 0, 20)
    state.available_reads[1] = ("file", "hash", 11, 20)
    assert not state.source_available("file", "hash", 0, 20)


def test_guard_outcome_survives_failed_tool_view_and_execution_ledger():
    state = RunState()
    state.files_changed = ["/workspace/app.py"]
    state.stop_reason = "repeated_available_evidence"
    state.memory.plan = {
        "steps": [{"task": "Implement remaining filter", "status": "in_progress"}],
        "next_action": "Edit the filter component",
    }
    outcome = execution_outcome(state, "sandbox-id", failed=True)
    raw = json.dumps(
        {
            "task_id": "task-id",
            "execution_outcome": outcome,
            "summary": "Stopped by no-progress guard",
            "commands": ["noisy output" * 10000],
        }
    )
    for ref in (None, "result-1"):
        view = compact_result("delegate_to_coder", raw, success=False, result_ref=ref)
        data = json.loads(view.content)
        assert data["execution_outcome"]["stop_reason"] == "repeated_available_evidence"
        assert data["execution_outcome"]["files_preserved"] is True
        assert data["execution_outcome"]["sandbox_id"] == "sandbox-id"
        assert "not a filesystem permission denial" in view.content
        assert "noisy output" not in view.content
        assert "Implement remaining filter" in view.content
    ledger = ExecutionState()
    ledger.record({"id": "call", "tool": "delegate_to_coder"}, False, raw)
    assert (
        ledger.failures["delegate_to_coder"]["coder_outcome"]["stop_reason"]
        == "repeated_available_evidence"
    )
    assert "noisy output" not in ledger.message()["content"]
