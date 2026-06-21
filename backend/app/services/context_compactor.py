"""
Context Compactor for RealOpen-AI.

Auto-summarizes conversation history as it approaches the model's
context window limit. This is essential for local models with small
context windows.

Also handles end-of-conversation summarization for cross-session memory.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings

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
    """Rough token count: ~0.4 tokens per character for English text."""
    if not text:
        return 0
    return max(1, int(len(text) * 0.4))


def _estimate_message_tokens(msg: Dict) -> int:
    """Estimate tokens in a message dict."""
    content = msg.get("content", "")
    if isinstance(content, list):
        content = " ".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("text")
        )
    return _estimate_tokens(str(content)) + 4  # role overhead


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

COMPACT_SYSTEM_PROMPT = """Summarize this conversation fragment concisely. Preserve:
- User's name, identity, preferences
- The task or question being worked on
- Key decisions, code written, files created
- Important facts, numbers, URLs
- Current state and next steps

Keep it under 200 words. Be dense — every word matters. Do not add commentary."""


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
                        {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
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

    content = str(content)[:2000]  # Cap per-message
    return f"{role}: {content}"


def _build_compaction_text(messages: List[Dict]) -> str:
    """Build a compact text representation for the summarizer."""
    return "\n\n".join(_format_message_for_compaction(m) for m in messages)


async def compact_conversation(
    messages: List[Dict],
    model: str,
) -> Tuple[List[Dict], bool]:
    """Check if compaction is needed and compact if so.

    Args:
        messages: Current conversation messages (including system prompt)
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

    # Split into system + conversation
    system_msgs = [m for m in messages if m.get("role") == "system"]
    convo_msgs = [m for m in messages if m.get("role") != "system"]

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
        logger.warning("Compaction summary failed — keeping original messages")
        _log("Compaction summary failed for text: %s", compaction_text)
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

    result = compacted_system + [summary_msg] + recent

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


CONVERSATION_SUMMARY_PROMPT = """Summarize this conversation for future reference. Include:
1. What was discussed (main topic/theme)
2. Key outcomes, decisions, or code written
3. Important facts about the user learned
4. Open items or follow-ups

Keep it under 150 words. Be specific — include names, technologies, numbers."""


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
