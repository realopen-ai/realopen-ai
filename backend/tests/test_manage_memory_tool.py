"""Tests for app/agent/tools/manage_memory.py (ManageMemoryTool).

What is tested:
  - Tool metadata: schema, parameter declaration, required params,
    parameter aliases, registry registration.
  - execute() for every action with the MemoryManager layer mocked:
      * invalid action → error ToolResult
      * list (empty / with memories / pinned marker)
      * add (empty text → error; duplicate detected; successful save with
        source="ai_agent"; exception path)
      * edit (missing id; unknown id; nothing to update; success)
      * delete (missing id; unknown id; success)
      * search (missing query; no hits; hits)
  - _find_by_prefix: too-short prefix, dash-insensitive matching,
    no match, first match wins.

What is mocked:
  - app.agent.tools.manage_memory.async_session_factory is patched to a
    fake context manager (no real Postgres session is opened).
  - app.agent.tools.manage_memory.MemoryManager is patched with a
    MagicMock whose async methods are AsyncMocks — the tool never reaches
    the memory service, embeddings, or Ollama.
"""

import uuid
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest

import app.agent.tools.manage_memory as mm
from app.agent.base import tool_registry
from app.agent.tools.manage_memory import ManageMemoryTool, _memory_to_dict
from app.services.memory import VALID_CATEGORIES


# ══════════════════════════════════════════════════════════════════════
# Test doubles
# ══════════════════════════════════════════════════════════════════════


class _FakeSessionCtx:
    def __init__(self):
        self.sessions = []

    async def __aenter__(self):
        session = MagicMock()
        session.commit = AsyncMock()
        session.flush = AsyncMock()
        self.sessions.append(session)
        return session

    async def __aexit__(self, *exc):
        return False


def make_mem(**kwargs):
    defaults = dict(
        id=uuid.uuid4(),
        text="User likes pizza",
        category="preference",
        source="ai_agent",
        pinned=False,
    )
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


@pytest.fixture
def tool():
    return ManageMemoryTool()


@pytest.fixture
def manager_mock():
    """Patch MemoryManager used by the tool with a fully-async MagicMock."""
    mgr = MagicMock()
    mgr.get_all_memories = AsyncMock(return_value=[])
    mgr.add_memory = AsyncMock()
    mgr.update_memory = AsyncMock()
    mgr.delete_memory = AsyncMock(return_value=False)
    mgr.pin_memory = AsyncMock()
    mgr.search_memories = AsyncMock(return_value=[])
    mgr.find_duplicates = AsyncMock(return_value=[])
    ctx = _FakeSessionCtx()
    with (
        patch.object(mm, "async_session_factory", lambda: ctx),
        patch.object(mm, "MemoryManager", MagicMock(return_value=mgr)),
    ):
        yield mgr

# ══════════════════════════════════════════════════════════════════════
# 1. Metadata / schema
# ══════════════════════════════════════════════════════════════════════


def test_tool_metadata(tool):
    assert tool.name == "manage_memory"
    assert tool.display_name == "Memory Management"
    schema = tool.get_schema()
    assert schema["name"] == "manage_memory"
    assert "memories" in tool.description.lower()


def test_tool_parameters(tool):
    params = tool.get_parameters()
    assert set(params) == {"action", "text", "memory_id", "category", "query"}
    assert params["action"]["enum"] == ["list", "add", "edit", "delete", "search"]
    assert params["category"]["enum"] == sorted(VALID_CATEGORIES)
    assert tool.get_required_params() == ["action"]


def test_tool_param_aliases(tool):
    assert tool.param_aliases["id"] == "memory_id"
    assert tool.param_aliases["search"] == "query"


def test_tool_registered_in_registry():
    assert tool_registry.has_tool("manage_memory")
    assert tool_registry.get("manage_memory").name == "manage_memory"


def test_memory_to_dict_shape():
    m = make_mem()
    d = _memory_to_dict(m)
    assert d == {
        "id": str(m.id),
        "text": m.text,
        "category": m.category,
        "source": m.source,
        "pinned": m.pinned,
    }


def test_log_handles_bad_format_string(capsys):
    # a bad %-format tuple must not raise — falls back to raw concatenation
    mm._log("bad %d", "oops")
    assert "bad" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════
# 2. execute() — validation errors (no DB touched)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_invalid_action(tool, manager_mock):
    result = await tool.execute(action="explode")
    assert result.success is False
    assert "Invalid action: explode" in result.output
    assert result.tool_call.status == "error"
    assert result.tool_call.error is not None


@pytest.mark.asyncio
async def test_execute_add_requires_text(tool, manager_mock):
    result = await tool.execute(action="add", text="   ")
    assert result.success is False
    assert "cannot be empty" in result.output
    manager_mock.add_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_edit_requires_memory_id(tool, manager_mock):
    result = await tool.execute(action="edit", text="new text")
    assert result.success is True  # graceful message, not an error
    assert "Edit requires a memory_id" in result.output


@pytest.mark.asyncio
async def test_execute_delete_requires_memory_id(tool, manager_mock):
    result = await tool.execute(action="delete")
    assert result.success is True
    assert "Delete requires a memory_id" in result.output


@pytest.mark.asyncio
async def test_execute_search_requires_query(tool, manager_mock):
    result = await tool.execute(action="search", query="  ")
    assert result.success is True
    assert "Search requires a query." in result.output


# ══════════════════════════════════════════════════════════════════════
# 3. execute() — list
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_list_empty(tool, manager_mock):
    result = await tool.execute(action="list")
    assert result.success is True
    assert result.output == "No memories stored yet."
    manager_mock.get_all_memories.assert_awaited_once()
    kwargs = manager_mock.get_all_memories.call_args.kwargs
    assert kwargs["category"] is None  # no category filter
    assert kwargs["limit"] == 50


@pytest.mark.asyncio
async def test_execute_list_with_category_filter(tool, manager_mock):
    manager_mock.get_all_memories = AsyncMock(return_value=[])
    await tool.execute(action="list", category="identity")
    assert (
        manager_mock.get_all_memories.call_args.kwargs.get("category") == "identity"
        or manager_mock.get_all_memories.call_args.args[1] == "identity"
    )


@pytest.mark.asyncio
async def test_execute_list_formats_memories(tool, manager_mock):
    mem = make_mem(text="User's name is Sam", category="identity", pinned=True)
    manager_mock.get_all_memories = AsyncMock(return_value=[mem])
    result = await tool.execute(action="list")
    assert result.success is True
    assert "Stored memories (1 shown):" in result.output
    assert "- [identity] User's name is Sam [pinned] (id: " in result.output
    assert str(mem.id)[:8] in result.output
    assert result.tool_call.status == "completed"


# ══════════════════════════════════════════════════════════════════════
# 4. execute() — add
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_add_success(tool, manager_mock):
    created = make_mem(text="User prefers tabs", category="preference", pinned=False)
    manager_mock.add_memory = AsyncMock(return_value=created)
    result = await tool.execute(action="add", text="User prefers tabs")
    assert result.success is True
    assert "Memory saved" in result.output
    assert "User prefers tabs" in result.output
    manager_mock.add_memory.assert_awaited_once()
    call = manager_mock.add_memory.call_args
    # source is always ai_agent — the LLM cannot forge "user"
    assert call.kwargs["source"] == "ai_agent"
    assert call.kwargs["category"] == "fact"  # default category


@pytest.mark.asyncio
async def test_execute_add_duplicate_detected(tool, manager_mock):
    dup = make_mem(text="User prefers tabs")
    manager_mock.find_duplicates = AsyncMock(return_value=[dup])
    result = await tool.execute(action="add", text="User prefers tabs")
    assert result.success is True
    assert "Memory already exists" in result.output
    assert str(dup.id)[:8] in result.output
    manager_mock.add_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_add_failure_returns_error(tool, manager_mock):
    manager_mock.add_memory = AsyncMock(side_effect=RuntimeError("db down"))
    result = await tool.execute(action="add", text="Some fact")
    assert result.success is False
    assert "Memory operation failed" in result.output
    assert result.tool_call.status == "error"


# ══════════════════════════════════════════════════════════════════════
# 5. execute() — edit
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_edit_unknown_id(tool, manager_mock):
    manager_mock.get_all_memories = AsyncMock(return_value=[])
    result = await tool.execute(action="edit", memory_id=str(uuid.uuid4()), text="x")
    assert result.success is True
    assert "No memory found with id starting" in result.output


@pytest.mark.asyncio
async def test_execute_edit_nothing_to_update(tool, manager_mock):
    mem = make_mem()
    manager_mock.get_all_memories = AsyncMock(return_value=[mem])
    result = await tool.execute(action="edit", memory_id=str(mem.id))
    assert "Edit requires at least one of: text, category." in result.output
    manager_mock.update_memory.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_edit_success(tool, manager_mock):
    mem = make_mem()
    updated = make_mem(id=mem.id, text="new text", category="goal")
    manager_mock.get_all_memories = AsyncMock(return_value=[mem])
    manager_mock.update_memory = AsyncMock(return_value=updated)
    result = await tool.execute(
        action="edit", memory_id=str(mem.id), text="new text", category="goal"
    )
    assert result.success is True
    assert "Memory updated" in result.output
    manager_mock.update_memory.assert_awaited_once_with(
        ANY, str(mem.id), text="new text", category="goal"
    )


@pytest.mark.asyncio
async def test_execute_edit_update_failed(tool, manager_mock):
    # prefix found + updates given, but the manager returns None → the
    # tool reports a graceful failure message
    mem = make_mem()
    manager_mock.get_all_memories = AsyncMock(return_value=[mem])
    manager_mock.update_memory = AsyncMock(return_value=None)
    result = await tool.execute(action="edit", memory_id=str(mem.id), text="new")
    assert result.success is True
    assert result.output == "Memory update failed."


# ══════════════════════════════════════════════════════════════════════
# 6. execute() — delete
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_delete_unknown_id(tool, manager_mock):
    manager_mock.get_all_memories = AsyncMock(return_value=[])
    result = await tool.execute(action="delete", memory_id=str(uuid.uuid4()))
    assert "No memory found with id starting" in result.output


@pytest.mark.asyncio
async def test_execute_delete_success(tool, manager_mock):
    mem = make_mem(text="User hates spam")
    manager_mock.get_all_memories = AsyncMock(return_value=[mem])
    manager_mock.delete_memory = AsyncMock(return_value=True)
    result = await tool.execute(action="delete", memory_id=str(mem.id))
    assert result.success is True
    assert "Memory deleted" in result.output
    assert "User hates spam" in result.output
    manager_mock.delete_memory.assert_awaited_once_with(ANY, str(mem.id))


@pytest.mark.asyncio
async def test_execute_delete_failed(tool, manager_mock):
    # prefix found, but the manager reports the row could not be deleted
    mem = make_mem()
    manager_mock.get_all_memories = AsyncMock(return_value=[mem])
    manager_mock.delete_memory = AsyncMock(return_value=False)
    result = await tool.execute(action="delete", memory_id=str(mem.id))
    assert result.success is True
    assert result.output == "Memory delete failed."


# ══════════════════════════════════════════════════════════════════════
# 7. execute() — search
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_search_no_hits(tool, manager_mock):
    result = await tool.execute(action="search", query="pizza")
    assert result.success is True
    assert result.output == "No memories matching 'pizza'."
    manager_mock.search_memories.assert_awaited_once()
    assert manager_mock.search_memories.call_args.kwargs.get("limit") == 10


@pytest.mark.asyncio
async def test_execute_search_hits(tool, manager_mock):
    mem = make_mem(text="User likes pizza", category="preference", pinned=True)
    manager_mock.search_memories = AsyncMock(return_value=[mem])
    result = await tool.execute(action="search", query="pizza")
    assert "Memories matching 'pizza' (1):" in result.output
    assert "- [preference] User likes pizza [pinned] (id: " in result.output


# ══════════════════════════════════════════════════════════════════════
# 8. _find_by_prefix
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_find_by_prefix_too_short_returns_none(tool):
    manager = MagicMock()
    manager.get_all_memories = AsyncMock(return_value=[])
    assert await tool._find_by_prefix(manager, MagicMock(), "abc") is None
    manager.get_all_memories.assert_not_awaited()


@pytest.mark.asyncio
async def test_find_by_prefix_matches_full_id(tool):
    mem = make_mem()
    manager = MagicMock()
    manager.get_all_memories = AsyncMock(return_value=[mem])
    assert await tool._find_by_prefix(manager, MagicMock(), str(mem.id)) is mem


@pytest.mark.asyncio
async def test_find_by_prefix_matches_8_char_prefix_with_dashes(tool):
    mem = make_mem()
    prefix = str(mem.id)[:8]
    manager = MagicMock()
    manager.get_all_memories = AsyncMock(return_value=[mem])
    assert await tool._find_by_prefix(manager, MagicMock(), prefix) is mem


@pytest.mark.asyncio
async def test_find_by_prefix_dash_insensitive(tool):
    mem = make_mem()
    id_str = str(mem.id)
    # A prefix containing the UUID's own dash still matches after
    # normalization (dashes are stripped on both sides).
    prefix = id_str[:13]  # "xxxxxxxx-xxxx" — contains a dash
    assert "-" in prefix
    manager = MagicMock()
    manager.get_all_memories = AsyncMock(return_value=[mem])
    assert await tool._find_by_prefix(manager, MagicMock(), prefix) is mem


@pytest.mark.asyncio
async def test_find_by_prefix_no_match(tool):
    other = make_mem()
    manager = MagicMock()
    manager.get_all_memories = AsyncMock(return_value=[other])
    target = make_mem()
    prefix = str(target.id)[:8]
    assert await tool._find_by_prefix(manager, MagicMock(), prefix) is None


@pytest.mark.asyncio
async def test_find_by_prefix_returns_first_match(tool):
    mem_a = make_mem()
    mem_b = make_mem()
    # Both share the same 8-char prefix in this contrived setup
    mem_b.id = mem_a.id
    manager = MagicMock()
    manager.get_all_memories = AsyncMock(return_value=[mem_a, mem_b])
    assert await tool._find_by_prefix(manager, MagicMock(), str(mem_a.id)[:8]) is mem_a
