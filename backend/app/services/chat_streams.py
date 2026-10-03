"""Connection-independent buffering for live chat SSE streams."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import AsyncIterator


@dataclass
class ActiveChatStream:
    conversation_id: str
    events: list[str] = field(default_factory=list)
    # Sequence number of the latest generation_done event. chat.py persists
    # the assistant snapshot before yielding that event.
    persisted_through: int = 0
    done: bool = False
    cancel_requested: bool = False
    task: asyncio.Task | None = None
    finished_at: float | None = None
    condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    async def publish(self, chunk: str) -> None:
        async with self.condition:
            sequence = len(self.events) + 1
            framed = f"id: {sequence}\n{chunk}" if chunk.startswith("data:") else chunk
            self.events.append(framed)
            if chunk.startswith("data: "):
                payload = chunk.split("\n", 1)[0][6:]
                try:
                    if json.loads(payload).get("event") == "generation_done":
                        self.persisted_through = sequence
                except (json.JSONDecodeError, AttributeError):
                    pass
            self.condition.notify_all()

    async def finish(self) -> None:
        async with self.condition:
            self.done = True
            self.finished_at = time.monotonic()
            self.condition.notify_all()

    async def subscribe(self, after: int = 0) -> AsyncIterator[str]:
        cursor = max(0, after)
        while True:
            async with self.condition:
                while cursor >= len(self.events) and not self.done:
                    await self.condition.wait()
                batch = self.events[cursor:]
                finished = self.done
            for event in batch:
                cursor += 1
                yield event
            if finished and cursor >= len(self.events):
                return


_active: dict[str, ActiveChatStream] = {}


async def start_stream(
    conversation_id: str, source: AsyncIterator[str]
) -> ActiveChatStream:
    previous = _active.get(conversation_id)
    if previous and not previous.done and previous.task:
        previous.cancel_requested = True
        previous.task.cancel()

    stream = ActiveChatStream(conversation_id=conversation_id)
    _active[conversation_id] = stream

    async def pump() -> None:
        try:
            async for chunk in source:
                await stream.publish(chunk)
        finally:
            await stream.finish()

    stream.task = asyncio.create_task(pump())
    return stream


def get_stream(conversation_id: str) -> ActiveChatStream | None:
    stream = _active.get(conversation_id)
    if (
        stream
        and stream.finished_at is not None
        and time.monotonic() - stream.finished_at > 600
    ):
        _active.pop(conversation_id, None)
        return None
    return stream


async def stop_stream(conversation_id: str) -> bool:
    stream = _active.get(conversation_id)
    if not stream or stream.done or not stream.task:
        return False
    stream.cancel_requested = True
    stream.task.cancel()
    return True
