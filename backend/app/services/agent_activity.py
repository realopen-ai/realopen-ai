"""Stream transient activity only while an eligible operation actually runs."""

import asyncio
from contextlib import suppress


async def activity_events(operation, activity):
    """Run operation(on_start), yielding activity events then its result.

    Eligibility checks can return without announcing activity. Closing or
    cancelling the stream also cancels the operation; no detached inference.
    """
    queue = asyncio.Queue()
    started = False

    def on_start():
        nonlocal started
        if not started:
            started = True
            queue.put_nowait(
                {"event": "agent_activity", "activity": activity, "active": True}
            )

    task = asyncio.create_task(operation(on_start))
    task.add_done_callback(lambda _: queue.put_nowait(None))
    try:
        while (event := await queue.get()) is not None:
            yield event
        try:
            result = await task
        finally:
            if started:
                yield {"event": "agent_activity", "activity": activity, "active": False}
        yield {"result": result}
    finally:
        if not task.done():
            task.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await task
