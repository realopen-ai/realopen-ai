"""Run a user-requested continuation through the existing chat SSE/persistence path."""

import asyncio
import json
import time
import uuid

from app.agent.coder.agent import CoderAgent


def frame(event, **payload):
    return "data: " + json.dumps({"event": event, **payload}) + "\n\n"


async def resume_coder_stream(conversation_id, task_id):
    parent_id = "tc-coder-resume-" + uuid.uuid4().hex
    started = int(time.time() * 1000)
    parent = {
        "id": parent_id,
        "type": "sandbox",
        "status": "running",
        "title": "Resuming coding task",
        "startedAt": started,
    }
    yield frame("tool_call", tool_call=parent)
    queue = asyncio.Queue()
    execution = asyncio.create_task(
        CoderAgent().execute(
            task="Resume the saved coding task",
            conversation_id=conversation_id,
            _resume_task_id=task_id,
            _event_queue=queue,
            _parent_tool_call_id=parent_id,
        )
    )
    try:
        while not execution.done() or not queue.empty():
            try:
                event = await asyncio.wait_for(queue.get(), timeout=0.1)
            except TimeoutError:
                continue
            if event.get("event"):
                yield frame(
                    event["event"],
                    **{key: value for key, value in event.items() if key != "event"}
                )
            else:
                yield frame("tool_call", tool_call=event)
        result = await execution
        yield frame(
            "tool_call",
            tool_call={
                **parent,
                "status": "completed" if result.success else "error",
                "output": result.output,
                "completedAt": int(time.time() * 1000),
            },
        )
        data = json.loads(result.output)
        yield frame(
            "message",
            message={
                "role": "assistant",
                "content": data.get("summary", "Coding task finished."),
            },
        )
        yield frame(
            "generation_done", generationDuration=(time.time() * 1000 - started) / 1000
        )
    finally:
        if not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
