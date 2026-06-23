"""
manage_memory tool — lets the LLM explicitly save/forget/edit memories.

WHY A TOOL:
Previously memory was read-only injection — the LLM could see memories
in context but could not modify them. The ``ai_agent`` source value in
the Memory model was dead code. This tool activates it: when the user
says "remember that I prefer tabs over spaces" or "forget what I told
you about X", the LLM can call manage_memory to persist or delete the
fact immediately, without waiting for the background extractor.

ACTIONS:
- list:   list memories (optionally filtered by category)
- add:    add a new memory (source="ai_agent")
- edit:   edit an existing memory's text and/or category
- delete: delete a memory by id
- search: search memories by keyword

HARD STEERING (not implemented here — handled in agent/service.py):
When the user says "save this address for Alex", the agent service can
REMOVE manage_memory from the toolset to prevent the model defaulting
to memory when the user really wants a contact. For now we keep it
simple and rely on the tool description to steer.

VALIDATION:
- add: requires non-empty text; category defaults to "fact"; source is
  always "ai_agent" (the LLM cannot forge "user" source).
- edit/delete: require a memory_id. We use prefix matching so the LLM
  doesn't need to emit a full UUID — the first 8 chars suffice.
- No dedup runs on LLM-added memories (the audit will consolidate
  duplicates later).
"""

from __future__ import annotations

import logging
import time
from typing import List

from app.agent.base import BaseTool, ToolCall, ToolResult, ToolType, tool_registry
from app.db.session import async_session_factory
from app.services.memory import MemoryManager, VALID_CATEGORIES

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[manage_memory] {formatted}", flush=True)


def _memory_to_dict(m) -> dict:
    return {
        "id": str(m.id),
        "text": m.text,
        "category": m.category,
        "source": m.source,
        "pinned": m.pinned,
    }


class ManageMemoryTool(BaseTool):
    """Let the LLM manage the user's persistent memories."""

    name = "manage_memory"
    description = (
        "Manage the user's persistent memories: list, add, edit, delete, or search. "
        "Memories persist across conversations. Use 'add' when the user explicitly "
        "asks you to remember something (e.g. 'remember that I prefer tabs'). "
        "Use 'delete' when the user asks you to forget something. "
        "Use 'search' to check if a fact is already stored before adding it. "
        "For facts about the USER (name, preferences, contacts) — NOT for general knowledge."
    )
    tool_type = ToolType.FILE_WRITE  # writes to the memory store

    param_aliases = {
        "action": "action",
        "text": "text",
        "memory_id": "memory_id",
        "id": "memory_id",
        "category": "category",
        "query": "query",
        "search": "query",
    }

    def get_parameters(self) -> dict:
        return {
            "action": {
                "type": "string",
                "enum": ["list", "add", "edit", "delete", "search"],
                "description": "The action to perform",
            },
            "text": {
                "type": "string",
                "description": "Memory text (for add) or new text (for edit)",
            },
            "memory_id": {
                "type": "string",
                "description": "Memory ID (for edit/delete). Prefix match accepted.",
            },
            "category": {
                "type": "string",
                "enum": sorted(VALID_CATEGORIES),
                "description": "Memory category (for add/edit/list filter)",
            },
            "query": {
                "type": "string",
                "description": "Search query (for search action)",
            },
        }

    def get_required_params(self) -> List[str]:
        return ["action"]

    async def execute(self, *, action: str, **kwargs) -> ToolResult:
        """Execute the memory management action."""
        start = time.time()
        tool_call = ToolCall(
            id=f"tc-mem-{int(start * 1000)}",
            type=self.tool_type,
            name=self.name,
            status="running",
            title=f"Memory: {action}",
            started_at=start,
        )

        if not action or action not in ("list", "add", "edit", "delete", "search"):
            tool_call.status = "error"
            tool_call.completed_at = time.time()
            tool_call.error = f"Invalid action: {action}"
            return ToolResult(
                success=False,
                output=f"Invalid action: {action}. Use list/add/edit/delete/search.",
                tool_call=tool_call,
            )

        try:
            async with async_session_factory() as db:
                manager = MemoryManager()
                result_text = ""

                if action == "list":
                    category = kwargs.get("category")
                    memories = await manager.get_all_memories(
                        db, category=category, limit=50
                    )
                    if not memories:
                        result_text = "No memories stored yet."
                    else:
                        lines = [f"Stored memories ({len(memories)} shown):"]
                        for m in memories:
                            pin = " [pinned]" if m.pinned else ""
                            lines.append(
                                f"- [{m.category}] {m.text}{pin} (id: {str(m.id)[:8]})"
                            )
                        result_text = "\n".join(lines)

                elif action == "add":
                    text = (kwargs.get("text") or "").strip()
                    category = kwargs.get("category") or "fact"
                    if not text:
                        tool_call.status = "error"
                        tool_call.completed_at = time.time()
                        tool_call.error = "Memory text cannot be empty"
                        return ToolResult(
                            success=False,
                            output="Memory text cannot be empty. Provide a 'text' field.",
                            tool_call=tool_call,
                        )
                    # Check for duplicates first (lightweight — exact + fuzzy only,
                    # no vector call to keep this fast).
                    duplicates = await manager.find_duplicates(db, text)
                    if duplicates:
                        result_text = (
                            f"Memory already exists (duplicate of "
                            f"{str(duplicates[0].id)[:8]}): {duplicates[0].text}"
                        )
                    else:
                        memory = await manager.add_memory(
                            db,
                            text,
                            category=category,
                            source="ai_agent",
                        )
                        await db.commit()
                        result_text = (
                            f"Memory saved (id: {str(memory.id)[:8]}, "
                            f"category: {memory.category}"
                            f"{' [pinned]' if memory.pinned else ''}): {memory.text}"
                        )
                        _log("LLM added memory: %s", text[:80])

                elif action == "edit":
                    memory_id = (kwargs.get("memory_id") or "").strip()
                    new_text = (kwargs.get("text") or "").strip()
                    new_category = kwargs.get("category")
                    if not memory_id:
                        result_text = (
                            "Edit requires a memory_id (prefix match accepted)."
                        )
                    else:
                        # Find by prefix match
                        memory = await self._find_by_prefix(manager, db, memory_id)
                        if not memory:
                            result_text = (
                                f"No memory found with id starting '{memory_id}'."
                            )
                        else:
                            updates = {}
                            if new_text:
                                updates["text"] = new_text
                            if new_category and new_category in VALID_CATEGORIES:
                                updates["category"] = new_category
                            if not updates:
                                result_text = (
                                    "Edit requires at least one of: text, category."
                                )
                            else:
                                updated = await manager.update_memory(
                                    db, str(memory.id), **updates
                                )
                                if updated:
                                    await db.commit()
                                    result_text = (
                                        f"Memory updated (id: {str(updated.id)[:8]}): "
                                        f"{updated.text} [{updated.category}]"
                                    )
                                    _log("LLM edited memory %s", str(updated.id)[:8])
                                else:
                                    result_text = "Memory update failed."

                elif action == "delete":
                    memory_id = (kwargs.get("memory_id") or "").strip()
                    if not memory_id:
                        result_text = (
                            "Delete requires a memory_id (prefix match accepted)."
                        )
                    else:
                        memory = await self._find_by_prefix(manager, db, memory_id)
                        if not memory:
                            result_text = (
                                f"No memory found with id starting '{memory_id}'."
                            )
                        else:
                            text_snapshot = memory.text
                            deleted = await manager.delete_memory(db, str(memory.id))
                            if deleted:
                                await db.commit()
                                result_text = (
                                    f"Memory deleted: '{text_snapshot}' "
                                    f"(was id {str(memory.id)[:8]})"
                                )
                                _log("LLM deleted memory %s", str(memory.id)[:8])
                            else:
                                result_text = "Memory delete failed."

                elif action == "search":
                    query = (kwargs.get("query") or "").strip()
                    if not query:
                        result_text = "Search requires a query."
                    else:
                        memories = await manager.search_memories(db, query, limit=10)
                        if not memories:
                            result_text = f"No memories matching '{query}'."
                        else:
                            lines = [f"Memories matching '{query}' ({len(memories)}):"]
                            for m in memories:
                                pin = " [pinned]" if m.pinned else ""
                                lines.append(
                                    f"- [{m.category}] {m.text}{pin} (id: {str(m.id)[:8]})"
                                )
                            result_text = "\n".join(lines)

                tool_call.status = "completed"
                tool_call.completed_at = time.time()
                tool_call.output = result_text
                return ToolResult(
                    success=True,
                    output=result_text,
                    tool_call=tool_call,
                )

        except Exception as e:
            logger.exception("manage_memory failed: %s", e)
            print(f"[manage_memory] ERROR: {e}", flush=True)
            tool_call.status = "error"
            tool_call.completed_at = time.time()
            tool_call.error = str(e)
            return ToolResult(
                success=False,
                output=f"Memory operation failed: {e}",
                tool_call=tool_call,
            )

    async def _find_by_prefix(self, manager: MemoryManager, db, prefix: str):
        """Find a memory by ID prefix (first 8+ chars). Returns the Memory or None.

        This lets the LLM use a short ID (8 chars) instead of a full 36-char UUID.
        If multiple memories share the prefix, returns the first (oldest).
        """
        # Normalize the prefix — strip dashes, lowercase
        prefix_clean = prefix.lower().replace("-", "")
        if len(prefix_clean) < 6:
            return None  # too short to be safe

        # Get all memories and find prefix match. For <10k memories this is fine.
        all_mems = await manager.get_all_memories(db, limit=10000)
        for m in all_mems:
            if str(m.id).lower().replace("-", "").startswith(prefix_clean):
                return m
        return None


# Register the tool
tool_registry.register(ManageMemoryTool())
logger.info("[manage_memory] ManageMemoryTool registered")
