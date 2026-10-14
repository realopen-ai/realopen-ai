"""Native protocol negotiation and durable capability state, without models."""

import asyncio
import json

import httpx
import pytest

from app.agent import service
from app.agent.coder import provider as coder
from app.config import settings
from app.services import providers, tool_capabilities as capabilities


def error(status, message):
    request = httpx.Request("POST", "http://engine/api/chat")
    response = httpx.Response(status, text=message, request=request)
    return httpx.HTTPStatusError("Provider error", request=request, response=response)


@pytest.mark.parametrize(
    "status,message,reason",
    [
        (400, "model does not support tools", "unsupported_tools"),
        (422, "Function calling is not supported", "unsupported_tools"),
        (501, "Unsupported tools", "unsupported_tools"),
        (500, "Failed to parse tool call: invalid JSON", None),
        (500, "error parsing function_call", None),
        (
            500,
            "XML syntax error on line 3: element <function> closed by </parameter>",
            None,
        ),
        (400, "unsupported tool calling schema", None),
        (500, "tools not available", None),
        (500, "tools not enabled", None),
        (401, "model does not support tools", None),
        (429, "model does not support tools", None),
        (400, "invalid tool arguments: missing query", None),
        (400, "invalid tool schema", None),
        (400, "cannot validate schema for tools", None),
        (400, "unsupported schema type in tools", None),
        (500, "out of memory", None),
        (503, "service unavailable", None),
        (500, "failed to parse model configuration", None),
    ],
)
def test_only_protocol_rejections_are_rememberable(status, message, reason):
    assert capabilities.rejection_reason(error(status, message)) == reason


def test_timeout_and_connection_errors_do_not_disable_native():
    assert capabilities.rejection_reason(httpx.ConnectError("Offline")) is None
    assert capabilities.rejection_reason(httpx.ReadTimeout("Timed out")) is None
    assert capabilities.rejection_reason(ValueError("does not support tools")) is None


def test_failed_generation_text_cannot_poison_capability():
    payload = json.dumps(
        {
            "error": {
                "message": "Invalid arguments",
                "failed_generation": "model does not support tools",
            }
        }
    )
    assert capabilities.rejection_reason(error(400, payload)) is None


def test_unknown_models_are_native_first():
    assert service._model_supports_native_tools("custom-model-name:4b")


@pytest.mark.asyncio
async def test_fallback_survives_memory_reset_and_has_explicit_reset():
    await capabilities.remember_fallback("custom-model:4b", "unsupported_tools")
    capabilities._fallbacks.clear()  # simulate a new backend process
    assert not capabilities.should_try_native("custom-model:4b")
    assert capabilities.should_try_native("custom-model:9b")
    capabilities.reset("custom-model:4b")
    assert capabilities.should_try_native("custom-model:4b")


@pytest.mark.asyncio
async def test_cache_is_scoped_to_endpoint_and_provider(monkeypatch):
    await capabilities.remember_fallback("model", "unsupported_tools")
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://different-engine:11434")
    assert capabilities.should_try_native("model")
    monkeypatch.setattr(providers, "provider_of", lambda _: "groq")
    assert capabilities.should_try_native("model")


@pytest.mark.asyncio
async def test_concurrent_atomic_writes_and_no_secret_persistence(monkeypatch):
    monkeypatch.setattr(
        settings, "OLLAMA_BASE_URL", "https://user:secret-password@engine"
    )
    await asyncio.gather(
        *(
            capabilities.remember_fallback(f"model-{i % 3}", "unsupported_tools")
            for i in range(24)
        )
    )
    files = list(capabilities._directory().iterdir())
    assert len(files) == 3
    for file in files:
        text = file.read_text()
        assert json.loads(text)["protocol"] == "text"
        assert "secret-password" not in text
        assert "https://" not in text


@pytest.mark.asyncio
async def test_unwritable_state_does_not_break_runtime_fallback(monkeypatch):
    def broken(*args):
        raise PermissionError("Read only")

    monkeypatch.setattr(capabilities, "_write", broken)
    await capabilities.remember_fallback("model", "unsupported_tools")
    assert not capabilities.should_try_native("model")


def test_corrupt_cache_reprobes():
    path = capabilities._path("model")
    path.parent.mkdir(parents=True)
    path.write_text("broken json")
    assert capabilities.should_try_native("model")


@pytest.mark.parametrize(
    "message,expected",
    [
        ("XML syntax error on line 3: element <function> closed by </parameter>", True),
        ("Malformed XML in <tool_call>", True),
        ("Failed to parse tool call: invalid JSON", True),
        ("error parsing function_call", True),
        ("Malformed JSON in function arguments", True),
        ("Invalid JSON in tool_call arguments", True),
        ("XML syntax error in <document>", False),
        ("Invalid JSON model configuration", False),
        ("error parsing tool schema", False),
        ("failed to parse function_call schema", False),
        ("invalid arguments: missing query", False),
        ("model does not support tools", False),
        ("out of memory", False),
    ],
)
@pytest.mark.parametrize("stream_error", [False, True])
def test_transient_parser_errors_are_separate_from_capability(
    message, expected, stream_error
):
    exception = (
        providers.ProviderStreamError(message) if stream_error else error(500, message)
    )
    assert capabilities.native_parser_error(exception) is expected
    if expected:
        assert capabilities.rejection_reason(exception) is None


@pytest.mark.parametrize("status", [401, 403, 429])
def test_parser_words_in_auth_rate_limit_errors_are_not_retryable(status):
    assert not capabilities.native_parser_error(
        error(status, "failed to parse tool call")
    )
    assert not capabilities.native_parser_error(ValueError("failed to parse tool call"))


@pytest.mark.asyncio
async def test_old_parser_cache_is_ignored_and_new_parser_cache_is_rejected():
    path = capabilities._path("model")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"schema_version": 1, "protocol": "text", "reason": "native_parser"})
    )
    assert capabilities.should_try_native("model")
    with pytest.raises(ValueError, match="unsupported-tools"):
        await capabilities.remember_fallback("model", "native_parser")
    assert capabilities.should_try_native("model")
    await capabilities.remember_fallback("model", "unsupported_tools")
    assert not capabilities.should_try_native("model")


@pytest.mark.asyncio
async def test_coder_transient_parser_retry_success_keeps_native_for_next_request(
    monkeypatch,
):
    captured = []

    async def chat(model, messages, **kwargs):
        captured.append(kwargs)
        if len(captured) == 1:
            raise providers.ProviderStreamError(
                "XML syntax error: <function> closed by </parameter>"
            )
        return {
            "message": {
                "tool_calls": [{"function": {"name": "list_files", "arguments": {}}}]
            }
        }

    monkeypatch.setattr(coder.providers, "chat_once", chat)
    for _ in range(2):
        result = await coder.chat_with_tool_recovery(
            "custom", [{"role": "user", "content": "List"}]
        )
        assert result["message"]["tool_calls"]
    assert len(captured) == 3 and all(request["tools"] for request in captured)
    assert capabilities.should_try_native("custom")
    assert not capabilities._path("custom").exists()


@pytest.mark.asyncio
async def test_coder_repeated_parser_failures_fallback_for_execution_only(monkeypatch):
    captured = []

    async def chat(model, messages, **kwargs):
        captured.append(kwargs)
        if len(captured) <= 2:
            raise providers.ProviderStreamError(
                "failed to parse tool call: invalid JSON"
            )
        if kwargs["tools"]:
            return {"message": {"content": "Next request is native"}}
        return {"message": {"content": '{"final":"Recovered"}'}}

    monkeypatch.setattr(coder.providers, "chat_once", chat)
    state = set()
    for _ in range(2):
        result = await coder.chat_with_tool_recovery(
            "custom",
            [{"role": "user", "content": "Continue"}],
            transient_text_models=state,
        )
        assert result["message"]["content"] == "Recovered"
    assert state == {"custom"}
    await coder.chat_with_tool_recovery(
        "custom",
        [{"role": "user", "content": "New request"}],
        transient_text_models=set(),
    )
    assert [bool(request["tools"]) for request in captured] == [
        True,
        True,
        False,
        False,
        True,
    ]
    assert capabilities.should_try_native("custom")
    assert not capabilities._path("custom").exists()


@pytest.mark.asyncio
async def test_coder_remembers_and_directly_uses_json_fallback(monkeypatch):
    captured = []

    async def chat(model, messages, **kwargs):
        captured.append((messages, kwargs))
        if len(captured) == 1:
            raise error(400, "model does not support tools")
        return {"message": {"content": '{"tool":"list_files","args":{}}'}}

    monkeypatch.setattr(coder.providers, "chat_once", chat)
    original = [
        {"role": "system", "content": "Coding task rules"},
        {"role": "user", "content": "List files"},
    ]
    for _ in range(2):
        result = await coder.chat_with_tool_recovery("custom", original)
        assert result["message"]["tool_calls"][0]["function"]["name"] == "list_files"
    assert len(captured) == 3
    assert captured[0][1]["tools"]
    assert all(
        kwargs["tools"] is None and kwargs["format"] == "json"
        for _, kwargs in captured[1:]
    )
    assert any(
        "Native function calling is unavailable" in message["content"]
        for message in captured[2][0]
    )
    assert captured[2][0][0]["content"] == original[0]["content"]
    assert original[0]["content"] == "Coding task rules"
    assert not service._model_supports_native_tools(
        "custom"
    )  # shared with general agent


@pytest.mark.asyncio
async def test_coder_generic_server_error_recovers_without_caching(monkeypatch):
    attempted = []

    async def chat(model, messages, **kwargs):
        attempted.append(kwargs)
        if len(attempted) % 2:
            raise error(500, "Temporary internal error")
        return {"message": {"content": '{"final":"Done"}'}}

    monkeypatch.setattr(coder.providers, "chat_once", chat)
    for _ in range(2):
        await coder.chat_with_tool_recovery(
            "custom", [{"role": "user", "content": "Task"}]
        )
    assert [bool(call["tools"]) for call in attempted] == [True, False, True, False]
    assert capabilities.should_try_native("custom")


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 429])
async def test_coder_does_not_retry_auth_or_rate_limit(monkeypatch, status):
    count = 0

    async def chat(*args, **kwargs):
        nonlocal count
        count += 1
        raise error(status, "Access error")

    monkeypatch.setattr(coder.providers, "chat_once", chat)
    with pytest.raises(httpx.HTTPStatusError):
        await coder.chat_with_tool_recovery(
            "model", [{"role": "user", "content": "Task"}]
        )
    assert count == 1
    assert capabilities.should_try_native("model")
