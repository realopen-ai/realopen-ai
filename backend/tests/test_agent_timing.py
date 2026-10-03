from app.agent.base import ToolCall, ToolType
from app.agent.service import _tool_call_to_dict, _tool_call_to_update_dict


def test_tool_sse_timestamps_are_epoch_milliseconds():
    call = ToolCall(
        id="tool-1",
        type=ToolType.CODE_EXEC,
        name="use_code_exec",
        started_at=1_700_000_000.125,
        completed_at=1_700_000_001.375,
    )

    initial = _tool_call_to_dict(call)
    update = _tool_call_to_update_dict(call)

    assert initial["startedAt"] == 1_700_000_000_125
    assert initial["completedAt"] == 1_700_000_001_375
    assert update["completedAt"] == initial["completedAt"]
