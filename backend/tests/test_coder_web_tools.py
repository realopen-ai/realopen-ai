"""Focused coder web integration; no model inference or application build."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
import uuid

import pytest

from app.agent.base import ToolCall, ToolRegistry, ToolResult, ToolType
from app.agent.coder import agent, web_tools
from app.agent.coder.context import tokens
from app.agent.coder.working_state import RunState
from app.agent.tools import config_store
from app.agent.tools.discovery import DiscoverToolsTool, ReadToolResultTool
from app.agent.tools.web_fetch import WebFetchTool
from app.agent.tools.web_search import WebSearchTool


@pytest.fixture
def web(monkeypatch):
    registry = ToolRegistry()
    for tool in [
        WebSearchTool(),
        WebFetchTool(),
        DiscoverToolsTool(),
        ReadToolResultTool(),
    ]:
        registry.register(tool)
    configs = {
        tool.name: {"enabled": True, "always_load": False, "tags": []}
        for tool in registry.all_tools()
    }
    monkeypatch.setattr(web_tools, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(config_store, "get_tool_config", lambda name: configs.get(name))
    return registry, configs


def names(session):
    return {schema["function"]["name"] for schema in session.schemas}


def test_keyword_always_load_explicit_url_and_disabled_preferences(web):
    _, configs = web
    configs["use_websearch"]["tags"] = ["sapphire"]
    session = web_tools.CoderWebTools("Build a local backend")
    assert names(session) == {"discover_tools"}
    assert "use_websearch" in names(web_tools.CoderWebTools("sapphire documentation"))
    configs["use_webfetch"]["always_load"] = True
    assert "use_webfetch" in names(web_tools.CoderWebTools("Build a backend"))
    configs["use_webfetch"]["always_load"] = False
    assert "use_webfetch" in names(
        web_tools.CoderWebTools("Read https://example.org/docs")
    )
    configs["use_webfetch"]["enabled"] = False
    configs["use_websearch"]["enabled"] = False
    session = web_tools.CoderWebTools(
        "use_webfetch https://example.org; use_websearch sapphire"
    )
    assert not session.schemas
    assert json.loads(session.catalog())["optional_web_tools"] == []


@pytest.mark.asyncio
async def test_discovery_only_loads_enabled_web_schemas_and_never_executes(
    web, monkeypatch
):
    registry, configs = web
    search = AsyncMock(return_value=ToolResult(True, "Search results"))
    monkeypatch.setattr(registry.get("use_websearch"), "execute", search)
    session = web_tools.CoderWebTools("Build a backend")
    _, unloaded = await session.execute("use_websearch", {"query": "docs"}, 1024)
    assert unloaded["exit_code"] == 1
    search.assert_not_awaited()
    raw, _ = await session.execute("discover_tools", {"names": ["use_websearch"]}, 1024)
    assert json.loads(raw)["loaded"] == ["use_websearch"]
    assert json.loads(raw)["action_executed"] is False
    search.assert_not_awaited()
    assert "use_websearch" in names(session)
    await session.execute("use_websearch", {"query": "official docs"}, 1024)
    search.assert_awaited_once_with(query="official docs")
    configs["use_webfetch"]["enabled"] = False
    _, event = await session.execute(
        "discover_tools", {"names": ["use_webfetch"]}, 1024
    )
    assert event["exit_code"] == 1
    _, event = await session.execute("discover_tools", {"names": ["run_command"]}, 1024)
    assert event["exit_code"] == 1


@pytest.mark.asyncio
async def test_disabled_after_selection_is_rechecked_without_network(web, monkeypatch):
    registry, configs = web
    execute = AsyncMock()
    monkeypatch.setattr(registry.get("use_webfetch"), "execute", execute)
    session = web_tools.CoderWebTools("Read https://example.org")
    configs["use_webfetch"]["enabled"] = False
    assert "use_webfetch" not in names(session)
    raw, event = await session.execute(
        "use_webfetch", {"url": "https://example.org"}, 1024
    )
    assert event["exit_code"] == 1 and "disabled" in raw
    execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_full_result_paging_is_request_local_bounded_and_compaction_safe(
    web, monkeypatch
):
    registry, _ = web
    full = (
        "# Guide\n\nSource: https://example.org\nExtraction: text\n\n"
        + "Begin. " * 3000
        + "MIDDLE_REFERENCE"
        + " End." * 3000
    )
    monkeypatch.setattr(
        registry.get("use_webfetch"),
        "execute",
        AsyncMock(return_value=ToolResult(True, full, model_output_limit=2000)),
    )
    session = web_tools.CoderWebTools("Read https://example.org")
    raw, event = await session.execute(
        "use_webfetch", {"url": "https://example.org"}, 300
    )
    assert raw == full and tokens(event["model_output"]) <= 300
    ref = event["web_source"]["result_ref"]
    assert ref in event["model_output"]
    assert "read_tool_result" in names(session)
    state = RunState()
    state.record("use_webfetch", {"url": "https://example.org"}, raw, event)
    assert ref in state.context()["content"]
    page, _ = await session.execute(
        "read_tool_result",
        {"result_ref": ref, "offset": full.index("MIDDLE_REFERENCE"), "limit": 6000},
        300,
    )
    parsed = json.loads(page)
    assert parsed["content"].startswith("MIDDLE_REFERENCE")
    assert tokens(page) <= 300
    assert parsed["next_offset"] == parsed["offset"] + len(parsed["content"])
    other = web_tools.CoderWebTools("Read https://example.org")
    _, failed = await other.execute("read_tool_result", {"result_ref": ref}, 1024)
    assert failed["exit_code"] == 1


@pytest.mark.asyncio
async def test_disabled_paging_produces_honest_bounded_excerpt(web, monkeypatch):
    registry, configs = web
    configs["read_tool_result"]["enabled"] = False
    monkeypatch.setattr(
        registry.get("use_webfetch"),
        "execute",
        AsyncMock(return_value=ToolResult(True, "Evidence " * 5000)),
    )
    session = web_tools.CoderWebTools("Read https://example.org")
    _, event = await session.execute(
        "use_webfetch", {"url": "https://example.org"}, 400
    )
    view = json.loads(event["model_output"])
    assert tokens(event["model_output"]) <= 400
    assert "Paging is disabled" in view["message"]
    assert "result_ref" not in view and "read_tool_result" not in names(session)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, args, kind",
    [
        ("use_websearch", {"query": "official docs"}, "websearch"),
        ("use_webfetch", {"url": "https://example.org"}, "webfetch"),
    ],
)
async def test_step_reuses_ui_types_full_outputs_and_coder_trace(
    web, monkeypatch, name, args, kind
):
    registry, configs = web
    configs[name]["always_load"] = True
    raw = "Source evidence " * 2500
    call = ToolCall(
        "shared",
        ToolType.WEB_SEARCH,
        name,
        web_results=(
            [{"title": "Docs", "url": "https://example.org", "snippet": "Evidence"}]
            if name == "use_websearch"
            else None
        ),
    )
    monkeypatch.setattr(
        registry.get(name),
        "execute",
        AsyncMock(return_value=ToolResult(True, raw, call)),
    )
    observations = []
    monkeypatch.setattr(agent, "observe", lambda *a, **kw: observations.append((a, kw)))
    persist = AsyncMock()
    monkeypatch.setattr(agent, "_record_command", persist)
    queue = asyncio.Queue()
    session = web_tools.CoderWebTools("Build a backend")
    output, event = await agent._run_step(
        sandbox=SimpleNamespace(id=uuid.uuid4()),
        conversation=SimpleNamespace(id=uuid.uuid4()),
        task_id=uuid.uuid4(),
        name=name,
        args=args,
        state=RunState(),
        parent_id="parent",
        event_queue=queue,
        web_tools=session,
    )
    started, completed = queue.get_nowait(), queue.get_nowait()
    assert started["type"] == completed["type"] == kind
    assert started["agent"] == "coder" and started["parentId"] == "parent"
    assert "filePath" not in started
    assert completed["output"] == output == raw
    if name == "use_websearch":
        assert completed["webResults"] == call.web_results
    assert any(a[0] == "end_tool" and kw.get("output") == raw for a, kw in observations)
    persist.assert_not_awaited()


@pytest.mark.asyncio
async def test_offline_tool_failure_is_reported_without_aborting_coder_step(
    web, monkeypatch
):
    registry, configs = web
    configs["use_websearch"]["always_load"] = True
    monkeypatch.setattr(
        registry.get("use_websearch"),
        "execute",
        AsyncMock(return_value=ToolResult(False, "Search is offline")),
    )
    session = web_tools.CoderWebTools("Build a backend")
    raw, event = await session.execute("use_websearch", {"query": "docs"}, 1024)
    assert event["exit_code"] == 1 and raw == "Search is offline"
    assert "web_source" not in event


@pytest.mark.asyncio
async def test_recent_small_results_remain_pageable_and_cache_is_bounded(
    web, monkeypatch
):
    registry, _ = web
    monkeypatch.setattr(
        registry.get("use_webfetch"),
        "execute",
        AsyncMock(return_value=ToolResult(True, "Small source.")),
    )
    session = web_tools.CoderWebTools("Read https://example.org")
    refs = []
    for i in range(7):
        _, event = await session.execute(
            "use_webfetch", {"url": f"https://example.org/{i}"}, 1024
        )
        refs.append(event["web_source"]["result_ref"])
    assert len(session.results) == len(session.metadata) == 6
    assert "read_tool_result" in names(session)
    assert refs[0] not in session.results
    raw, _ = await session.execute("read_tool_result", {"result_ref": refs[-1]}, 1024)
    assert json.loads(raw)["content"] == "Small source."


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_web_schemas_reach_native_and_text_protocol_without_inference(
    web, monkeypatch, native
):
    from app.agent.coder import provider

    monkeypatch.setattr(
        provider.tool_capabilities, "should_try_native", lambda _: native
    )
    session = web_tools.CoderWebTools(
        "use_websearch and use_webfetch https://example.org"
    )
    selected = session.schemas
    chat = AsyncMock(return_value={"message": {"content": '{"final":"Done"}'}})
    monkeypatch.setattr(provider.providers, "chat_once", chat)
    await provider.chat_with_tool_recovery(
        "coder",
        [{"role": "user", "content": "Read docs"}],
        tools=selected,
        compact_context=AsyncMock(
            side_effect=lambda messages, model, **kw: (messages, False)
        ),
    )
    if native:
        assert chat.call_args.kwargs["tools"] == selected
    else:
        protocol = chat.call_args.args[1][-1]["content"]
        assert (
            '"name":"use_websearch"' in protocol and '"name":"use_webfetch"' in protocol
        )
