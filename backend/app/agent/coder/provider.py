"""Coder-model invocation and malformed tool-call recovery."""

from __future__ import annotations

import json
import re
import time

import httpx

from app.agent.coder.tooling import CODER_TOOLS
from app.agent.protocol import model_messages
from app.agent.tracing import observe, timestamp
from app.prompts import format_prompt
from app.services import providers, tool_capabilities
from app.services.context_compactor import context_budget, compact_conversation


def parse_calls(message: dict) -> list[dict]:
    return message.get("tool_calls") or []


def recovery_message(content: str) -> dict:
    raw = content.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.DOTALL)
    parsed = json.loads(raw)
    if isinstance(parsed, dict) and parsed.get("tool"):
        return {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "function": {
                        "name": parsed["tool"],
                        "arguments": parsed.get("args", {}),
                    }
                }
            ],
        }
    if isinstance(parsed, dict) and parsed.get("final") is not None:
        final = parsed["final"]
        return {
            "role": "assistant",
            "content": (
                json.dumps(final, ensure_ascii=False)
                if isinstance(final, dict)
                else str(final)
            ),
        }
    raise ValueError("Coder recovery response contained neither tool nor final")


async def _chat_attempt(model, messages, tools, *, recovery=False, **kwargs):
    options = dict(kwargs.get("options") or {})
    budget = context_budget(model, options.get("num_predict"))
    options["num_predict"] = budget.output
    if not providers.is_groq_model(model):
        options["num_ctx"] = budget.window
    kwargs["options"] = options
    if recovery:
        messages = [dict(message) for message in messages]
        protocol = format_prompt(
            "coder_text_tools",
            tool_schemas=json.dumps(CODER_TOOLS, separators=(",", ":")),
        )
        # Keep core prefix/history byte-identical; protocol context is a
        # separate application-owned message, frozen for this attempt.
        messages.append(
            {
                "role": "user",
                "content": protocol,
                "metadata": {"agent_context": True, "trusted": True},
            }
        )
        messages, _ = await compact_conversation(
            messages,
            model,
            tools=tools,
            max_output_tokens=budget.output,
            native_tools=False,
        )
    messages = model_messages(messages, native=not recovery, fenced_tools=not recovery)
    started_at, started = timestamp(), time.monotonic()
    try:
        result = await providers.chat_once(model, messages, tools=tools, **kwargs)
    except BaseException as exc:
        error = {"type": type(exc).__name__, "message": str(exc)}
        if isinstance(exc, httpx.HTTPStatusError):
            error.update(
                status_code=exc.response.status_code, response=exc.response.text
            )
        observe(
            "model_attempt",
            messages,
            tools,
            error=error,
            recovery=recovery,
            started_at=started_at,
            duration_ms=(time.monotonic() - started) * 1000,
        )
        raise
    observe(
        "model_attempt",
        messages,
        tools,
        response=result,
        recovery=recovery,
        started_at=started_at,
        duration_ms=(time.monotonic() - started) * 1000,
    )
    return result


async def chat_with_tool_recovery(model: str, messages: list[dict]) -> dict:
    async def text_attempt():
        fallback = await _chat_attempt(
            model,
            messages,
            tools=None,
            recovery=True,
            timeout=1200,
            format="json",
            think=False,
            options={"num_predict": 2048, "temperature": 0},
        )
        try:
            fallback["message"] = recovery_message(
                fallback.get("message", {}).get("content", "")
            )
        except Exception as parse_error:
            observe("recovery_error", str(parse_error))
            raise
        return fallback

    if not tool_capabilities.should_try_native(model):
        return await text_attempt()
    try:
        result = await _chat_attempt(
            model,
            messages,
            tools=CODER_TOOLS,
            timeout=1200,
            think=False,
            options={"num_predict": 2048, "temperature": 0},
        )
        return result
    except (httpx.HTTPStatusError, providers.ProviderStreamError) as first_error:
        reason = tool_capabilities.rejection_reason(first_error)
        # Retain one-off recovery for generic server errors, but never cache it
        # as a model capability. Authentication/rate-limit failures propagate.
        server_error = isinstance(
            first_error, httpx.HTTPStatusError
        ) and first_error.response.status_code in {500, 502, 503, 504}
        if not reason and not server_error:
            raise
        if reason:
            await tool_capabilities.remember_fallback(model, reason)
        try:
            return await text_attempt()
        except Exception as recovery_error:
            raise RuntimeError(
                "The coder model emitted an invalid tool call and JSON recovery "
                f"also failed: {recovery_error}"
            ) from first_error
