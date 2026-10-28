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
from app.services import coder_settings
from app.services.context_compactor import context_budget, compact_conversation
from app.services.agent_activity import activity_events


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


async def _chat_attempt(
    model,
    messages,
    tools,
    *,
    recovery=False,
    on_activity=None,
    on_thinking=None,
    compact_context=None,
    **kwargs,
):
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
                "metadata": {
                    "agent_context": True,
                    "trusted": True,
                    "coder_protocol": True,
                },
            }
        )
        async for activity in activity_events(
            lambda on_start: (compact_context or compact_conversation)(
                messages,
                model,
                tools=tools,
                max_output_tokens=budget.output,
                native_tools=False,
                on_start=on_start,
            ),
            "context_compaction",
        ):
            if activity.get("event"):
                if on_activity:
                    on_activity(activity)
            else:
                messages, _ = activity["result"]
    messages = model_messages(messages, native=not recovery, fenced_tools=not recovery)
    started_at, started = timestamp(), time.monotonic()
    result = None
    try:
        if on_thinking is None:
            result = await providers.chat_once(model, messages, tools=tools, **kwargs)
        else:
            # Assemble complete calls before executing; forward only thinking live.
            result = {
                "model": model,
                "message": {
                    "role": "assistant",
                    "content": "",
                    "thinking": "",
                    "tool_calls": [],
                },
                "done": False,
            }
            try:
                async for chunk in providers.stream_chat(
                    model, messages, tools=tools, **kwargs
                ):
                    message = result["message"]
                    message["content"] += chunk.get("content", "")
                    token = chunk.get("thinking", "")
                    if token:
                        message["thinking"] += token
                        on_thinking(token)
                    elif chunk.get("content") or chunk.get("tool_calls"):
                        on_thinking(None)
                    message["tool_calls"].extend(chunk.get("tool_calls") or [])
                    for key in ("finish_reason", "usage"):
                        if chunk.get(key) is not None:
                            result[key] = chunk[key]
                    result["done"] = bool(chunk.get("done"))
                if not result["done"]:
                    raise providers.ProviderStreamError(
                        "Coder model stream ended before completion"
                    )
            finally:
                on_thinking(None)
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
            response=result,
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


async def chat_with_tool_recovery(
    model: str,
    messages: list[dict],
    *,
    on_activity=None,
    on_thinking=None,
    transient_text_models=None,
    compact_context=None,
) -> dict:
    preferences = coder_settings.effective()

    async def text_attempt():
        fallback = await _chat_attempt(
            model,
            messages,
            tools=None,
            recovery=True,
            compact_context=compact_context,
            on_activity=on_activity,
            on_thinking=on_thinking,
            timeout=1200,
            format="json",
            think=preferences.thinking,
            options={
                "num_predict": coder_settings.effective().generation_limit,
                "temperature": preferences.temperature,
            },
        )
        try:
            thinking = fallback.get("message", {}).get("thinking", "")
            fallback["message"] = recovery_message(
                fallback.get("message", {}).get("content", "")
            )
            if thinking:
                fallback["message"]["thinking"] = thinking
        except Exception as parse_error:
            observe("recovery_error", str(parse_error))
            raise
        return fallback

    if (
        transient_text_models is not None and model in transient_text_models
    ) or not tool_capabilities.should_try_native(model):
        return await text_attempt()
    failure = None
    for native_attempt in range(2):
        try:
            return await _chat_attempt(
                model,
                messages,
                tools=CODER_TOOLS,
                on_thinking=on_thinking,
                timeout=1200,
                think=preferences.thinking,
                options={
                    "num_predict": coder_settings.effective().generation_limit,
                    "temperature": preferences.temperature,
                },
            )
        except (httpx.HTTPStatusError, providers.ProviderStreamError) as error:
            failure = error
            reason = tool_capabilities.rejection_reason(error)
            parser_failure = tool_capabilities.native_parser_error(error)
            # Existing generic server-error recovery stays one-off. Auth,
            # rate limits, connection errors and validation errors propagate.
            server_error = isinstance(
                error, httpx.HTTPStatusError
            ) and error.response.status_code in {500, 502, 503, 504}
            if reason:
                await tool_capabilities.remember_fallback(model, reason)
                break
            if parser_failure:
                if native_attempt == 0:
                    continue
                if transient_text_models is not None:
                    transient_text_models.add(model)
                break
            if not server_error:
                raise
            break
    try:
        return await text_attempt()
    except Exception as recovery_error:
        raise RuntimeError(
            "The coder model emitted an invalid tool call and JSON recovery "
            f"also failed: {recovery_error}"
        ) from failure
