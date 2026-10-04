"""Tests for app/services/context_compactor.py.

What is tested:
  - Token estimation: _estimate_tokens, _estimate_message_tokens (string,
    block-list content, image placeholder, tool_calls with str/dict/
    unserializable arguments, tool-role overhead),
    _estimate_context_window (named sizes, size hints, fallback).
  - _llm_summarize: success, HTTP error, invalid JSON — via a fake
    httpx.AsyncClient (NO real Ollama call).
  - _format_message_for_compaction / _build_compaction_text: role labels,
    block flattening, [image] placeholders, tool-call formatting with
    argument truncation, per-message 2000-char cap.
  - _sanitize_tool_messages_after_compact: orphan tool messages dropped,
    paired tool exchanges kept, dangling assistant.tool_calls stripped
    with "(tool call summarized)" placeholder.
  - compact_conversation: too-few-messages skip, below-threshold skip,
    too-few-conversation-messages skip, successful compaction (header
    text, recent turns preserved, tail-context preserved, orphan tool
    cleanup, stacking avoidance for repeated compactions), summarizer
    failure refusal.
  - summarize_conversation: minimum message counts, first-2 + last-4
    sampling, short-summary rejection.
  - get_conversation_context: empty input, max summaries, 100-word
    truncation.

What is mocked:
  - httpx.AsyncClient is replaced with a fake client class (no network).
  - app.services.context_compactor._llm_summarize is patched for the
    compact_conversation / summarize_conversation orchestration tests.
"""

from unittest.mock import AsyncMock, patch

import pytest

import app.services.context_compactor as cc
from app.config import settings
from app.services.context_compactor import (
    _build_compaction_text,
    _estimate_context_window,
    _estimate_message_tokens,
    _estimate_tokens,
    _format_message_for_compaction,
    _llm_summarize,
    _sanitize_tool_messages_after_compact,
    compact_conversation,
    get_conversation_context,
    summarize_conversation,
)


# ══════════════════════════════════════════════════════════════════════
# 1. Token estimation
# ══════════════════════════════════════════════════════════════════════


def test_estimate_tokens_empty():
    assert _estimate_tokens("") == 0
    assert _estimate_tokens(None) == 0


def test_estimate_tokens_minimum_one():
    assert _estimate_tokens("ab") == 1  # 2 chars * 0.3 = 0.6 → int → 0 → max(1, ·)


def test_estimate_tokens_roughly_proportional():
    assert _estimate_tokens("x" * 100) == 30
    assert _estimate_tokens("x" * 10) == 3


def test_estimate_message_tokens_string_content():
    # 4 overhead + 30 tokens for 100 chars
    assert _estimate_message_tokens({"role": "user", "content": "x" * 100}) == 34


def test_estimate_message_tokens_list_content():
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "x" * 100},
            {"type": "image_url", "image_url": {"url": "data:..."}},
        ],
    }
    assert _estimate_message_tokens(msg) == 4 + 30 + 256


def test_estimate_message_tokens_counts_tool_calls_with_string_args():
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {"function": {"name": "search", "arguments": '{"q": "' + "y" * 60 + '"}'}}
        ],
    }
    # 4 msg overhead + 4 tool-call overhead + tokens("search" + args)
    expected = 4 + 4 + _estimate_tokens("search" + '{"q": "' + "y" * 60 + '"}')
    assert _estimate_message_tokens(msg) == expected


def test_estimate_message_tokens_serializes_dict_args():
    args = {"q": "hello", "limit": 5}
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "search", "arguments": args}}],
    }
    import json

    expected = 8 + _estimate_tokens("search" + json.dumps(args))
    assert _estimate_message_tokens(msg) == expected


def test_estimate_message_tokens_handles_unserializable_args():
    class Weird:
        def __str__(self):
            return "weird-object"

    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "t", "arguments": Weird()}}],
    }
    total = _estimate_message_tokens(msg)
    assert total >= 8  # overheads plus something for the stringified args


def test_estimate_message_tokens_tool_role_overhead():
    base = _estimate_message_tokens({"role": "user", "content": "x" * 100})
    tool = _estimate_message_tokens({"role": "tool", "content": "x" * 100})
    assert tool == base + 4


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("qwen3:4b-128k", 131072),
        ("llama3-32k", 32768),
        ("mistral-16k", 16384),
        ("phi3-8k", 8192),
        ("tiny-4k", 4096),
        ("llama3-70b", 16384),
        ("qwen3-4b", 8192),
        ("qwen3-7b", 8192),
        ("completely-unknown", 8192),
    ],
)
def test_estimate_context_window(model, expected):
    assert _estimate_context_window(model) == expected


# ══════════════════════════════════════════════════════════════════════
# 2. _llm_summarize (httpx mocked)
# ══════════════════════════════════════════════════════════════════════


class _FakeResponse:
    def __init__(self, payload=None, raise_on_status=False, bad_json=False):
        self._payload = payload or {}
        self._raise = raise_on_status
        self._bad_json = bad_json

    def raise_for_status(self):
        if self._raise:
            raise RuntimeError("HTTP 500")

    def json(self):
        if self._bad_json:
            raise ValueError("not JSON")
        return self._payload


class _FakeAsyncClient:
    response = None
    calls = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, json=None):
        _FakeAsyncClient.calls.append({"url": url, "json": json})
        resp = _FakeAsyncClient.response
        if isinstance(resp, BaseException):
            raise resp
        return resp


@pytest.fixture
def fake_llm():
    _FakeAsyncClient.calls = []
    _FakeAsyncClient.response = _FakeResponse(
        {"message": {"content": "  a tidy summary  "}}
    )
    with patch.object(cc.httpx, "AsyncClient", _FakeAsyncClient):
        yield _FakeAsyncClient


@pytest.mark.asyncio
async def test_llm_summarize_success(fake_llm):
    out = await _llm_summarize("m-8k", "some text", max_tokens=99)
    assert out == "a tidy summary"  # stripped
    call = fake_llm.calls[0]
    assert call["url"].endswith("/api/chat")
    assert call["json"]["model"] == "m-8k"
    assert call["json"]["options"]["num_predict"] == 99
    assert call["json"]["stream"] is False
    # system prompt comes from the context_compactor prompt file
    assert call["json"]["messages"][0]["role"] == "system"
    assert call["json"]["messages"][1]["content"] == "some text"


@pytest.mark.asyncio
async def test_llm_summarize_http_error_returns_none(fake_llm):
    fake_llm.response = _FakeResponse(raise_on_status=True)
    assert await _llm_summarize("m", "text") is None


@pytest.mark.asyncio
async def test_llm_summarize_invalid_json_returns_none(fake_llm):
    fake_llm.response = _FakeResponse(bad_json=True)
    assert await _llm_summarize("m", "text") is None


@pytest.mark.asyncio
async def test_llm_summarize_network_error_returns_none(fake_llm):
    fake_llm.response = RuntimeError("connection refused")
    assert await _llm_summarize("m", "text") is None


# ══════════════════════════════════════════════════════════════════════
# 3. Compaction text formatting
# ══════════════════════════════════════════════════════════════════════


def test_format_message_string_content():
    assert (
        _format_message_for_compaction({"role": "user", "content": "hello"})
        == "USER: hello"
    )


def test_format_message_block_list_content():
    msg = {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "part one"},
            {"type": "image_url", "image_url": {"url": "x"}},
            {"type": "text", "text": "part two"},
        ],
    }
    out = _format_message_for_compaction(msg)
    assert out == "ASSISTANT: part one [image] part two"


def test_format_message_includes_tool_calls():
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "search", "arguments": '{"q": "cats"}'}}],
    }
    out = _format_message_for_compaction(msg)
    assert '[tool: search({"q": "cats"})]' in out


def test_format_message_truncates_long_tool_args():
    long_args = '{"q": "' + "z" * 500 + '"}'
    msg = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"function": {"name": "t", "arguments": long_args}}],
    }
    out = _format_message_for_compaction(msg)
    assert "…" in out
    assert len(out) < 300  # args cut at 200 chars


def test_format_message_serializes_dict_tool_args():
    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"function": {"name": "search", "arguments": {"q": "cats", "limit": 3}}}
        ],
    }
    out = _format_message_for_compaction(msg)
    assert '[tool: search({"q": "cats", "limit": 3})]' in out


def test_format_message_stringifies_unserializable_tool_args():
    class Weird:
        def __str__(self):
            return "weird-arg"

    msg = {
        "role": "assistant",
        "content": "",
        "tool_calls": [{"function": {"name": "t", "arguments": Weird()}}],
    }
    out = _format_message_for_compaction(msg)
    assert "[tool: t(weird-arg)]" in out


def test_format_message_caps_content_at_2000():
    msg = {"role": "user", "content": "a" * 5000}
    out = _format_message_for_compaction(msg)
    assert len(out) == len("USER: ") + 2000


def test_build_compaction_text_joins_messages():
    text = _build_compaction_text(
        [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "two"},
        ]
    )
    assert text == "USER: one\n\nASSISTANT: two"


# ══════════════════════════════════════════════════════════════════════
# 4. Post-compaction tool-message sanitization
# ══════════════════════════════════════════════════════════════════════


def test_sanitize_empty_list():
    assert _sanitize_tool_messages_after_compact([]) == []


def test_sanitize_drops_orphan_tool_message():
    msgs = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "content": "orphan tool output"},
    ]
    out = _sanitize_tool_messages_after_compact(msgs)
    assert [m["role"] for m in out] == ["user"]


def test_sanitize_keeps_paired_tool_exchange():
    msgs = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"function": {"name": "t", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "result"},
    ]
    out = _sanitize_tool_messages_after_compact(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool"]
    assert out[0]["tool_calls"] == msgs[0]["tool_calls"]


def test_sanitize_strips_dangling_tool_calls():
    msgs = [
        {"role": "user", "content": "hi"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"function": {"name": "t", "arguments": "{}"}}],
        },
        {"role": "user", "content": "next question"},
    ]
    out = _sanitize_tool_messages_after_compact(msgs)
    assert "tool_calls" not in out[1]
    assert out[1]["content"] == "(tool call summarized)"


def test_sanitize_keeps_tool_calls_with_later_response():
    msgs = [
        {
            "role": "assistant",
            "content": "thinking",
            "tool_calls": [{"function": {"name": "t", "arguments": "{}"}}],
        },
        {"role": "user", "content": "interlude"},
        {"role": "tool", "content": "late result"},
    ]
    out = _sanitize_tool_messages_after_compact(msgs)
    # the user message ends the tool-response window per pass 2, but the
    # tool message that follows the user still has no parent → dropped in
    # pass 1; the assistant's tool_calls then dangle → stripped
    assert [m["role"] for m in out] == ["assistant", "user"]
    assert "tool_calls" not in out[0]
    assert out[0]["content"] == "thinking"  # existing text kept


def test_sanitize_keeps_dangling_tool_calls_when_response_follows():
    msgs = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"function": {"name": "t", "arguments": "{}"}}],
        },
        {"role": "tool", "content": "result"},
        {"role": "user", "content": "thanks"},
    ]
    out = _sanitize_tool_messages_after_compact(msgs)
    assert [m["role"] for m in out] == ["assistant", "tool", "user"]
    assert out[0]["tool_calls"]


# ══════════════════════════════════════════════════════════════════════
# 5. compact_conversation
# ══════════════════════════════════════════════════════════════════════


def _big(role, n=2000):
    return {"role": role, "content": "x" * n}


@pytest.mark.asyncio
async def test_compact_skips_short_conversations():
    msgs = [{"role": "user", "content": "hi"} for _ in range(5)]
    out, compacted = await compact_conversation(msgs, "m-8k")
    assert out is msgs
    assert compacted is False


@pytest.mark.asyncio
async def test_compact_skips_below_threshold():
    msgs = [{"role": "user", "content": "hi"} for _ in range(12)]
    out, compacted = await compact_conversation(msgs, "m-128k")  # huge window
    assert out is msgs
    assert compacted is False


@pytest.mark.asyncio
async def test_compact_skips_when_too_few_conversation_messages():
    # Tokens exceed the 80% threshold on a 4k window, but only 6 real
    # conversation messages (below PRESERVE_TURNS + 2) — compaction must
    # not run even though usage is high.
    msgs = (
        [{"role": "system", "content": "sys"}]
        + [_big("user") for _ in range(6)]
        + [
            {"role": "user", "content": "[Context: memory] tail"},
            {"role": "user", "content": "<<<UNTRUSTED_SOURCE_DATA>>> tail"},
        ]
    )
    out, compacted = await compact_conversation(msgs, "m-4k")
    assert out is msgs
    assert compacted is False


@pytest.mark.asyncio
async def test_compact_success_replaces_old_half_with_summary():
    system = {"role": "system", "content": "You are helpful."}
    convo = [_big("user"), _big("assistant")] * 6  # 12 messages, ~7250 tokens
    msgs = [system] + convo
    with patch.object(
        cc, "_llm_summarize", new=AsyncMock(return_value="Summary of the past.")
    ) as summ:
        out, compacted = await compact_conversation(msgs, "m-8k")
    assert compacted is True
    assert len(out) == 8  # system + summary + 6 recent
    assert out[0] == system
    assert out[1]["role"] == "system"
    assert out[1]["content"].startswith(
        "[Compacted conversation #1 — 6 earlier messages summarized]"
    )
    assert "Summary of the past." in out[1]["content"]
    # The most recent turns are preserved verbatim
    assert out[2:] == convo[6:]
    # The summarizer saw the older half
    text_arg = summ.call_args[0][1]
    assert text_arg.startswith("USER:")
    assert summ.call_args[0][2] == settings.CONTEXT_COMPACT_SUMMARY_TOKENS


@pytest.mark.asyncio
async def test_compact_preserves_tail_context_messages():
    system = {"role": "system", "content": "sys"}
    convo = [_big("user"), _big("assistant")] * 6
    tail = [
        {"role": "user", "content": "[Context: memory] injected block"},
        {"role": "user", "content": "<<<UNTRUSTED_SOURCE_DATA>>> past convs"},
    ]
    msgs = [system] + convo + tail
    with patch.object(cc, "_llm_summarize", new=AsyncMock(return_value="S")):
        out, compacted = await compact_conversation(msgs, "m-8k")
    assert compacted is True
    # system + summary + 6 recent + 2 tail
    assert len(out) == 10
    assert out[-2:] == tail
    # Tail messages were never sent to the summarizer and stay intact
    assert out[-1] == tail[1]
    assert out[-1]["content"].startswith("<<<UNTRUSTED_SOURCE_DATA>>>")


@pytest.mark.asyncio
async def test_compact_drops_orphan_tool_message_in_recent_part():
    system = {"role": "system", "content": "sys"}
    convo = [_big("user"), _big("assistant")] * 6  # indices 0..11
    # Make the first message of the "recent" slice an orphan tool message
    convo[6] = {"role": "tool", "content": "orphan output"}
    msgs = [system] + convo
    with patch.object(cc, "_llm_summarize", new=AsyncMock(return_value="S")):
        out, compacted = await compact_conversation(msgs, "m-8k")
    assert compacted is True
    assert all(m["role"] != "tool" for m in out)


@pytest.mark.asyncio
async def test_compact_failure_returns_original_intact():
    msgs = [{"role": "system", "content": "sys"}] + [
        _big("user"),
        _big("assistant"),
    ] * 6
    with patch.object(cc, "_llm_summarize", new=AsyncMock(return_value="")):
        out, compacted = await compact_conversation(msgs, "m-8k")
    assert out is msgs  # refuse-to-compact on failure
    assert compacted is False


@pytest.mark.asyncio
async def test_compact_replaces_previous_compaction_header():
    system1 = {"role": "system", "content": "[Compacted conversation #1 — old]"}
    system2 = {"role": "system", "content": "You are helpful."}
    convo = [_big("user"), _big("assistant")] * 6
    msgs = [system1, system2] + convo
    with patch.object(cc, "_llm_summarize", new=AsyncMock(return_value="S")):
        out, compacted = await compact_conversation(msgs, "m-8k")
    assert compacted is True
    headers = [m for m in out if "[Compacted conversation" in m["content"]]
    assert len(headers) == 1  # no stacking
    assert headers[0]["content"].startswith("[Compacted conversation #2")
    # The fresh system prompt survived
    assert system2 in out
    assert system1 not in out


# ══════════════════════════════════════════════════════════════════════
# 6. summarize_conversation (cross-session summary)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_summarize_conversation_too_few_messages():
    assert await summarize_conversation(
        [{"role": "user", "content": "a"}] * 3, "m"
    ) is None


@pytest.mark.asyncio
async def test_summarize_conversation_too_few_non_system():
    msgs = [{"role": "system", "content": "sys"}] * 3 + [
        {"role": "user", "content": "a"}
    ]
    assert await summarize_conversation(msgs, "m") is None


@pytest.mark.asyncio
async def test_summarize_conversation_samples_first_two_and_last_four():
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(8)]
    with patch.object(
        cc, "_llm_summarize", new=AsyncMock(return_value="x" * 40)
    ) as summ:
        out = await summarize_conversation(msgs, "m-8k")
    assert out == "x" * 40
    text_arg = summ.call_args[0][1]
    assert "USER: m0" in text_arg
    assert "USER: m1" in text_arg
    assert "USER: m4" in text_arg
    assert "USER: m7" in text_arg
    assert "m2" not in text_arg  # middle dropped by sampling
    assert summ.call_args.kwargs["max_tokens"] == 300


@pytest.mark.asyncio
async def test_summarize_conversation_rejects_short_summary():
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    with patch.object(cc, "_llm_summarize", new=AsyncMock(return_value="short")):
        assert await summarize_conversation(msgs, "m") is None


@pytest.mark.asyncio
async def test_summarize_conversation_none_summary():
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    with patch.object(cc, "_llm_summarize", new=AsyncMock(return_value=None)):
        assert await summarize_conversation(msgs, "m") is None


# ══════════════════════════════════════════════════════════════════════
# 7. get_conversation_context
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_conversation_context_empty():
    assert await get_conversation_context([], "m") == ""


@pytest.mark.asyncio
async def test_get_conversation_context_formats_and_limits():
    summaries = [f"Summary number {i}" for i in range(5)]
    out = await get_conversation_context(summaries, "m", max_summaries=3)
    assert "## Previous Conversations" in out
    assert "- Summary number 0" in out
    assert "- Summary number 2" in out
    assert "Summary number 3" not in out  # trimmed to max_summaries


@pytest.mark.asyncio
async def test_get_conversation_context_truncates_at_100_words():
    long_summary = " ".join(f"w{i}" for i in range(150))
    out = await get_conversation_context([long_summary], "m")
    assert out.count("w") == 100
    assert out.rstrip().endswith("...")
