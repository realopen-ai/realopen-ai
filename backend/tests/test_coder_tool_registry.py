"""Characterize the existing coder protocol while tool implementations move."""

import hashlib
import inspect
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent.coder import tooling
from app.agent.coder.tools import (
    CODER_TOOLS,
    TOOL_MODULES,
    TOOL_REGISTRY,
    commands,
    dispatch_tool,
    start_preview,
    workspace,
)


TOOL_NAMES = (
    "scaffold_crud_app",
    "scaffold_project",
    "check_preview",
    "load_skill",
    "setup_python_project",
    "list_files",
    "read_file",
    "edit_file",
    "write_file",
    "search_files",
    "run_command",
    "run_tests",
    "install_dependencies",
    "run_checks",
    "run_browser_tests",
    "start_preview",
    "update_plan",
)


def test_model_facing_schemas_and_order_are_unchanged_by_refactor():
    assert tuple(TOOL_REGISTRY) == TOOL_NAMES
    assert len(TOOL_MODULES) == len(TOOL_REGISTRY) == len(CODER_TOOLS)
    serialized = json.dumps(CODER_TOOLS, ensure_ascii=False, separators=(",", ":"))
    # Pre-refactor schema snapshot: update deliberately for a protocol change,
    # not for moving Python handlers between files.
    assert hashlib.sha256(serialized.encode()).hexdigest() == (
        "53556ec6949126341c868ce2644ef5ba08a5929b8c1c5c0838ad978aadc9cbf2"
    )


def test_each_tool_owns_its_schema_and_async_handler():
    for module, schema, name in zip(TOOL_MODULES, CODER_TOOLS, TOOL_NAMES):
        assert module.__name__ == "app.agent.coder.tools." + name
        assert module.SCHEMA is schema
        assert schema["function"]["name"] == name
        assert inspect.iscoroutinefunction(module.execute)
        assert module.execute.__module__ == module.__name__


def test_compatibility_imports_remain_available():
    assert tooling.CODER_TOOLS is CODER_TOOLS
    assert tooling.dispatch_tool is dispatch_tool
    assert tooling.infer_preview is start_preview.infer_preview
    assert tooling.workspace_path is workspace.workspace_path
    assert tooling.normalize_tool_args is workspace.normalize_tool_args
    assert tooling.source_fingerprint is workspace.source_fingerprint


@pytest.mark.asyncio
@pytest.mark.parametrize("name", TOOL_NAMES)
async def test_registry_dispatches_arguments_progress_and_receipt_unchanged(
    monkeypatch, name
):
    sandbox = SimpleNamespace(id="sandbox")
    args = {"example": "input"}
    progress = [].append
    receipt = ("output", {"changed": False})
    execute = AsyncMock(return_value=receipt)
    monkeypatch.setattr(TOOL_REGISTRY[name], "execute", execute)
    assert await dispatch_tool(sandbox, name, args, progress) is receipt
    execute.assert_awaited_once_with(sandbox, args, progress)


@pytest.mark.asyncio
async def test_unknown_tool_keeps_existing_error_result():
    assert await dispatch_tool(None, "not_registered", {}) == (
        "Unknown coder tool: not_registered",
        None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name,refresh",
    [("run_command", False), ("run_tests", False), ("install_dependencies", True)],
)
async def test_command_tools_share_guards_without_losing_refresh_behavior(
    monkeypatch, name, refresh
):
    sandbox, args = object(), {"command": "check-project"}
    receipt = ("result", {"exit_code": 0})
    execute = AsyncMock(return_value=receipt)
    monkeypatch.setattr(commands, "execute", execute)
    assert await dispatch_tool(sandbox, name, args) is receipt
    execute.assert_awaited_once_with(sandbox, args, refresh_project=refresh)
