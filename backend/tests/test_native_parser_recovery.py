"""Transient native parser recovery, without durable model capability changes."""

from copy import deepcopy

import pytest

from app.agent import service
from app.services import providers, tool_capabilities
from test_agent_protocol import agent as agent_fixture, call, run


@pytest.fixture
def agent(monkeypatch):
    return agent_fixture.__wrapped__(monkeypatch)


@pytest.mark.asyncio
async def test_xml_retry_succeeds_natively_and_next_request_stays_native(
    agent, monkeypatch
):
    captured = []

    async def stream(model, messages, **kwargs):
        captured.append((deepcopy(messages), kwargs))
        if len(captured) == 1:
            yield {"thinking": "Read first"}
            yield {"content": "I'll read the file first."}
            # Even a completed call in an incomplete stream must not execute.
            yield {"tool_calls": [call({"input": "discarded"})]}
            raise providers.ProviderStreamError(
                "XML syntax error on line 3: element <function> closed by </parameter>"
            )
        if len(captured) == 2:
            yield {
                "content": "I'll read the file first.",
                "tool_calls": [call({"input": "selected"})],
                "done": True,
            }
        else:
            yield {"content": "Done", "done": True}

    monkeypatch.setattr(service.providers, "stream_chat", stream)
    events = await run()
    await run()
    assert len(captured) == 4
    assert all(kwargs["tools"] for _, kwargs in captured)
    assert agent.executed == [{"input": "selected"}]
    visible = "".join(
        event["message"]["content"] for event in events if event["event"] == "message"
    )
    assert visible.count("I'll read the file first.") == 1
    assert not any(event["event"] == "error" for event in events)
    assert any(
        "Successful actions from earlier rounds remain completed" in message["content"]
        for message in captured[1][0]
    )
    assert not any(
        "The previous native tool-call attempt failed" in message["content"]
        for message in captured[2][0]
    )
    assert captured[0][0][0]["content"] == captured[1][0][0]["content"]
    assert tool_capabilities.should_try_native("qwen3:4b")
    assert not tool_capabilities._path("qwen3:4b").exists()


@pytest.mark.asyncio
async def test_repeated_parser_failure_uses_text_for_execution_not_next_request(
    agent, monkeypatch
):
    captured = []

    async def stream(model, messages, **kwargs):
        captured.append((deepcopy(messages), kwargs))
        if len(captured) <= 2:
            yield {"content": "Checking."}
            raise providers.ProviderStreamError(
                "failed to parse tool call: invalid JSON"
            )
        if len(captured) == 3:
            yield {
                "content": 'Checking.\n```tool\n{"tool":"echo","args":{"input":"selected"}}\n```',
                "done": True,
            }
        else:
            yield {"content": "Done", "done": True}

    monkeypatch.setattr(service.providers, "stream_chat", stream)
    events = await run()
    assert events[-1]["event"] == "done"
    assert tool_capabilities.should_try_native("qwen3:4b")
    await run()
    assert [bool(kwargs["tools"]) for _, kwargs in captured] == [
        True,
        True,
        False,
        False,
        True,
    ]
    assert agent.executed == [{"input": "selected"}]
    visible = "".join(
        event["message"]["content"] for event in events if event["event"] == "message"
    )
    assert visible.count("Checking.") == 1 and "```tool" not in visible
    assert all(message["role"] != "tool" for message in captured[2][0])
    assert not tool_capabilities._path("qwen3:4b").exists()


@pytest.mark.asyncio
async def test_retry_final_answer_continues_partial_text_without_replaying_it(
    agent, monkeypatch
):
    count = 0

    async def stream(*args, **kwargs):
        nonlocal count
        count += 1
        if count == 1:
            yield {"content": "The answer is "}
            raise providers.ProviderStreamError("error parsing function_call")
        yield {"content": "The answer is 42.", "done": True}

    monkeypatch.setattr(service.providers, "stream_chat", stream)
    events = await run()
    text = "".join(e["message"]["content"] for e in events if e["event"] == "message")
    assert text.count("The answer is ") == 1 and "42." in text
    assert text.strip() == "The answer is 42."
    assert count == 2 and events[-1]["event"] == "done"


@pytest.mark.asyncio
async def test_text_mode_does_not_retry_native_parser_diagnostics(agent, monkeypatch):
    monkeypatch.setattr(service, "_model_supports_native_tools", lambda _: False)
    attempts = []

    async def stream(*args, **kwargs):
        attempts.append(kwargs)
        raise providers.ProviderStreamError("error parsing function_call")
        yield

    monkeypatch.setattr(service.providers, "stream_chat", stream)
    events = await run()
    assert len(attempts) == 1 and attempts[0]["tools"] is None
    assert events[-1]["event"] == "error"
    assert not agent.executed
