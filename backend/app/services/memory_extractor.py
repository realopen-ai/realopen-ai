"""
memory_extractor.py

Background auto-extraction of facts from chat conversations.
After each LLM response, this module sends the last few messages to the
local Ollama model asking it to extract memorable facts, then stores them
in PostgreSQL via the memory service.

Periodically audits all memories via LLM to consolidate duplicates,
rewrite vague entries, and remove junk.
"""

import hashlib
import json
import logging
import re
from typing import Optional

import httpx

from app.config import settings
from app.db.session import async_session_factory
from app.services.memory import MemoryManager

logger = logging.getLogger(__name__)

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
    "Make a best effort to follow the rules, but when in doubt, EXTRACT RATHER THAN SKIP."
    "Return a JSON array of objects with 'text' and 'category' fields.\n"
    "Categories: 'identity', 'preference', 'fact', 'contact', 'project', 'goal'\n\n"
    "Return ONLY valid JSON, no markdown fences, no extra commentary."
)

AUDIT_SYSTEM_PROMPT = (
    "You are a memory database curator. Be CONSERVATIVE: remove only TRUE "
    "duplicates and clearly useless entries. Every distinct fact must survive. "
    "When in doubt, KEEP the entry. Return the cleaned list.\n\n"
    "Rules:\n"
    "1. MERGE only entries that state the SAME fact in different words. If you "
    "are not sure two entries are the same fact, KEEP BOTH.\n"
    "   Merge: 'User's name is Sam' + 'The user is called Sam' -> one.\n"
    "   Do NOT merge related-but-distinct facts: 'Likes Python' and 'Uses "
    "Python at work' are DIFFERENT — keep both.\n"
    "2. REMOVE only entries that are genuinely worthless: about what the AI did "
    "(not the user), empty, or meaningless. Do NOT drop a real fact just "
    "because it seems minor or niche.\n"
    "3. Keep the original wording. Only lightly trim obvious redundancy — do "
    "NOT aggressively rewrite or shorten.\n"
    "4. Preserve the 'id' of the entry you keep when merging.\n"
    "5. Never invent facts. When unsure, KEEP.\n\n"
    "Return a JSON array of objects with fields: id, text, category.\n"
    "Return ONLY valid JSON, no markdown fences, no extra commentary."
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

CONTEXT_WINDOW = 6
AUDIT_INTERVAL = 5
_extractions_since_audit = 0

# ---------------------------------------------------------------------------
# Fingerprinting for audit short-circuit
# ---------------------------------------------------------------------------


def _fingerprint_entries(entries) -> str:
    """Stable hash of memories — order-independent, depends on id+text+category."""
    items = sorted(
        (str(e.get("id", "")), e.get("text", ""), e.get("category", ""))
        for e in _memory_dicts(entries)
    )
    h = hashlib.sha256()
    for triple in items:
        h.update(("\x1f".join(triple) + "\x1e").encode("utf-8"))
    return h.hexdigest()


def _memory_dicts(entries):
    for entry in entries or []:
        if isinstance(entry, dict):
            yield entry


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
        parts = []
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
# Fallback regex extraction
# ---------------------------------------------------------------------------


def _fallback_memory_candidates(messages) -> list:
    """Extract obvious durable facts without relying on the LLM.

    This is deliberately narrow. The LLM remains the main extractor, but
    simple identity/preference/goal statements should not silently vanish just
    because the background model judged them too conversational.
    """
    candidates = []
    seen = set()

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

        m = re.search(r"\bmy name is\s+([A-Za-z][A-Za-z0-9 .'\-]{1,50})\b", text, re.I)
        if m:
            name = _clean_memory_value(m.group(1), 50)
            if name:
                add(f"User's name is {name}.", "identity")

        m = re.search(r"\bcall me\s+([A-Za-z][A-Za-z0-9 .'\-]{1,50})\b", text, re.I)
        if m:
            name = _clean_memory_value(m.group(1), 50)
            if name:
                add(f"User wants to be called {name}.", "identity")

        m = re.search(r"\bi (?:live in|am from|'m from)\s+([^.!?\n]{2,80})", text, re.I)
        if m:
            place = _clean_memory_value(m.group(1), 80)
            if place:
                add(f"User lives in {place}.", "identity")

        m = re.search(
            r"\bi (?:prefer|like|love|hate|do not like|don't like)\s+([^.!?\n]{4,100})",
            text,
            re.I,
        )
        if m:
            preference = _clean_memory_value(m.group(1), 100)
            if preference:
                add(f"User prefers {preference}.", "preference")

        m = re.search(
            r"\bi (?:(?:want|would like|plan|hope) to|wanna) "
            r"(?:go|travel|move|visit) to\s+([^.!?\n]{2,80})",
            text,
            re.I,
        )
        if m:
            destination = _clean_memory_value(m.group(1), 80)
            if destination:
                add(f"User wants to visit {destination}.", "goal")

    return candidates[:2]


# ---------------------------------------------------------------------------
# Text-based dedup check
# ---------------------------------------------------------------------------


def _is_text_duplicate(new_text: str, existing: list, threshold: float = 0.6) -> bool:
    """Check if new_text is too similar to any existing memory (Jaccard similarity)."""
    new_tokens = set(new_text.lower().split())
    if not new_tokens:
        return False
    for entry in _memory_dicts(existing):
        old_tokens = set(entry.get("text", "").lower().split())
        if not old_tokens:
            continue
        intersection = new_tokens & old_tokens
        union = new_tokens | old_tokens
        if len(intersection) / len(union) >= threshold:
            return True
    return False


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
    global _extractions_since_audit

    model = settings.resolve_model("default")
    ollama_url = settings.OLLAMA_BASE_URL

    if not ollama_url or not model:
        print("[memory-extract] No model or URL configured, skipping")
        return 0

    try:
        recent = (
            messages[-CONTEXT_WINDOW:] if len(messages) > CONTEXT_WINDOW else messages
        )

        if len(recent) < 2:
            return 0  # Need at least a user message and assistant response

        print("   🔄 launching memory extraction task (last 6 messages)")

        # Strip media from messages — only need text for extraction
        stripped_recent = []
        for msg in recent:
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
            stripped_recent.append({"role": role, "content": content})

        if not stripped_recent:
            return 0

        fallback_facts = _fallback_memory_candidates(stripped_recent)

        extraction_messages = [
            {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
        ] + stripped_recent

        facts = []
        rounds = 0
        while rounds < 2:
            print(
                f"[memory-extract] Sending {len(stripped_recent)} messages to LLM for extraction... | round #{rounds + 1}"
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

                # Parse JSON from response (handle markdown fences)
                text = raw.strip()
                if text.startswith("```"):
                    text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

                try:
                    facts = json.loads(text)
                    break  # Success
                except json.JSONDecodeError:
                    print(
                        f"[memory-extract] JSON decode failed, response was: </><|><\\>\n{text}..."
                    )
            except Exception as e:
                logger.warning(
                    f"LLM memory extraction failed; using fallback candidates if available: {e}"
                )
                print(f"[memory-extract] LLM call failed:\n{e}")
            rounds += 1

        if not isinstance(facts, list):
            facts = []

        if fallback_facts:
            facts = list(facts) + fallback_facts

        if not facts:
            logger.info("Auto memory extraction ran: 0 candidates")
            print("[memory-extract] Auto memory extraction ran: 0 candidates")
            return 0

        # Store new memories via the memory service
        added = 0
        async with async_session_factory() as db:
            manager = MemoryManager()

            # Get existing memories for dedup
            existing_memories = await manager.get_all_memories(db, limit=10000)
            existing_dicts = [
                {"id": str(m.id), "text": m.text, "category": m.category}
                for m in existing_memories
            ]

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

                # Dedup: exact text match
                exact_dup = any(
                    m.text.lower() == fact_text.lower() for m in existing_memories
                )
                if exact_dup:
                    continue

                # Fuzzy text similarity check
                if _is_text_duplicate(fact_text, existing_dicts):
                    logger.debug(
                        f"Memory dedup (fuzzy): '{fact_text[:50]}' too similar to existing"
                    )
                    print(
                        f"[memory-extract] Memory dedup (fuzzy): '{fact_text[:50]}' too similar to existing"
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
                    existing_dicts.append(
                        {
                            "id": str(memory.id),
                            "text": memory.text,
                            "category": memory.category,
                        }
                    )
                    added += 1
                except Exception as e:
                    logger.warning(f"Failed to store memory: {e}")
                    print(f"[memory-extract] Failed to store memory: {e}")

            if added > 0:
                await db.commit()
                logger.info(f"Auto-extracted {added} memories from conversation")
                print(
                    f"[memory-extract] Auto-extracted {added} memories from conversation"
                )

                _extractions_since_audit += added
                if _extractions_since_audit >= AUDIT_INTERVAL:
                    _extractions_since_audit = 0
                    logger.info("Audit threshold reached, running memory audit")
                    print(
                        "[memory-extract] Audit threshold reached, running memory audit"
                    )
                    await audit_memories()
            else:
                logger.info("Auto memory extraction ran: 0 added")
                print("[memory-extract] Auto memory extraction ran: 0 added")

        return added

    except Exception as e:
        logger.error(f"Memory extraction failed: {e}")
        print(f"[memory-extract] Memory extraction failed: {e}")
        return 0


# ---------------------------------------------------------------------------
# Audit / consolidation
# ---------------------------------------------------------------------------

# In-memory fingerprint to skip redundant audits
_last_audit_fingerprint: Optional[str] = None


async def audit_memories() -> dict:
    """Send all memories to the LLM for deduplication and consolidation.

    - Merges near-duplicate entries
    - Rewrites vague entries to be concise
    - Removes junk / non-personal entries

    Safe to call manually or from the automatic trigger.
    Errors are logged, never raised.
    """
    global _last_audit_fingerprint

    model = settings.resolve_model("default")
    ollama_url = settings.OLLAMA_BASE_URL

    if not ollama_url or not model:
        logger.debug("[memory-audit] No model or URL configured, skipping")
        return {"error": "no_model"}

    try:
        async with async_session_factory() as db:
            manager = MemoryManager()
            existing = await manager.get_all_memories(db, limit=10000)

            if not existing:
                print("[memory-audit] Memory audit: nothing to audit")
                return {"before": 0, "after": 0}

            before_count = len(existing)

            # Skip the LLM call entirely when this exact set of memories was
            # already audited
            current_fp = _fingerprint_entries(
                [
                    {"id": str(m.id), "text": m.text, "category": m.category}
                    for m in existing
                ]
            )
            if _last_audit_fingerprint == current_fp:
                print("Memory audit: state unchanged since last tidy — skipping LLM")
                return {
                    "before": before_count,
                    "after": before_count,
                    "already_tidy": True,
                }

            # Build payload: list of {id, text, category} for the LLM
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
                    print(
                        f"[memory-audit] Sending {len(existing)} memories to LLM for audit..."
                    )
                    print(
                        f"[memory-audit] Audit system prompt: {AUDIT_SYSTEM_PROMPT[:200]}..."
                    )
                    print("[memory-audit] Audit payload:")
                    print(
                        {
                            "model": model,
                            "messages": audit_messages,
                            "stream": False,
                            "think": False,
                            "format": "json",
                        }
                    )
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
                    print(
                        f"[memory-audit] raw LLM response: ================================\n{raw}"
                    )
            except Exception as e:
                logger.error(f"Memory audit LLM call failed: {e}")
                return {
                    "before": before_count,
                    "after": before_count,
                    "error": "llm_failed",
                }

            # Parse the JSON list, tolerating reasoning-model noise
            text = (raw or "").strip()
            text = re.sub(
                r"<think(?:ing)?>[\s\S]*?</think(?:ing)?>", "", text, flags=re.I
            ).strip()

            def _loads_list(s):
                if not s:
                    return None
                for cand in (s, re.sub(r",(\s*[}\]])", r"\1", s)):
                    try:
                        v = json.loads(cand)
                        if isinstance(v, list):
                            return v
                    except Exception:
                        continue
                return None

            cleaned = _loads_list(text)
            if cleaned is None:
                _m = re.search(r"```(?:json)?\s*\n?([\s\S]*?)```", text)
                if _m:
                    cleaned = _loads_list(_m.group(1).strip())
            if cleaned is None:
                _a, _b = text.find("["), text.rfind("]")
                if _a >= 0 and _b > _a:
                    cleaned = _loads_list(text[_a : _b + 1])
            if cleaned is None:
                logger.error(f"Memory audit returned non-JSON: {text[:300]}")
                return {
                    "before": before_count,
                    "after": before_count,
                    "error": "bad_json",
                }

            # Build lookup of original entries by ID so we can preserve metadata
            originals = {str(m.id): m for m in existing}

            final_ids = set()
            for item in cleaned:
                if not isinstance(item, dict):
                    continue
                mid = item.get("id", "")
                new_text = item.get("text", "").strip()
                if not new_text:
                    continue

                if mid in originals:
                    memory = originals[mid]
                    memory.text = new_text
                    if item.get("category"):
                        memory.category = item["category"]
                    final_ids.add(mid)
                else:
                    logger.debug(f"Audit returned unknown id {mid}, skipping")
                    continue

            after_count = len(final_ids)

            # Safety net against catastrophic over-deletion
            if before_count >= 8 and after_count < before_count * 0.5:
                print(
                    f"[memory-audit] Memory audit would cut {before_count} -> {after_count} "
                    f"(>50% removed) — refusing as unsafe, keeping originals"
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

            await db.commit()

            # Update fingerprint
            remaining = await manager.get_all_memories(db, limit=10000)
            _last_audit_fingerprint = _fingerprint_entries(
                [
                    {"id": str(m.id), "text": m.text, "category": m.category}
                    for m in remaining
                ]
            )

            print(
                f"[memory-audit] Memory audit complete: {before_count} -> {after_count} entries "
                f"({before_count - after_count} removed/merged)"
            )

            return {"before": before_count, "after": after_count}

    except Exception as e:
        print(f"[memory-audit] Memory audit failed: {e}")
        return {"error": str(e)}
