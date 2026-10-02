import asyncio

import pytest

from app.services.chat_streams import ActiveChatStream, start_stream, stop_stream


@pytest.mark.asyncio
async def test_stream_continues_after_subscriber_disconnects():
    release = asyncio.Event()

    async def source():
        yield 'data: {"event":"message","message":{"content":"one"}}\n\n'
        await release.wait()
        yield 'data: {"event":"done"}\n\n'
        yield "data: [DONE]\n\n"

    stream = await start_stream("disconnect-test", source())
    subscriber = stream.subscribe()
    first = await anext(subscriber)
    assert first.startswith("id: 1\n")
    await subscriber.aclose()

    release.set()
    await asyncio.wait_for(stream.task, timeout=1)
    assert stream.done is True
    assert len(stream.events) == 3


@pytest.mark.asyncio
async def test_explicit_stop_cancels_the_backend_owned_task():
    interrupted = asyncio.Event()

    async def source():
        try:
            yield 'data: {"event":"message","message":{"content":"partial"}}\n\n'
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            interrupted.set()
            yield 'data: {"event":"interrupted"}\n\n'
            yield "data: [DONE]\n\n"

    stream = await start_stream("stop-test", source())
    await asyncio.sleep(0)
    assert await stop_stream("stop-test") is True
    await asyncio.wait_for(stream.task, timeout=1)

    assert interrupted.is_set()
    assert any('"interrupted"' in event for event in stream.events)
    assert stream.events[-1].endswith("data: [DONE]\n\n")


@pytest.mark.asyncio
async def test_generation_done_tracks_persisted_reconnect_checkpoint():
    stream = ActiveChatStream(conversation_id="checkpoint")
    await stream.publish('data: {"event":"message","message":{"content":"# Hi"}}\n\n')
    await stream.publish(
        'data: {"event":"generation_done","generationDuration":1}\n\n'
    )
    await stream.publish(
        'data: {"event":"tool_call","tool_call":{"id":"1"}}\n\n'
    )
    await stream.finish()

    assert stream.persisted_through == 2
    resumed = [event async for event in stream.subscribe(stream.persisted_through)]
    assert len(resumed) == 1
    assert '"event":"tool_call"' in resumed[0]
