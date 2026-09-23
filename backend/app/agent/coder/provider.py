"""Coder-model invocation and malformed tool-call recovery."""

from __future__ import annotations

import json
import re

import httpx

from app.agent.coder.tooling import CODER_TOOLS
from app.services import providers


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
        return {"role": "assistant", "content": str(parsed["final"])}
    raise ValueError("Coder recovery response contained neither tool nor final")


async def chat_with_tool_recovery(model: str, messages: list[dict]) -> dict:
    try:
        return await providers.chat_once(
            model,
            messages,
            tools=CODER_TOOLS,
            timeout=1200,
            think=False,
            options={"num_predict": 2048, "temperature": 0},
        )
    except httpx.HTTPStatusError as first_error:
        if first_error.response.status_code < 500:
            raise
        retry_messages = [
            *messages,
            {
                "role": "user",
                "content": (
                    "Your native tool call could not be parsed. Continue the same task "
                    "using exactly one JSON object and no prose. Use either "
                    '{"tool":"function_name","args":{...}} or '
                    '{"final":"completion summary"}. Available function schemas: '
                    + json.dumps(CODER_TOOLS, separators=(",", ":"))
                ),
            },
        ]
        try:
            fallback = await providers.chat_once(
                model,
                retry_messages,
                tools=None,
                timeout=1200,
                format="json",
                think=False,
                options={"num_predict": 2048, "temperature": 0},
            )
            fallback["message"] = recovery_message(
                fallback.get("message", {}).get("content", "")
            )
            return fallback
        except Exception as recovery_error:
            raise RuntimeError(
                "The coder model emitted an invalid tool call and JSON recovery "
                f"also failed: {recovery_error}"
            ) from first_error
