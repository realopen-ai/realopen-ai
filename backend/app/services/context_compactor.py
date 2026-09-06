"""
Context Compactor for RealOpen-AI.

Auto-summarizes conversation history as it approaches the model's
context window limit. This is essential for local models with small
context windows.

Also handles end-of-conversation summarization for cross-session memory.

IMPROVEMENTS:
- Tool-call-aware token estimation: counts tool_calls[].function.arguments
  (not just content), with 4 tokens overhead per tool_call. Without this,
  a tool-only assistant turn (content=None, large tool body) reads as
  ~0 tokens and compaction triggers late.
- Better token estimate: 0.3 tokens/char (was 0.4) — closer to real BPE
  output. Code and CJK are still underestimated but less so.
- Post-trim tool-message sanitization: drops orphan `role:"tool"`
  messages whose parent was compacted away, preventing the OpenAI API's
  "tool message without preceding tool_call" 400 error.
- Refuse-to-compact on summarizer failure: returns the original messages
  intact rather than silently dropping the older half.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.prompts import format_prompt

logger = logging.getLogger(__name__)


def _log(msg: str, *args: Any) -> None:
    """Always-visible print() logger.

    Uses print() with flush=True so output appears immediately in the
    container logs — no DEBUG flag needed.
    """
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[context_compactor] {formatted}", flush=True)


# ── Constants ───────────────────────────────────────────────────

# Default context lengths by model size for estimation
DEFAULT_CONTEXT_LENGTHS: Dict[str, int] = {
    "4k": 4096,
    "8k": 8192,
    "16k": 16384,
    "32k": 32768,
    "128k": 131072,
}


def _estimate_tokens(text: str) -> int:
    """Rough token count: ~0.3 tokens per character for English text.

    Uses 0.3 (chars * 0.3) instead of the commonly-cited 0.25
    (chars/4) because 0.3 is closer to real BPE output. Still a rough
    estimate — code and CJK are off, but better than 0.4 which
    overestimates English and triggers compaction too early.
    """
    if not text:
        return 0
    return max(1, int(len(text) * 0.3))


def _estimate_message_tokens(msg: Dict) -> int:
    """Estimate tokens in a message dict — TOOL-CALL-AWARE.

    Counts:
    - The text content (str or list-of-blocks)
    - 4 tokens per-message overhead (role, formatting)
    - tool_calls[].function.name + .arguments (with 4 tokens per call)
      — a tool-only assistant turn carries content=None with the real
      payload in tool_calls, so ignoring them made the compaction gates
      blind to large tool arguments.
    """
    total = 4  # per-message overhead
    content = msg.get("content", "")
    if isinstance(content, str):
        total += _estimate_tokens(content)
    elif isinstance(content, list):
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    total += _estimate_tokens(item.get("text", ""))
                elif item.get("type") == "image_url":
                    total += 256  # image placeholder overhead (rough)

    # Count tool_calls (assistant messages with function calls). A tool-only
    # turn carries content=None with the real payload in tool_calls —
    # ignoring them made the compaction gates blind to large tool args.
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list):
        for tc in tool_calls:
            total += 4  # per-tool-call overhead
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            name = str(fn.get("name", ""))
            args = fn.get("arguments", "")
            if not isinstance(args, str):
                import json as _json

                try:
                    args = _json.dumps(args)
                except Exception:
                    args = str(args)
            total += _estimate_tokens(name + args)

    # role:"tool" messages carry tool_call_id + content — count the content
    # (already handled above) plus a small overhead for the tool_call_id ref.
    if msg.get("role") == "tool":
        total += 4

    return total


def _estimate_context_window(model_name: str) -> int:
    """Estimate a model's context window from its name.

    Falls back to 8192 for unknown models (conservative for 4B/7B).
    """
    ml = model_name.lower()
    if "128k" in ml or "131k" in ml:
        return 131072
    if "32k" in ml:
        return 32768
    if "16k" in ml:
        return 16384
    if "8k" in ml:
        return 8192
    if "4k" in ml:
        return 4096
    # Heuristic: models mentioning 70b, 34b, 32b likely have larger context
    if any(x in ml for x in ("70b", "72b", "34b", "32b", "405b")):
        return 16384
    if any(x in ml for x in ("3b", "4b", "7b", "8b")):
        return 8192
    # Default conservative
    return 8192


# ── Compaction prompt ────────────────────────────────────────────


async def _llm_summarize(model: str, text: str, max_tokens: int = 512) -> Optional[str]:
    """Call the LLM to produce a summary. Returns None on failure."""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            _log(
                " =================== Calling LLM for summary (model=%s, max_tokens=%d)",
                model,
                max_tokens,
            )
            resp = await client.post(
                f"{settings.OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": model,
                    "messages": [
                        {
                            "role": "system",
                            "content": format_prompt("context_compactor_system"),
                        },
                        {"role": "user", "content": text},
                    ],
                    "think": False,
                    "stream": False,
                    "options": {"num_predict": max_tokens},
                },
            )
            resp.raise_for_status()
            _log(
                " =================== LLM summary response=%d",
                resp.json(),
            )
            data = resp.json()
            return data.get("message", {}).get("content", "").strip()
    except Exception as e:
        logger.warning("Summarization LLM call failed: %s", e)
        _log(" ============================== LLM summarize failed for text: %s", text)
        return None


# ── In-conversation compaction ───────────────────────────────────


def _format_message_for_compaction(msg: Dict) -> str:
    """Format a single message for the compaction summarizer."""
    role = msg.get("role", "unknown").upper()
    content = msg.get("content", "")

    if isinstance(content, list):
        parts = []
        for b in content:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    parts.append(b.get("text", ""))
                elif b.get("type") == "image_url":
                    parts.append("[image]")
        content = " ".join(parts)

    # Include tool_calls in the compaction text so the summary knows what
    # tools were called and with what arguments (truncated).
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        tc_parts = []
        for tc in tool_calls:
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            name = fn.get("name", "unknown")
            args = fn.get("arguments", "")
            if not isinstance(args, str):
                import json as _json

                try:
                    args = _json.dumps(args)
                except Exception:
                    args = str(args)
            if len(args) > 200:
                args = args[:200] + "…"
            tc_parts.append(f"[tool: {name}({args})]")
        content = (str(content) + " " + " ".join(tc_parts)).strip()

    content = str(content)[:2000]  # Cap per-message
    return f"{role}: {content}"


def _build_compaction_text(messages: List[Dict]) -> str:
    """Build a compact text representation for the summarizer."""
    return "\n\n".join(_format_message_for_compaction(m) for m in messages)


def _sanitize_tool_messages_after_compact(messages: List[Dict]) -> List[Dict]:
    """Drop orphan tool/assistant-tool_calls messages after compaction.

    After compaction splits the conversation, a `role:"tool"` message
    may end up without its parent `assistant.tool_calls` message (which
    got summarized into the compacted block). Ollama/OpenAI APIs reject
    this with "tool message without preceding tool_call". Similarly, a
    dangling `assistant.tool_calls` with no following tool response
    breaks the pairing.

    Two passes:
    1. Drop orphan `role:"tool"` messages (no preceding assistant.tool_calls).
    2. Strip `tool_calls` from assistant messages that have no following
       tool response (keep their text content if any).
    """
    if not messages:
        return messages

    result: List[Dict] = []
    for i, m in enumerate(messages):
        role = m.get("role")
        if role == "tool":
            # Check if the previous KEPT message is an assistant with tool_calls
            has_parent = False
            if result:
                prev = result[-1]
                if (
                    prev.get("role") == "assistant"
                    and isinstance(prev.get("tool_calls"), list)
                    and prev["tool_calls"]
                ):
                    has_parent = True
            if not has_parent:
                _log("dropping orphan tool message after compaction")
                continue
        result.append(dict(m))

    # Pass 2: strip dangling assistant.tool_calls (no following tool response)
    final: List[Dict] = []
    for i, m in enumerate(result):
        if (
            m.get("role") == "assistant"
            and isinstance(m.get("tool_calls"), list)
            and m["tool_calls"]
        ):
            # Check if any of the next messages is a tool response
            has_response = False
            for nxt in result[i + 1 :]:
                if nxt.get("role") == "tool":
                    has_response = True
                    break
                if nxt.get("role") == "user":
                    break  # user message ends the tool-response window
            if not has_response:
                _log("stripping dangling assistant.tool_calls after compaction")
                m = {k: v for k, v in m.items() if k != "tool_calls"}
                # If content is empty/None, give it a placeholder so the
                # message isn't dropped by the API for having no content.
                if not m.get("content"):
                    m["content"] = "(tool call summarized)"
        final.append(m)

    return final


async def compact_conversation(
    messages: List[Dict],
    model: str,
) -> Tuple[List[Dict], bool]:
    """Check if compaction is needed and compact if so.

    Args:
        messages: Current conversation messages (including system prompt and
            any dynamic context messages appended after the conversation).
        model: Ollama model name (for context window detection)

    Returns:
        (compacted_messages, was_compacted)
    """
    if len(messages) < 8:
        return messages, False

    context_length = _estimate_context_window(model)
    total_tokens = sum(_estimate_message_tokens(m) for m in messages)
    usage_pct = total_tokens / context_length if context_length else 0

    if usage_pct < settings.CONTEXT_COMPACT_THRESHOLD:
        return messages, False

    _log(
        "Context at %.0f%% (%d/%d tokens) — compacting",
        usage_pct * 100,
        total_tokens,
        context_length,
    )

    # Split into system + conversation + tail-context.
    # Tail-context messages (memories, cross-session, datetime) are
    # user-role messages we appended AFTER the conversation — they should
    # NOT be compacted away. Identify them by the untrusted-context guard
    # markers or the [Context: ...] prefix.
    system_msgs = [m for m in messages if m.get("role") == "system"]
    convo_msgs = []
    tail_context_msgs = []
    for m in messages:
        if m.get("role") == "system":
            continue
        content = str(m.get("content", ""))
        # Tail context messages are wrapped in untrusted guards or have
        # the [Context: ...] prefix. Keep them out of compaction.
        if "<<<UNTRUSTED_SOURCE_DATA>>>" in content or content.startswith("[Context:"):
            tail_context_msgs.append(m)
        else:
            convo_msgs.append(m)

    if len(convo_msgs) < settings.CONTEXT_COMPACT_PRESERVE_TURNS + 2:
        return messages, False

    # Summarize older half, keep recent turns
    split = len(convo_msgs) - settings.CONTEXT_COMPACT_PRESERVE_TURNS
    older = convo_msgs[:split]
    recent = convo_msgs[split:]

    compaction_text = _build_compaction_text(older)
    if not compaction_text:
        return messages, False

    # Count existing compactions for the header
    existing_compactions = sum(
        1 for m in system_msgs if "[Compacted conversation" in str(m.get("content", ""))
    )

    summary = await _llm_summarize(
        model, compaction_text, settings.CONTEXT_COMPACT_SUMMARY_TOKENS
    )
    if not summary:
        # Refuse-to-compact on failure: return original intact rather
        # than silently dropping the older half.
        logger.warning("Compaction summary failed — keeping original messages")
        _log("Compaction summary failed — keeping original messages")
        return messages, False

    # Build the compacted message list
    summary_msg = {
        "role": "system",
        "content": (
            f"[Compacted conversation #{existing_compactions + 1} — "
            f"{len(older)} earlier messages summarized]\n\n{summary}"
        ),
    }

    # Replace old compaction summaries with the latest one (avoid stacking)
    compacted_system = [
        m
        for m in system_msgs
        if "[Compacted conversation" not in str(m.get("content", ""))
    ]

    # Reassemble: system prefix + compaction summary + recent convo + tail context
    result = compacted_system + [summary_msg] + recent + tail_context_msgs

    # Sanitize: drop orphan tool messages left by the compaction split
    result = _sanitize_tool_messages_after_compact(result)

    new_tokens = sum(_estimate_message_tokens(m) for m in result)
    _log(
        "Compacted: %d → %d tokens (%d messages → %d messages)",
        total_tokens,
        new_tokens,
        len(messages),
        len(result),
    )

    return result, True


# ── Conversation-end summary for cross-session memory ────────────


async def summarize_conversation(
    messages: List[Dict],
    model: str,
) -> Optional[str]:
    """Generate a durable summary for cross-session memory.

    Called when a conversation ends or reaches a significant length.
    The summary is stored with the conversation for retrieval in
    future sessions.
    """
    if len(messages) < 4:
        return None

    # Take the conversation messages (no system prompt)
    convo_msgs = [m for m in messages if m.get("role") != "system"]
    if len(convo_msgs) < 4:
        return None

    # Sample: first 2 + last 4 turns for balanced context
    sampled = convo_msgs[:2] + convo_msgs[-4:]
    text = _build_compaction_text(sampled)

    summary = await _llm_summarize(model, text, max_tokens=300)
    return summary if (summary and len(summary) > 20) else None


# ── Cross-session context retrieval ──────────────────────────────


async def get_conversation_context(
    recent_summaries: List[str],
    model: str,
    max_summaries: int = 3,
) -> str:
    """Build a cross-session context block from recent conversation summaries.

    Injects into the system prompt to give the agent continuity across sessions.
    For local models, keeps it terse to avoid context bloat.
    """
    if not recent_summaries:
        return ""

    # Trim to max summaries and format compactly
    summaries = recent_summaries[:max_summaries]
    lines = ["\n\n## Previous Conversations"]
    for i, s in enumerate(summaries, 1):
        # Truncate each summary to ~100 words
        words = s.split()
        short = " ".join(words[:100])
        if len(words) > 100:
            short += "..."
        lines.append(f"- {short}")

    return "\n".join(lines)
