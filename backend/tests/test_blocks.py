import time

from app.services.blocks import BlockBuilder


def test_finish_open_thinking_finalizes_interrupted_block():
    builder = BlockBuilder()
    builder.on_thinking_start()
    builder.on_thinking_token("partial thought")
    builder._thinking_started_at = time.monotonic() - 2

    builder.finish_open_thinking()

    assert builder.blocks == [
        {"type": "thinking", "content": "partial thought", "duration": 2}
    ]


def test_finish_open_thinking_preserves_completed_duration():
    builder = BlockBuilder()
    builder.on_thinking_start()
    builder.on_thinking_done(7)

    builder.finish_open_thinking()

    assert builder.blocks[0]["duration"] == 7


def test_terminal_only_tool_error_is_persisted_between_thinking_blocks():
    builder = BlockBuilder()
    builder.on_thinking_start()
    builder.on_thinking_token("First attempt")
    builder.on_thinking_done(2)
    rejected = {
        "type": "quizzes",
        "status": "error",
        "title": "Invalid arguments for create_quiz",
        "error": "questions must be an array",
        "startedAt": 1700000000000,
        "completedAt": 1700000000000,
        "durationMs": 0,
    }
    builder.on_tool_call_update("rejected", rejected)
    builder.on_tool_call_update("rejected", rejected)
    builder.on_thinking_start()
    builder.on_thinking_token("Repair")
    blocks = builder.to_db_blocks()
    assert [b["type"] for b in blocks] == ["thinking", "tool_call", "thinking"]
    assert blocks[1]["tool_call"] == {"id": "rejected", **rejected}


def test_progress_only_update_does_not_invent_a_tool_block():
    builder = BlockBuilder()
    builder.on_tool_call_update("missing", {"progress": {"completed": 1}})
    assert builder.to_db_blocks() == []


def test_coder_thinking_metadata_survives_persistence_without_becoming_answer_text():
    builder = BlockBuilder()
    context = {
        "agent": "coder",
        "model": "ornith-1.5:9b",
        "round": 2,
        "parentId": "task",
    }
    builder.on_thinking_start(context)
    builder.on_thinking_token("Inspect the existing routes first.")
    builder.on_thinking_done(1.25)
    builder.on_thinking_start()
    builder.on_thinking_token("General assistant resumes.")
    builder.on_thinking_done(2)
    blocks = builder.to_db_blocks()
    assert blocks[0] == {
        "type": "thinking",
        "content": "Inspect the existing routes first.",
        "duration": 1.25,
        **context,
    }
    assert "agent" not in blocks[1]
    assert builder.get_text_content() == ""
