"""
memory_extractor.py

Background auto-extraction of facts from chat conversations.

Trigger logic:
- Called from chat.py as a background asyncio task AFTER the assistant
  message is persisted, but ONLY when the number of NEW messages since
  the conversation's `memory_watermark_message_id` reaches the configured
  threshold (default 4, see settings.MEMORY_EXTRACTION_INTERVAL).
- The watermark is updated to the latest message ID after extraction
  runs, so the same messages are never re-extracted across restarts or
  parallel requests.
- This replaces the previous behavior of running on every turn from the
  2nd message onwards, which was wasteful and re-processed old messages.

Deduplication (3-tier, with content-aware vector guard):
  1. Vector similarity (pgvector cosine) >= 0.85 AND content_jaccard
     >= 0.10 → drop. The content guard prevents false positives where
     two unrelated short facts get high cosine similarity from shared
     boilerplate words ("the user", "is", "named").
  2. Exact case-insensitive text match → drop
  3. Jaccard token similarity >= 0.6 → drop

NOTE ON LLM OUTPUT CLEANING:
- Small models often ignore the "return only JSON" instruction and wrap
  the output in ```json ... ``` fences, prepend <think>...</think>
  reasoning, or add commentary. We strip all of these before parsing.

Audit / consolidation:
- Triggered automatically when the DB-persisted counter
  `memory.extractions_since_audit` reaches AUDIT_INTERVAL (default 5).
- Uses an LLM with a conservative prompt: merge only true duplicates,
  remove only worthless entries, refuse to cut >50% of memories.
- Fingerprint short-circuit: if the memory set hasn't changed since the
  last audit, the LLM call is skipped entirely.
- The fingerprint is persisted in app_state, so it survives restarts.

The extraction prompt is fixed (missing-period bug from previous version
is corrected). The extraction model defaults to the chat model via the
`default_utility` role, which can be overridden in profiles.yml to a
smaller model for cheaper background work.
"""

import json
import re
import uuid
from datetime import datetime
from typing import List, Optional, Tuple

import httpx
from sqlalchemy import select

from app.config import settings
from app.db.models import Conversation, Message
from app.db.session import async_session_factory
from app.services.memory import (
    MemoryManager,
    _fingerprint_memories,
    _get_audit_fingerprint,
    _increment_extractions_since_audit,
    _reset_extractions_since_audit,
    _set_audit_fingerprint,
    get_text_similarity,
)

# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM_PROMPT = (
    "You are a memory extraction assistant. Analyze the conversation and extract ONLY "
    "durable personal facts about the user that would be useful across many future conversations.\n\n"
    "Good examples: name, job title, city, family members, long-term projects, strong preferences.\n"
    "Bad examples: what they asked about today, temporary moods, generic statements, "
    "things the assistant said, one-off tasks, opinions on the current topic.\n\n"
    "Rules:\n"
    "- MAX 2 facts per conversation — only the most important\n"
    "- Only extract facts the USER stated or clearly implied\n"
    "- Each fact must be a single short sentence (under 15 words)\n"
    "- If a fact is similar to something likely already known, skip it\n"
    "- If nothing durable was revealed, return []\n\n"
    "Make a best effort to follow the rules, but when in doubt, EXTRACT RATHER THAN SKIP.\n\n"
    "Return a JSON array of objects with 'text' and 'category' fields.\n"
    "Categories: 'identity', 'preference', 'fact', 'contact', 'project', 'goal'\n\n"
    "Return ONLY valid JSON, no markdown fences, no extra commentary."
)

AUDIT_SYSTEM_PROMPT = """
You are a memory database auditor.

Goal:
Reduce redundancy WITHOUT losing information.

DEFAULT ACTION: KEEP.

Deletion or merge requires HIGH CONFIDENCE that no information is lost.

Procedure:

Step 1 — Classify each memory:
- identity → stable personal attributes
- fact → concrete factual statement
- preference → likes/dislikes/tendencies
- project → goals, work, plans, initiatives
- other

Step 2 — Compare memories pairwise.

MERGE only if ALL are true:
A. Same subject
B. Same category
C. Same information content
D. One can be removed with ZERO loss of meaning

Examples:
MERGE:
- "User's name is Sam"
- "The user is called Sam"

KEEP BOTH:
- "User likes Python"
- "User uses Python at work"

KEEP BOTH:
- "User works on cloud cost optimization"
- "User likes DevOps"

KEEP BOTH:
- "User lives in Casablanca"
- "User name is Abdel and lives in Casablanca"
(composite memories are NOT replacements)

KEEP BOTH:
- Specific fact vs broader summary
  Example:
  "User has Cloud Cost Optimizer project"
  +
  "User prefers DevOps projects"

→ KEEP BOTH.

Step 3 — Remove only:
- empty text
- malformed entries
- AI-behavior statements
- exact duplicates

Rules:
- NEVER generalize.
- NEVER replace specific memories with broader summaries.
- NEVER infer equivalence.
- Prefer redundancy over deletion.
- Preserve original wording.
- Preserve id of kept entries.
- Output entries in original order.

Return ONLY:
[
  {
    "id": "...",
    "text": "...",
    "category": "..."
  }
]
"""


# ---------------------------------------------------------------------------
# LLM output cleaning — handles markdown fences, <think> tags, trailing
# commas, and surrounding commentary that small models (qwen3.5:4b-mlx)
# add even when told not to.
# ---------------------------------------------------------------------------


def _strip_think_tags(text: str) -> str:
    """Remove <think>...</think> or <thinking>...</thinking> blocks."""
    return re.sub(
        r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", text, flags=re.I
    ).strip()


def _extract_json_list(raw: str) -> Optional[list]:
    """Best-effort extraction of a JSON list from a noisy LLM response.

    Tries (in order):
      1. Strip <think> tags, then direct json.loads.
      2. Strip ```json ... ``` or ``` ... ``` fences, then json.loads.
      3. Find the first '[' and last ']' and json.loads the slice.
      4. Repair trailing commas and retry.

    Returns the parsed list, or None if nothing parseable was found.
    """
    if not raw:
        return None

    text = _strip_think_tags(raw)

    def _loads_list(s: str) -> Optional[list]:
        if not s:
            return None
        # Try as-is, then with trailing commas removed (small models often
        # emit `{"text": "x",}` which is invalid JSON).
        for cand in (s, re.sub(r",(\s*[}\]])", r"\1", s)):
            try:
                v = json.loads(cand)
                if isinstance(v, list):
                    return v
            except Exception:
                continue
        return None

    # 1. Direct parse
    parsed = _loads_list(text)
    if parsed is not None:
        return parsed

    # 2. Fenced code block (```json ... ``` or ``` ... ```)
    m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", text)
    if m:
        parsed = _loads_list(m.group(1).strip())
        if parsed is not None:
            return parsed

    # 3. First '[' to last ']' slice
    a, b = text.find("["), text.rfind("]")
    if a >= 0 and b > a:
        parsed = _loads_list(text[a : b + 1])
        if parsed is not None:
            return parsed

    return None


# ---------------------------------------------------------------------------
# Message helpers
# ---------------------------------------------------------------------------


def _message_text(message) -> str:
    content = getattr(message, "content", None)
    if content is None and isinstance(message, dict):
        content = message.get("content")
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: List[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return " ".join(p for p in parts if p).strip()
    return ""


def _message_role(message) -> str:
    role = getattr(message, "role", None)
    if role is None and isinstance(message, dict):
        role = message.get("role")
    return str(role or "").lower()


# ---------------------------------------------------------------------------
# Clean / validate
# ---------------------------------------------------------------------------


def _clean_memory_value(value: str, max_len: int = 80) -> str:
    value = re.sub(r"\s+", " ", value or "").strip(
        " .,!?:;\"'`\u201c\u201d\u2018\u2019"
    )
    value = re.sub(r"^(?:the|a|an)\s+", "", value, flags=re.I)
    if not value or len(value) > max_len:
        return ""
    if re.search(r"https?://|@|[{}<>]", value):
        return ""
    return value


# ---------------------------------------------------------------------------
# Fallback regex extraction (Unicode-aware — supports Clémence, Søren, etc.)
# ---------------------------------------------------------------------------


# \w with re.UNICODE matches accented letters, so this catches names like
# "Clémence", "Søren", "François" that the [A-Za-z]+ pattern misses.
_NAME_PATTERN = r"[\w][\w .'\-]{1,50}"


def _fallback_memory_candidates(messages) -> List[dict]:
    """Extract obvious durable facts without relying on the LLM.

    Deliberately narrow. The LLM remains the main extractor, but simple
    identity/preference/goal statements should not silently vanish just
    because the background model judged them too conversational.
    """
    candidates: List[dict] = []
    seen: set[str] = set()

    def add(text: str, category: str):
        text = _clean_memory_value(text, 120)
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append({"text": text, "category": category})

    for msg in messages:
        if _message_role(msg) != "user":
            continue
        text = _message_text(msg)
        if not text:
            continue

        m = re.search(rf"\bmy name is\s+({_NAME_PATTERN})\b", text, re.I | re.UNICODE)
        if m:
            name = _clean_memory_value(m.group(1), 50)
            if name:
                add(f"User's name is {name}.", "identity")

        m = re.search(rf"\bcall me\s+({_NAME_PATTERN})\b", text, re.I | re.UNICODE)
        if m:
            name = _clean_memory_value(m.group(1), 50)
            if name:
                add(f"User wants to be called {name}.", "identity")

        m = re.search(
            r"\bi (?:live in|am from|'m from)\s+([^.!?\n]{2,80})",
            text,
            re.I | re.UNICODE,
        )
        if m:
            place = _clean_memory_value(m.group(1), 80)
            if place:
                add(f"User lives in {place}.", "identity")

        m = re.search(
            r"\bi (?:prefer|like|love|hate|do not like|don't like)\s+([^.!?\n]{4,100})",
            text,
            re.I | re.UNICODE,
        )
        if m:
            preference = _clean_memory_value(m.group(1), 100)
            if preference:
                add(f"User prefers {preference}.", "preference")

        # "My girlfriend is X", "My brother is X" etc.
        m = re.search(
            rf"\bmy (girlfriend|boyfriend|wife|husband|partner|sister|brother|"
            rf"mother|mom|father|dad|son|daughter|friend|colleague|boss)\s+"
            rf"(?:is|'s)\s+({_NAME_PATTERN})(?:[^.!?\n]{{0,80}})?",
            text,
            re.I | re.UNICODE,
        )
        if m:
            relation = m.group(1).lower()
            name = _clean_memory_value(m.group(2), 50)
            if name:
                add(f"User's {relation} is {name}.", "fact")

        m = re.search(
            r"\bi (?:(?:want|would like|plan|hope) to|wanna) "
            r"(?:go|travel|move|visit) to\s+([^.!?\n]{2,80})",
            text,
            re.I | re.UNICODE,
        )
        if m:
            destination = _clean_memory_value(m.group(1), 80)
            if destination:
                add(f"User wants to visit {destination}.", "goal")

    return candidates[:2]


# ---------------------------------------------------------------------------
# Main extraction function
# ---------------------------------------------------------------------------


async def extract_and_store(
    messages: list,
    conversation_id: Optional[str] = None,
) -> int:
    """Extract facts from recent conversation and store them.

    Designed to run as a background task (asyncio.create_task).
    Errors are logged, never raised.
    Returns count of added memories.
    """
    # Resolve the utility model — falls back to the chat model if no
    # default_utility role is defined in profiles.yml.
    model = settings.resolve_model(settings.MEMORY_EXTRACTION_MODEL_ROLE)
    ollama_url = settings.OLLAMA_BASE_URL

    if not ollama_url or not model:
        print("[memory-extract] no model or URL configured, skipping")
        return 0

    try:
        # The caller (chat.py) already slices to the last N messages and
        # includes the just-generated assistant response. We just need
        # at least 2 (a user + assistant pair).
        if len(messages) < 2:
            return 0

        print(
            "[memory-extract] launching extraction task (%d messages, model=%s)",
            len(messages),
            model,
        )

        # Strip media from messages — only need text for extraction.
        stripped_recent: List[dict] = []
        for msg in messages:
            role = msg.get("role") if isinstance(msg, dict) else _message_role(msg)
            content = (
                msg.get("content", "") if isinstance(msg, dict) else _message_text(msg)
            )
            if isinstance(content, list):
                text_only = [
                    b
                    for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                if not text_only and content:
                    continue
                content = text_only
            if role in ("user", "assistant") and content:
                stripped_recent.append({"role": role, "content": content})

        if not stripped_recent:
            return 0

        fallback_facts = _fallback_memory_candidates(
            [m for m in stripped_recent if m["role"] == "user"]
        )

        # ── FLATTENED TRANSCRIPT ──────────────────
        # Small local models (qwen3:4b, etc.) treat alternating-role
        # messages as a conversation to CONTINUE rather than a transcript
        # to ANALYZE, so they reliably return [] — they "answer" instead
        # of extracting. Controlled repro on qwen3.5:4b-mlx: 0/6 trials
        # with alternating roles vs 6/6 with a flattened single user
        # message. So we flatten the whole window into ONE user message
        # labeled "Conversation to analyze".
        transcript_parts: List[str] = []
        for m in stripped_recent:
            role_label = m["role"].upper()
            text = m["content"] if isinstance(m["content"], str) else str(m["content"])
            # Cap each turn to keep the total payload small for 4B models.
            if len(text) > 1200:
                text = text[:1200] + "…"
            transcript_parts.append(f"{role_label}: {text}")
        transcript = "\n\n".join(transcript_parts)

        extraction_messages = [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    "Conversation to analyze:\n\n"
                    + transcript
                    + "\n\nReturn the JSON array of durable facts now (or [] if none)."
                ),
            },
        ]

        facts: list = []
        rounds = 0
        while rounds < 2:
            print(
                "[memory-extract] sending %d messages to LLM (round %d)",
                len(stripped_recent),
                rounds + 1,
            )
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    print(
                        {
                            "model": model,
                            "messages": extraction_messages,
                            "stream": False,
                            "think": False,
                            "format": "json",
                        }
                    )
                    response = await client.post(
                        f"{ollama_url}/api/chat",
                        json={
                            "model": model,
                            "messages": extraction_messages,
                            "stream": False,
                            "think": False,
                            "format": "json",
                        },
                    )
                    response.raise_for_status()
                    print(
                        f"[memory-extract] raw LLM response: ================================\n{response.text}..."
                    )
                    raw = response.json().get("message", {}).get("content", "")

                # Parse JSON from response (handles markdown fences, <think>
                # tags, surrounding commentary, trailing commas — small
                # models like qwen3.5:4b-mlx often produce all of these).
                text = raw.strip()
                print(
                    "[memory-extract] LLM returned (round %d): %s",
                    rounds + 1,
                    text[:200],
                )
                parsed = _extract_json_list(text)
                if isinstance(parsed, list):
                    facts = parsed
                    break
                print(
                    "[memory-extract] JSON extraction failed (round %d): %s",
                    rounds + 1,
                    text[:200],
                )
            except Exception as e:
                print("[memory-extract] LLM call failed (round %d): %s", rounds + 1, e)
            rounds += 1

        if not isinstance(facts, list):
            facts = []

        if fallback_facts:
            facts = list(facts) + fallback_facts

        if not facts:
            print("[memory-extract] ran: 0 candidates")
            return 0

        # Store new memories via the memory service
        added = 0
        async with async_session_factory() as db:
            manager = MemoryManager()

            # Get existing memories for the text-fallback dedup tiers
            existing_memories = await manager.get_all_memories(db, limit=10000)

            for fact in facts:
                if isinstance(fact, str):
                    fact_text = fact
                    category = "fact"
                elif isinstance(fact, dict):
                    fact_text = fact.get("text", "").strip()
                    category = fact.get("category", "fact")
                else:
                    continue

                if not fact_text or len(fact_text) < 5:
                    continue

                # Tier 1: vector similarity (preferred — catches paraphrases)
                try:
                    similar = await manager.find_similar_by_vector(
                        db,
                        fact_text,
                        threshold=settings.MEMORY_DEDUP_VECTOR_THRESHOLD,
                    )
                    if similar is not None:
                        print(
                            "[memory-extract] vector dedup: '%s...' matches '%s...'",
                            fact_text[:50],
                            similar.text[:50],
                        )
                        continue
                except Exception as e:
                    print(
                        "[memory-extract] vector dedup unavailable, using text tiers: %s",
                        e,
                    )

                # Tier 2: exact match
                if any(
                    (m.text or "").lower() == fact_text.lower()
                    for m in existing_memories
                ):
                    continue

                # Tier 3: Jaccard similarity
                is_dup = False
                for m in existing_memories:
                    if (
                        get_text_similarity(fact_text, m.text or "")
                        >= settings.MEMORY_DEDUP_TEXT_THRESHOLD
                    ):
                        is_dup = True
                        break
                if is_dup:
                    print(
                        "[memory-extract] fuzzy dedup: '%s...' too similar to existing",
                        fact_text[:50],
                    )
                    continue

                try:
                    memory = await manager.add_memory(
                        db,
                        fact_text,
                        category=category,
                        source="auto",
                        conversation_id=conversation_id,
                    )
                    existing_memories.append(memory)
                    added += 1
                except Exception as e:
                    print("[memory-extract] failed to store memory: %s", e)

            if added > 0:
                await db.commit()
                print(
                    "[memory-extract] auto-extracted %d memories from conversation %s",
                    added,
                    conversation_id,
                )

                # Increment the DB-persisted audit counter and trigger audit
                # when the threshold is reached (default: every 5).
                new_count = await _increment_extractions_since_audit(db, added)
                if new_count >= settings.MEMORY_AUDIT_INTERVAL:
                    print(
                        "[memory-extract] audit threshold reached (%d >= %d), "
                        "running memory audit",
                        new_count,
                        settings.MEMORY_AUDIT_INTERVAL,
                    )
                    await _reset_extractions_since_audit(db)
                    await db.commit()
                    # Run audit in a fresh session to avoid transaction conflicts
                    await audit_memories()
                else:
                    await db.commit()
            else:
                print("[memory-extract] ran: 0 added (all duplicates)")
                await db.commit()

        return added

    except Exception as e:
        print("[memory-extract] extraction failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Audit / consolidation
# ---------------------------------------------------------------------------


async def audit_memories() -> dict:
    """Send all memories to the LLM for deduplication and consolidation.

    - Merges near-duplicate entries
    - Rewrites vague entries to be concise
    - Removes junk / non-personal entries

    Safe to call manually or from the automatic trigger.
    Errors are logged, never raised.
    """
    model = settings.resolve_model(settings.MEMORY_AUDIT_MODEL_ROLE)
    ollama_url = settings.OLLAMA_BASE_URL

    if not ollama_url or not model:
        print("[memory-audit] no model or URL configured, skipping")
        return {"error": "no_model"}

    try:
        async with async_session_factory() as db:
            manager = MemoryManager()
            existing = await manager.get_all_memories(db, limit=10000)

            if not existing:
                print("[memory-audit] nothing to audit")
                return {"before": 0, "after": 0}

            before_count = len(existing)

            # Fingerprint short-circuit: skip the LLM call entirely if the
            # memory set hasn't changed since the last audit.
            current_fp = _fingerprint_memories(existing)
            last_fp = await _get_audit_fingerprint(db)
            if last_fp == current_fp:
                print("[memory-audit] state unchanged since last audit — skipping LLM")
                return {
                    "before": before_count,
                    "after": before_count,
                    "already_tidy": True,
                }

            # Build payload: just {id, text, category} for the LLM
            memory_payload = [
                {"id": str(m.id), "text": m.text, "category": m.category}
                for m in existing
            ]

            audit_messages = [
                {"role": "system", "content": AUDIT_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(memory_payload, ensure_ascii=False),
                },
            ]

            raw = ""
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    print("[memory-audit] sending %d memories to LLM", len(existing))
                    response = await client.post(
                        f"{ollama_url}/api/chat",
                        json={
                            "model": model,
                            "messages": audit_messages,
                            "stream": False,
                            "think": False,
                            "format": "json",
                        },
                    )
                    response.raise_for_status()
                    raw = response.json().get("message", {}).get("content", "")
            except Exception as e:
                print("[memory-audit] LLM call failed: %s", e)
                return {
                    "before": before_count,
                    "after": before_count,
                    "error": "llm_failed",
                }

            # Parse the JSON list, tolerating reasoning-model noise
            # (delegates to the shared _extract_json_list helper that
            # handles <think> tags, ```json fences, trailing commas,
            # and surrounding commentary).
            cleaned = _extract_json_list(raw or "")
            if cleaned is None:
                print("[memory-audit] non-JSON response: %s", (raw or "")[:300])
                return {
                    "before": before_count,
                    "after": before_count,
                    "error": "bad_json",
                }

            # Build lookup of original entries by ID so we can preserve metadata
            originals = {str(m.id): m for m in existing}

            final_ids: set[str] = set()
            for item in cleaned:
                if not isinstance(item, dict):
                    continue
                mid = item.get("id", "")
                new_text = (item.get("text") or "").strip()
                if not new_text:
                    continue

                if mid in originals:
                    memory = originals[mid]
                    # Update text + regenerate embedding if text changed
                    if new_text != (memory.text or ""):
                        memory.text = new_text
                        from app.services.embeddings import get_embedding

                        memory.embedding = await get_embedding(new_text)
                    if item.get("category"):
                        memory.category = item["category"]
                    memory.updated_at = datetime.utcnow()
                    final_ids.add(mid)
                else:
                    print("[memory-audit] unknown id %s, skipping", mid)

            after_count = len(final_ids)

            # Safety net against catastrophic over-deletion
            if before_count >= 8 and after_count < before_count * 0.5:
                print(
                    "[memory-audit] would cut %d -> %d (>50%% removed) — refusing as unsafe",
                    before_count,
                    after_count,
                )
                return {
                    "before": before_count,
                    "after": before_count,
                    "error": "unsafe_removal",
                }

            # Delete memories that were not in the cleaned list
            for mid_str, memory in originals.items():
                if mid_str not in final_ids:
                    await db.delete(memory)

            # Persist the new fingerprint so we short-circuit next time
            # if nothing has changed. Read the survivors back to compute it.
            await db.flush()
            survivors = await manager.get_all_memories(db, limit=10000)
            new_fp = _fingerprint_memories(survivors)
            await _set_audit_fingerprint(db, new_fp)

            await db.commit()

            print(
                "[memory-audit] complete: %d -> %d entries (%d removed/merged)",
                before_count,
                after_count,
                before_count - after_count,
            )

            return {"before": before_count, "after": after_count}

    except Exception as e:
        print("[memory-audit] failed: %s", e)
        return {"error": str(e)}


# ---------------------------------------------------------------------------
# Watermark helpers — used by chat.py to decide when to trigger extraction
# ---------------------------------------------------------------------------


async def get_messages_since_watermark(
    db,
    conversation_id: str,
) -> Tuple[List[Message], Optional[uuid.UUID]]:
    """Return (new_messages, current_watermark_id) for a conversation.

    new_messages is the list of messages AFTER the watermark, ordered by
    created_at. If the watermark is NULL, returns ALL messages.
    """
    try:
        conv_uuid = uuid.UUID(str(conversation_id))
    except (ValueError, AttributeError):
        return [], None

    conv = await db.get(Conversation, conv_uuid)
    if not conv:
        return [], None

    stmt = select(Message).where(Message.conversation_id == conv_uuid)
    if conv.memory_watermark_message_id is not None:
        # Messages strictly after the watermark message, by created_at.
        # Using created_at avoids depending on auto-increment IDs.
        watermark_msg = await db.get(Message, conv.memory_watermark_message_id)
        if watermark_msg is not None:
            stmt = stmt.where(Message.created_at > watermark_msg.created_at)
    stmt = stmt.order_by(Message.created_at.asc())

    result = await db.execute(stmt)
    new_messages = list(result.scalars().all())
    return new_messages, conv.memory_watermark_message_id


async def update_watermark(
    db,
    conversation_id: str,
    message_id: uuid.UUID,
) -> None:
    """Set the conversation's watermark to the given message ID."""
    try:
        conv_uuid = uuid.UUID(str(conversation_id))
    except (ValueError, AttributeError):
        return
    conv = await db.get(Conversation, conv_uuid)
    if conv:
        conv.memory_watermark_message_id = message_id
        conv.updated_at = datetime.utcnow()
        await db.flush()
