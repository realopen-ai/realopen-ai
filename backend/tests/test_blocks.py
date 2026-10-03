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
