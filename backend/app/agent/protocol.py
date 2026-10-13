"""Provider-independent tool transcripts with native and text-mode encoders."""

import json
import uuid
import re
from copy import deepcopy

from app.core.prompt_security import untrusted_context_message
from app.prompts import format_prompt


def normalize_native_calls(native_calls):
    calls = []
    for raw in native_calls:
        if not isinstance(raw, dict):
            raw = {"function": {"arguments": raw}}
        fn = raw.get("function") or {}
        if not isinstance(fn, dict):
            fn = {"arguments": fn}
        name = fn.get("name")
        original = fn.get("arguments", {})
        error = None
        try:
            args = json.loads(original) if isinstance(original, str) else original
            if not isinstance(args, dict):
                raise ValueError("Arguments must be a JSON object")
            if not isinstance(name, str) or not name.strip():
                raise ValueError("A non-empty tool name is required")
        except (ValueError, TypeError) as exc:
            args = {}
            error = f"Invalid tool arguments: {exc}; received {original}"
        calls.append(
            {
                "id": (
                    raw["id"]
                    if isinstance(raw.get("id"), str) and raw["id"]
                    else "call-" + uuid.uuid4().hex
                ),
                "tool": name if isinstance(name, str) else "unknown",
                "args": args,
                "raw_arguments": original,
                **({"parse_error": error} if error else {}),
            }
        )
    return calls


def assistant_call_message(content, calls, thinking=""):
    return {
        "role": "assistant",
        "content": content or "",
        **({"thinking": thinking} if thinking else {}),
        "tool_calls": [
            {
                "id": call.setdefault("id", "call-" + uuid.uuid4().hex),
                "type": "function",
                "function": {
                    "name": call.get("tool") or call.get("name", "unknown"),
                    "arguments": deepcopy(call.get("args", {})),
                },
            }
            for call in calls
        ],
    }


def tool_result_message(call, output, *, skill_guidance=False):
    name = call.get("tool") or call.get("name", "unknown")
    return {
        "role": "tool",
        "tool_name": name,
        "tool_call_id": call["id"],
        "content": (
            format_prompt("agent_skill_result", instructions=output)
            if skill_guidance
            else untrusted_context_message(f"tool result: {name}", output)["content"]
        ),
    }


def model_messages(messages, native=True, *, fenced_tools=True):
    """Keep tool identities for native templates; use one envelope for fallback."""
    encoded = []
    for message in messages:
        item = {
            k: v
            for k, v in message.items()
            if k
            in {
                "role",
                "content",
                "tool_calls",
                "tool_name",
                "tool_call_id",
                "thinking",
                "images",
            }
        }
        if item.get("tool_calls"):
            names = "|".join(
                re.escape(c["function"]["name"]) for c in item["tool_calls"]
            )
            prose = re.sub(
                r"```(?:tool|" + names + r")\b.*?```",
                "",
                item.get("content") or "",
                flags=re.S,
            )
            prose = re.sub(r"<tool_call>.*?</tool_call>", "", prose, flags=re.S)
            if native:
                item["content"] = prose.strip()
                encoded.append(item)
                continue
            blocks = [
                json.dumps(
                    {
                        "tool": call["function"]["name"],
                        "args": call["function"]["arguments"],
                    },
                    ensure_ascii=False,
                )
                for call in item["tool_calls"]
            ]
            if fenced_tools:
                blocks = ["```tool\n" + block + "\n```" for block in blocks]
            # Text fallback has no native-tool transcript fields.
            item = {
                "role": "assistant",
                "content": "\n".join([prose, *blocks]).strip(),
            }
        elif not native and item["role"] == "tool":
            item = {"role": "user", "content": item["content"]}
        encoded.append(item)
    return encoded
