"""
LLM-powered auto-titling of conversations.

When the FIRST user message lands in a conversation whose title is still the
default ("New Chat"), a small utility model (role `default_utility`, resolved
via profiles.yml) reads the message and produces a short 2-3 word title:

    "can you help me write a python script that renames all my
     photos by EXIF date?"              →  "Photo Rename Script"

Trigger:
- Called from the chat endpoints (chat.py) right after the assistant message
  is persisted, BEFORE the [DONE] sentinel is yielded. The result is streamed
  to the frontend as a `conversation_title` SSE event so the sidebar updates
  live without a refresh. The DB write happens inside this service, so the
  title survives page reloads even if the client disconnects first.

Guards (all inside maybe_generate_and_save_title):
- Only fires when the conversation still carries the default title AND has
  exactly one user message (the first turn). A manual rename (PATCH
  /conversations) moves the title away from the default, so auto-titling can
  never clobber a user-chosen name — not even one made while the LLM call is
  in flight (the title is re-checked right before the write).
- Fully fault-tolerant: every failure mode (Ollama down, timeout, garbage
  output, DB hiccup) is logged and swallowed — the chat stream is unaffected.
  On LLM failure the title falls back to a cleaned truncation of the first
  message (mirroring the frontend's optimistic sidebar preview), so a
  conversation never lingers as "New Chat".

Output cleaning (small local models are messy — same philosophy as
memory_extractor):
- strips <think>...</think> reasoning, unclosed <think> blocks, markdown code
  fences, surrounding quotes, and "Title:"-style label prefixes
- takes the first line only, collapses whitespace
- strips trailing punctuation, caps at 6 words / 60 chars
"""

import logging
import re
import uuid
from typing import Optional

import httpx

from app.config import settings
from app.core.logger import get_debug_logger
from app.db.session import async_session_factory
from app.services import conversations as conv_service
from app.services.conversations import DEFAULT_TITLE
from app.prompts import format_prompt

logger = logging.getLogger(__name__)
dbg = get_debug_logger(__name__)


# ─── Cleaning helpers ───────────────────────────────────────────────

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_OPEN_THINK_RE = re.compile(r"<think>.*", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"```[a-zA-Z0-9_-]*\n?|```")
_LABEL_RE = re.compile(
    r"^\s*(title|titre|topic|subject|sujet)\s*[:\-–—]\s*", re.IGNORECASE
)
_QUOTE_CHARS = "\"'“”‘’«»`"
_TRAILING_PUNCT = " .!?…:;,、。！？：；，"

# Hard caps — the prompt asks for 2-3 words, but tiny models sometimes ignore
# it. These bounds keep a runaway title from wrecking the sidebar layout.
MAX_TITLE_WORDS = 6
MAX_TITLE_CHARS = 60
# First-line + prompt-snippet bounds.
_MAX_PROMPT_CHARS = 600
# Fallback truncation length (mirrors the frontend's optimistic preview).
_FALLBACK_MAX_CHARS = 40


def _clean_title(raw: str) -> str:
    """Normalize a raw LLM response into a sidebar-safe title.

    Handles the usual small-model pathologies: <think> reasoning, markdown
    fences, wrapping quotes, "Title:" labels, trailing commentary on later
    lines, and trailing punctuation. Returns "" when nothing usable remains.
    """
    if not raw:
        return ""
    text = str(raw)

    # Drop reasoning blocks (closed first, then any unterminated <think>).
    text = _THINK_RE.sub("", text)
    text = _OPEN_THINK_RE.sub("", text)
    # Drop markdown code fences.
    text = _FENCE_RE.sub("", text)

    # First non-empty line only — fence/think removal leaves blank lines
    # behind, and models often append commentary on later lines.
    non_empty = [ln.strip() for ln in text.split("\n") if ln.strip()]
    if not non_empty:
        return ""
    text = non_empty[0]

    # Strip wrapping quote characters from both ends.
    text = text.strip().strip(_QUOTE_CHARS).strip()

    # Drop a leading "Title:" style label, then re-strip quotes.
    text = _LABEL_RE.sub("", text)
    text = text.strip().strip(_QUOTE_CHARS).strip()

    # Collapse internal whitespace runs to single spaces.
    text = re.sub(r"\s+", " ", text).strip()

    # Strip trailing punctuation (keep inner punctuation like "C# vs Python").
    text = text.rstrip(_TRAILING_PUNCT).strip()

    # Cap word count, then character count.
    words = text.split(" ")
    if len(words) > MAX_TITLE_WORDS:
        text = " ".join(words[:MAX_TITLE_WORDS])
    if len(text) > MAX_TITLE_CHARS:
        text = text[:MAX_TITLE_CHARS].rstrip(_TRAILING_PUNCT).rstrip()

    return text.strip()


def _fallback_title(user_message: str) -> str:
    """Truncation fallback when the LLM is unavailable.

    Mirrors the frontend's optimistic preview (first 40 chars + ellipsis) so
    the persisted title stays consistent with what the user already saw.
    """
    text = re.sub(r"\s+", " ", (user_message or "").strip())
    if not text:
        return ""
    if len(text) > _FALLBACK_MAX_CHARS:
        text = text[:_FALLBACK_MAX_CHARS].rstrip() + "…"
    return text


# ─── LLM call ───────────────────────────────────────────────────────


async def generate_title(user_message: str) -> Optional[str]:
    """Generate a 2-3 word title from a conversation's first user message.

    Uses the utility model role (TITLE_GENERATION_MODEL_ROLE, default
    `default_utility`) resolved through profiles.yml — the same pattern as
    the memory extractor, so a profile that defines a small utility model
    gets cheap titles automatically.

    Returns the cleaned title, or None on any failure (the caller decides
    the fallback). Never raises.
    """
    try:
        model = settings.resolve_model(settings.TITLE_GENERATION_MODEL_ROLE)
        ollama_url = settings.OLLAMA_BASE_URL
        if not ollama_url or not model:
            dbg("no model or URL configured for title generation, skipping")
            return None

        snippet = (user_message or "").strip()
        if len(snippet) > _MAX_PROMPT_CHARS:
            snippet = snippet[:_MAX_PROMPT_CHARS] + "…"

        messages = [
            {"role": "system", "content": format_prompt("title_system")},
            {
                "role": "user",
                "content": (
                    "First message:\n"
                    f"{snippet}\n\n"
                    "Reply with the 2-3 word title now."
                ),
            },
        ]

        timeout = float(getattr(settings, "TITLE_GENERATION_TIMEOUT_SECONDS", 60))
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{ollama_url}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "think": False,
                    # Short, deterministic output; the hard cap also stops a
                    # chatty small model from wasting tokens.
                    "options": {"temperature": 0.2, "num_predict": 30},
                },
            )
            response.raise_for_status()
            raw = response.json().get("message", {}).get("content", "")

        title = _clean_title(raw)
        if not title:
            dbg("LLM title unusable: raw=%r", (raw or "")[:120])
            return None
        dbg("LLM title generated: raw=%r → %r", (raw or "")[:80], title)
        return title
    except Exception as e:
        logger.warning("[title-gen] LLM title generation failed: %s", e)
        return None


# ─── Entry point (used by chat.py) ──────────────────────────────────


def _is_default_title(title: Optional[str]) -> bool:
    """True when the title is empty or the default placeholder — i.e. the
    conversation has not been titled (or manually renamed) yet."""
    current = (title or "").strip()
    return (not current) or current.lower() == DEFAULT_TITLE.lower()


async def maybe_generate_and_save_title(
    conv_id: uuid.UUID, user_message: str
) -> Optional[str]:
    """Auto-title a conversation from its first user message (LLM-powered).

    Runs at most once per conversation: all conditions must hold —
    - the conversation exists
    - its title is still the default ("New Chat" / empty)
    - it contains exactly one user message (this is the first turn)

    On success: generates a title (LLM, with a truncation fallback), persists
    it, and returns it so the caller can emit a `conversation_title` SSE
    event. Returns None (and changes nothing) otherwise. Never raises.

    The DB work is split into two short sessions with the LLM call in
    between, so no connection is held during generation, and the second
    session re-checks the title right before writing — a manual rename that
    lands while the LLM is thinking is never overwritten.
    """
    if conv_id is None:
        return None
    if not settings.TITLE_GENERATION_ENABLED:
        return None

    try:
        # ── Phase 1 — eligibility check (fast DB reads only) ──
        async with async_session_factory() as db:
            conv = await conv_service.get_conversation(db, conv_id)
            if conv is None:
                return None
            if not _is_default_title(conv.title):
                return None  # already titled or manually renamed

            msgs = await conv_service.get_messages(db, conv_id)
            user_count = sum(
                1 for m in msgs if (getattr(m, "role", "") or "").lower() == "user"
            )
            if user_count != 1:
                return None  # not the first turn (or an empty conversation)

        # ── Phase 2 — LLM call (no DB connection held) ──
        title = await generate_title(user_message)
        if not title:
            title = _fallback_title(user_message)
        title = (title or "").strip()
        if not title:
            return None

        # ── Phase 3 — conditional update (re-check guards against a
        #    manual rename that landed during the LLM call) ──
        async with async_session_factory() as db:
            conv = await conv_service.get_conversation(db, conv_id)
            if conv is None:
                return None
            if not _is_default_title(conv.title):
                return None  # renamed meanwhile — don't clobber

            await conv_service.update_conversation_title(db, conv_id, title)
            await db.commit()

        dbg("conversation %s auto-titled: %r", conv_id, title)
        return title
    except Exception as e:
        logger.warning("[title-gen] auto-title failed (non-fatal): %s", e)
        return None
