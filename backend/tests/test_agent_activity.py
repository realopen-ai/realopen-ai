"""Live activity, eligibility, cancellation and replay without real inference."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.api import chat
from app.agent import service
from app.agent.base import ToolResult, ToolRegistry
from app.agent.coder import provider as coder_provider
from app.config import settings
from app.services import context_compactor as cc, conversation_memory as cm
from app.services import chat_streams
from app.services.agent_activity import activity_events
from app.services.chat_streams import start_stream
from test_agent_protocol import agent as agent_fixture, run, script, call


@pytest.fixture
def agent(monkeypatch):
    return agent_fixture.__wrapped__(monkeypatch)


@pytest.mark.asyncio
async def test_activity_is_visible_before_operation_finishes():
    release = asyncio.Event()

    async def operation(on_start):
        on_start()
        on_start()  # One spinner per operation, including multiple summary batches.
        await release.wait()
        return "summary"

    events = activity_events(operation, "context_compaction")
    assert await asyncio.wait_for(anext(events), 1) == {
        "event": "agent_activity",
        "activity": "context_compaction",
        "active": True,
    }
    release.set()
    assert (await anext(events))["active"] is False
    assert await anext(events) == {"result": "summary"}
    with pytest.raises(StopAsyncIteration):
        await anext(events)


@pytest.mark.asyncio
async def test_skipped_operation_does_not_show_a_fake_spinner():
    async def operation(on_start):
        return False

    assert [
        item async for item in activity_events(operation, "conversation_summary")
    ] == [{"result": False}]


@pytest.mark.asyncio
async def test_failure_clears_activity_then_preserves_the_original_error():
    async def operation(on_start):
        on_start()
        raise ValueError("compaction failed")

    events = activity_events(operation, "context_compaction")
    assert (await anext(events))["active"] is True
    assert (await anext(events))["active"] is False
    with pytest.raises(ValueError, match="compaction failed"):
        await anext(events)


@pytest.mark.asyncio
async def test_closing_activity_stream_cancels_child_inference():
    cancelled = asyncio.Event()

    async def operation(on_start):
        on_start()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    events = activity_events(operation, "context_compaction")
    await anext(events)
    await events.aclose()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_compactor_announces_only_real_summarization(monkeypatch):
    monkeypatch.setattr(settings, "AGENT_CONTEXT_WINDOW", 4096)
    monkeypatch.setattr(settings, "AGENT_CONTEXT_WINDOWS", {})
    monkeypatch.setattr(settings, "AGENT_OUTPUT_RESERVE_TOKENS", 512)
    monkeypatch.setattr(settings, "AGENT_CONTEXT_MARGIN_TOKENS", 256)
    summarize = AsyncMock(return_value="Compact history summary.")
    monkeypatch.setattr(cc, "_summarize_bounded", summarize)
    calls = []
    small = [{"role": "user", "content": "Hello"}]
    await cc.compact_conversation(small, "model", on_start=lambda: calls.append(True))
    assert not calls
    large = (
        [{"role": "system", "content": "Instructions"}]
        + [
            {"role": "user" if i % 2 == 0 else "assistant", "content": "history " * 600}
            for i in range(8)
        ]
        + [
            {
                "role": "user",
                "content": "Current request",
                "metadata": {"agent_request": True},
            }
        ]
    )
    _, compacted = await cc.compact_conversation(
        large, "model", on_start=lambda: calls.append(True)
    )
    assert compacted and calls == [True]
    assert summarize.await_count > 0


@pytest.mark.asyncio
async def test_agent_forwards_compaction_activity_without_changing_rounds(
    agent, monkeypatch
):
    async def compact(messages, model, **kwargs):
        kwargs["on_start"]()
        await asyncio.sleep(0)
        return messages, False

    monkeypatch.setattr(service, "compact_conversation", compact)
    script(monkeypatch, [[{"content": "Hello", "done": True}]])
    events = await run()
    activity = [event for event in events if event["event"] == "agent_activity"]
    assert [item["active"] for item in activity] == [True, False]
    assert events.index(activity[-1]) < next(
        i for i, event in enumerate(events) if event["event"] == "message"
    )
    assert events[-1]["event"] == "done"


@pytest.mark.asyncio
async def test_recoverable_summary_timeout_keeps_same_sse_subscription_through_later_rounds(
    agent, monkeypatch
):
    monkeypatch.setattr(settings, "AGENT_CONTEXT_WINDOW", 12000)
    monkeypatch.setattr(settings, "CONTEXT_COMPACT_THRESHOLD", 0.5)
    monkeypatch.setattr(chat_streams, "HEARTBEAT_SECONDS", 0.01)
    release = asyncio.Event()

    async def timed_out_summary(*args, **kwargs):
        await release.wait()
        # _llm_summarize returns None after catching a provider ReadTimeout.
        return None

    monkeypatch.setattr(cc, "_llm_summarize", timed_out_summary)
    script(
        monkeypatch,
        [
            [{"tool_calls": [call({"input": "after timeout"})], "done": True}],
            [{"content": "Finished after recovery", "done": True}],
        ],
    )
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": "x" * 2000}
        for i in range(12)
    ]
    messages = history + [{"role": "user", "content": "Use echo"}]
    stream = await start_stream(
        "summary-timeout-live", service.run_agent_stream(messages, model="local")
    )
    subscriber = stream.subscribe()
    initial = await asyncio.wait_for(anext(subscriber), 1)
    assert '"active": true' in initial
    assert await asyncio.wait_for(anext(subscriber), 1) == ": keep-alive\n\n"
    assert not stream.done
    release.set()
    rest = [event async for event in subscriber]
    assert agent.executed == [{"input": "after timeout"}]
    assert any("Finished after recovery" in event for event in rest)
    assert not any('"event": "error"' in event for event in rest)
    assert stream.done


@pytest.mark.asyncio
async def test_memory_summary_events_are_live_and_only_when_eligible(monkeypatch):
    db = SimpleNamespace(
        get=AsyncMock(return_value=SimpleNamespace(summary=None, summary_at=None)),
        execute=AsyncMock(
            return_value=SimpleNamespace(
                scalars=lambda: SimpleNamespace(
                    all=lambda: [SimpleNamespace(role="user", content="Text")] * 4
                )
            )
        ),
        commit=AsyncMock(),
    )

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(chat, "async_session_factory", session)
    monkeypatch.setattr(settings, "CONVERSATION_SUMMARY_MIN_MESSAGES", 4)
    release = asyncio.Event()

    async def summarize(*args):
        await release.wait()
        return "Summary"

    monkeypatch.setattr(cm, "summarize_conversation", summarize)
    monkeypatch.setattr(cm, "_store_summary", AsyncMock(return_value=True))
    events = chat._stream_conversation_summary("conversation", "model")
    first = json.loads((await asyncio.wait_for(anext(events), 1))[6:])
    assert first == {
        "event": "agent_activity",
        "activity": "conversation_summary",
        "active": True,
    }
    db.commit.assert_not_awaited()
    release.set()
    last = json.loads((await anext(events))[6:])
    assert last["active"] is False
    assert [item async for item in events] == []
    db.commit.assert_awaited_once()
    db.get.return_value = None
    assert [
        item async for item in chat._stream_conversation_summary("missing", "model")
    ] == []


@pytest.mark.asyncio
async def test_activity_events_replay_after_frontend_reload():
    release = asyncio.Event()

    async def source():
        yield 'data: {"event":"agent_activity","activity":"context_compaction","active":true}\n\n'
        await release.wait()
        yield 'data: {"event":"agent_activity","activity":"context_compaction","active":false}\n\n'

    stream = await start_stream("activity-test", source())
    initial = stream.subscribe()
    assert '"active":true' in await asyncio.wait_for(anext(initial), 1)
    await initial.aclose()
    resumed = stream.subscribe()
    assert '"active":true' in await anext(resumed)
    release.set()
    assert '"active":false' in await anext(resumed)
    assert [item async for item in resumed] == []


@pytest.mark.asyncio
async def test_coder_fallback_reports_compaction_without_changing_provider_args(
    monkeypatch,
):
    async def compact(messages, model, **kwargs):
        kwargs["on_start"]()
        return messages, True

    monkeypatch.setattr(coder_provider, "compact_conversation", compact)
    infer = AsyncMock(return_value={"message": {"content": '{"final":"Done"}'}})
    monkeypatch.setattr(coder_provider.providers, "chat_once", infer)
    events = []
    await coder_provider._chat_attempt(
        "local",
        [{"role": "user", "content": "Task"}],
        tools=None,
        recovery=True,
        on_activity=events.append,
    )
    assert [event["active"] for event in events] == [True, False]
    assert "on_activity" not in infer.call_args.kwargs


@pytest.mark.asyncio
async def test_delegated_coder_activity_keeps_its_event_type(agent, monkeypatch):
    agent.name = "delegate_to_coder"
    registry = ToolRegistry()
    registry.register(agent)
    monkeypatch.setattr(service, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(service, "_select_tools", lambda _: {agent.name})

    async def execute(**kwargs):
        queue = kwargs["_event_queue"]
        queue.put_nowait(
            {
                "event": "agent_activity",
                "activity": "context_compaction",
                "active": True,
            }
        )
        await asyncio.sleep(0)
        queue.put_nowait(
            {
                "event": "agent_activity",
                "activity": "context_compaction",
                "active": False,
            }
        )
        return ToolResult(True, "Done")

    monkeypatch.setattr(agent, "execute", execute)
    script(
        monkeypatch,
        [
            [{"tool_calls": [call({"input": "Task"}, name=agent.name)], "done": True}],
            [{"content": "Done", "done": True}],
        ],
    )
    events = await run()
    activity = [event for event in events if event["event"] == "agent_activity"]
    assert [event["active"] for event in activity] == [True, False]
    assert all(
        "event" not in event["tool_call"]
        for event in events
        if event["event"] == "tool_call"
    )


@pytest.mark.asyncio
async def test_coder_receives_original_request_and_thinking_is_forwarded_as_sse(
    agent, monkeypatch
):
    agent.name = "delegate_to_coder"
    registry = ToolRegistry()
    registry.register(agent)
    monkeypatch.setattr(service, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(service, "_select_tools", lambda _: {agent.name})
    original = "Build a FastAPI API with SQLite persistence and tests."
    thinking = [
        {
            "event": "thinking_start",
            "agent": "coder",
            "round": 1,
            "model": "ornith-1.5:9b",
        },
        {
            "event": "thinking",
            "thinking": "SQLite persistence is an explicit requirement.",
        },
        {"event": "thinking_done", "thinkingDuration": 0.5},
        {
            "event": "persistence_checkpoint",
            "agent": "coder",
            "round": 1,
        },
    ]

    async def execute(**kwargs):
        assert kwargs["_user_request"] == original
        assert kwargs["input"] == "Build an in-memory API on port 8000"
        for event in thinking:
            kwargs["_event_queue"].put_nowait(event)
        return ToolResult(True, "Done")

    monkeypatch.setattr(agent, "execute", execute)
    script(
        monkeypatch,
        [
            [
                {
                    "tool_calls": [
                        call(
                            {"input": "Build an in-memory API on port 8000"},
                            name=agent.name,
                        )
                    ],
                    "done": True,
                }
            ],
            [{"content": "Done", "done": True}],
        ],
    )
    events = await run(messages=[{"role": "user", "content": original}])
    forwarded = [event for event in events if event["event"].startswith("thinking")]
    assert forwarded == thinking[:-1]
    checkpoints = [
        event for event in events if event["event"] == "persistence_checkpoint"
    ]
    assert checkpoints[0] == thinking[-1]
    assert checkpoints[-1]["phase"] == "completed"
    assert all(
        "event" not in event["tool_call"]
        for event in events
        if event["event"] == "tool_call"
    )
