"""Budget agent requests and summarize complete historical exchanges.

Estimates include schemas, thinking and tool transcripts; generation reserves
and runtime windows use explicit configuration. Summaries remain untrusted
context. Protected task/source metadata is retained, and an oversized request
fails safely when compaction cannot make it fit. Callers retain the compacted
working transcript across subsequent rounds.

Also provides conversation-end summarization for cross-session retrieval.
"""

import logging
import json
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.prompts import get_prompt
from app.core.prompt_security import untrusted_context_message
from app.agent.protocol import model_messages
from app.services import providers, agent_settings

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


def _estimate_tokens(text: str) -> int:
    """Heuristic text estimate, weighted for denser non-ASCII tokenization.

    Not a provider tokenizer: templates/images still require a safety margin.
    """
    if not text:
        return 0
    # Non-ASCII languages/code symbols tokenize more densely than English.
    return max(1, int(len(text) * 0.3 + sum(ord(c) > 127 for c in text) * 0.7))


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
    total += _estimate_tokens(msg.get("thinking") or "")
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
                try:
                    args = json.dumps(args, ensure_ascii=False)
                except Exception:
                    args = str(args)
            total += _estimate_tokens(name + args + str(tc.get("id") or ""))

    # role:"tool" messages carry tool_call_id + content — count the content
    # (already handled above) plus a small overhead for the tool_call_id ref.
    if msg.get("role") == "tool":
        total += 4 + _estimate_tokens(
            str(msg.get("tool_name") or "") + str(msg.get("tool_call_id") or "")
        )

    return total


def _estimate_context_window(model_name: str) -> int:
    """Return the configured effective runtime window, never a name heuristic."""
    return settings.AGENT_CONTEXT_WINDOWS.get(
        model_name, agent_settings.effective().context_window
    )


class ContextBudgetExceeded(ValueError):
    """The protected request/context cannot fit without losing user data."""


@dataclass(frozen=True)
class ContextBudget:
    window: int
    output: int
    margin: int

    @property
    def input_limit(self):
        return self.window - self.output - self.margin


def context_budget(model: str, max_output_tokens=None) -> ContextBudget:
    budget = ContextBudget(
        _estimate_context_window(model),
        (
            max_output_tokens
            if max_output_tokens is not None
            else agent_settings.effective().generation_limit
        ),
        settings.AGENT_CONTEXT_MARGIN_TOKENS,
    )
    if (
        budget.window <= 0
        or budget.output <= 0
        or budget.margin < 0
        or budget.input_limit <= 0
    ):
        raise ContextBudgetExceeded(
            "Invalid context/output budget; increase the context window or reduce the output reserve."
        )
    return budget


def estimate_request_tokens(messages, tools=None):
    """Estimate the full input, including schemas and message/template overhead."""
    return sum(_estimate_message_tokens(m) for m in messages) + (
        _estimate_tokens(json.dumps(tools, ensure_ascii=False)) + 8 if tools else 0
    )


# ── Compaction prompt ────────────────────────────────────────────


async def _llm_summarize(model: str, text: str, max_tokens: int = 512) -> Optional[str]:
    """Call the LLM to produce a summary. Returns None on failure."""
    try:
        if providers.is_groq_model(model):
            response = await providers.chat_once(
                model,
                [
                    {
                        "role": "system",
                        "content": get_prompt("context_compactor_system"),
                    },
                    {"role": "user", "content": text},
                ],
                timeout=120,
                think=False,
                options={"num_predict": max_tokens},
            )
            return (response.get("message", {}).get("content") or "").strip()
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
                            "content": get_prompt("context_compactor_system"),
                        },
                        {"role": "user", "content": text},
                    ],
                    "think": False,
                    "stream": False,
                    "options": {
                        "num_predict": max_tokens,
                        "num_ctx": _estimate_context_window(model),
                    },
                },
            )
            resp.raise_for_status()
            _log(
                " =================== LLM summary response=%s",
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

    # Keep complete text/arguments; bounded summarization batches large inputs.
    tool_calls = msg.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        tc_parts = []
        for tc in tool_calls:
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            name = fn.get("name", "unknown")
            args = fn.get("arguments", "")
            if not isinstance(args, str):
                try:
                    args = json.dumps(args, ensure_ascii=False)
                except Exception:
                    args = str(args)
            tc_parts.append(f"[tool: {name}({args})]")
        content = (str(content) + " " + " ".join(tc_parts)).strip()

    return f"{role}: {content}"


def _build_compaction_text(messages: List[Dict]) -> str:
    """Build a compact text representation for the summarizer."""
    return "\n\n".join(_format_message_for_compaction(m) for m in messages)


def _sanitize_tool_messages_after_compact(messages: List[Dict]) -> List[Dict]:
    """Preserve complete parallel exchanges; discard only truly orphaned results."""
    result = []
    pending = []
    for message in messages:
        if message.get("role") == "tool":
            matches = [
                call
                for call in pending
                if (
                    message.get("tool_call_id") == call.get("id")
                    if message.get("tool_call_id")
                    else (
                        message.get("tool_name")
                        == (call.get("function") or {}).get("name")
                        if message.get("tool_name")
                        else True
                    )
                )
            ]
            if not matches:
                continue
            result.append(dict(message))
            pending.remove(matches[0])
            continue
        if pending:
            parent = next((m for m in reversed(result) if m.get("tool_calls")), None)
            if parent:
                unmatched = {id(c) for c in pending}
                parent["tool_calls"] = [
                    c for c in parent["tool_calls"] if id(c) not in unmatched
                ]
                if not parent["tool_calls"]:
                    parent.pop("tool_calls")
                    parent["content"] = (
                        parent.get("content") or "(tool call summarized)"
                    )
            pending = []
        result.append(dict(message))
        if message.get("tool_calls"):
            pending = list(message["tool_calls"])
    if pending:
        parent = next((m for m in reversed(result) if m.get("tool_calls")), None)
        if parent:
            unmatched = {id(c) for c in pending}
            parent["tool_calls"] = [
                c for c in parent["tool_calls"] if id(c) not in unmatched
            ]
            if not parent["tool_calls"]:
                parent.pop("tool_calls")
                parent["content"] = parent.get("content") or "(tool call summarized)"
    return result


def _protected(message):
    meta = message.get("metadata") or {}
    if meta.get("compaction"):
        return False
    content = str(message.get("content") or "")
    return bool(
        meta.get("agent_context")
        or meta.get("agent_request")
        or (
            message.get("role") == "user"
            and (
                content.startswith("[Context:")
                or content.startswith("<<<UNTRUSTED_SOURCE_DATA>>>")
            )
        )
    )


async def _summarize_bounded(model, text, max_tokens=None):
    """Summarize bounded source batches, then their summaries if necessary."""
    summary_tokens = (
        settings.CONTEXT_COMPACT_SUMMARY_TOKENS if max_tokens is None else max_tokens
    )
    limit = (
        context_budget(model, summary_tokens).input_limit
        - _estimate_tokens(get_prompt("context_compactor_system"))
        - 8
    )
    if limit < 128:
        return None
    for _ in range(3):
        if _estimate_tokens(text) <= limit:
            return await _llm_summarize(model, text, max_tokens=summary_tokens)
        # A conservative character batch is shorter than the token budget.
        summaries = []
        for offset in range(0, len(text), limit):
            part = await _llm_summarize(
                model, text[offset : offset + limit], max_tokens=summary_tokens
            )
            if not part:
                return None
            summaries.append(part)
        combined = "\n\n".join(summaries)
        if len(combined) >= len(text):
            return None
        text = combined
    return None


async def compact_conversation(
    messages: List[Dict],
    model: str,
    *,
    tools=None,
    max_output_tokens=None,
    native_tools=True,
) -> Tuple[List[Dict], bool]:
    """Budget the actual request and summarize whole exchanges, never partial pairs.

    Limits are configured per model, not inferred from its advertised/name window.
    Estimates include tool schemas, output reserve and a template/safety margin.
    Protected current-request/context data is never silently truncated.
    """
    budget = context_budget(model, max_output_tokens)
    # Demote legacy system-role summaries to history after the real prefix.
    legacy = [
        m
        for m in messages
        if m.get("role") == "system"
        and str(m.get("content", "")).startswith("[Compacted conversation")
    ]
    if legacy:
        messages = [m for m in messages if m not in legacy]
        insertion = next(
            (i for i, m in enumerate(messages) if m.get("role") != "system"),
            len(messages),
        )
        history = []
        for m in legacy:
            historical = untrusted_context_message(
                "compacted conversation", m["content"]
            )
            historical["metadata"]["compaction"] = True
            history.append(historical)
        messages[insertion:insertion] = history

    def size(items):
        return estimate_request_tokens(model_messages(items, native_tools), tools)

    total = size(messages)
    preferences = agent_settings.effective()
    if not preferences.history_compaction:
        if total > budget.input_limit:
            raise ContextBudgetExceeded(
                "History compaction is disabled and the request exceeds the context budget. Enable compaction or narrow the request."
            )
        return messages, False
    soft_limit = int(budget.input_limit * preferences.compaction_threshold)
    if total <= soft_limit:
        return messages, False

    # Build atomic groups; an assistant and all its tool results stay together.
    candidates = []
    for index, message in enumerate(messages):
        if message.get("role") == "system" and not str(
            message.get("content", "")
        ).startswith("[Compacted conversation"):
            continue
        if _protected(message):
            continue
        if (
            message.get("role") == "tool"
            and candidates
            and candidates[-1][-1] == index - 1
            and messages[candidates[-1][0]].get("tool_calls")
        ):
            candidates[-1].append(index)
        else:
            candidates.append([index])
    # Always preserve the latest user request if the caller did not mark it.
    latest_user = next(
        (
            i
            for i, m in reversed(list(enumerate(messages)))
            if m.get("role") == "user"
            and not _protected(m)
            and not (m.get("metadata") or {}).get("compaction")
        ),
        None,
    )
    if any((m.get("metadata") or {}).get("agent_request") for m in messages):
        latest_user = None
    if latest_user is not None:
        candidates = [g for g in candidates if latest_user not in g]
    # Never summarize the latest tool exchange: it is the model's exact
    # feedback, not historical context. This includes all parallel results.
    request_boundary = next(
        (
            i
            for i, m in enumerate(messages)
            if (m.get("metadata") or {}).get("agent_request")
        ),
        latest_user,
    )
    latest_exchange = next(
        (
            g
            for g in reversed(candidates)
            if messages[g[0]].get("tool_calls")
            and (request_boundary is None or g[0] > request_boundary)
        ),
        None,
    )
    if latest_exchange is not None:
        candidates = [g for g in candidates if g != latest_exchange]
    if not candidates:
        if total > budget.input_limit:
            raise ContextBudgetExceeded(
                "The current request, source context and tool schemas exceed the input budget. Narrow the request/context or increase AGENT_CONTEXT_WINDOW."
            )
        return messages, False

    # Prefer the configured recent suffix; shorten it only to satisfy the budget.
    preserve = preferences.recent_turns - (1 if latest_user is not None else 0)
    remove_count = max(1, len(candidates) - max(0, preserve))
    # A soft budget crossing is not a reason to summarize a greeting or
    # repeatedly rewrite a tiny summary. Require useful historical material.
    if (
        total <= budget.input_limit
        and sum(_estimate_message_tokens(messages[i]) for g in candidates for i in g)
        < 512
    ):
        return messages, False

    while True:
        removed = {i for group in candidates[:remove_count] for i in group}
        # Replace contiguous runs IN PLACE. In particular, tool activity
        # after the current request must never move before that request.
        result = []
        index = 0
        while index < len(messages):
            if index not in removed:
                result.append(messages[index])
                index += 1
                continue
            start = index
            while index in removed:
                index += 1
            summary = await _summarize_bounded(
                model, _build_compaction_text(messages[start:index])
            )
            if not summary:
                if total <= budget.input_limit:
                    return messages, False
                raise ContextBudgetExceeded(
                    "Context compaction failed; no oversized request was sent."
                )
            summary_message = untrusted_context_message(
                "compacted conversation", "[Compacted conversation]\n" + summary
            )
            summary_message["metadata"]["compaction"] = True
            result.append(summary_message)
        result = _sanitize_tool_messages_after_compact(result)
        new_size = size(result)
        if new_size <= budget.input_limit and total - new_size >= 64:
            return result, True
        if new_size <= budget.input_limit and total <= budget.input_limit:
            return messages, False
        if remove_count >= len(candidates):
            raise ContextBudgetExceeded(
                "The current request, execution state and latest tool feedback cannot fit even after compaction. Narrow the request or increase AGENT_CONTEXT_WINDOW."
            )
        # Regenerate from all newly removed messages; do not discard them silently.
        remove_count = len(candidates)


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

    summary = await _summarize_bounded(model, text, max_tokens=300)
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
