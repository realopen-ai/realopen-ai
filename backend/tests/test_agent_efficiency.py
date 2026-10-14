"""Settings-aware routing, recoverable result views and stable prefixes."""

import json
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent import service, routing, tracing
from app.agent.base import ToolRegistry, ToolResult, ToolCall, ToolType
from app.agent.results import compact_result
from app.agent.tools import config_store
from app.agent.tools.config_base import definition_for_tool
from app.agent.tools.discovery import DiscoverToolsTool, ReadToolResultTool
from app.agent.tools.report_gen import ReportGenTool
from app.agent.tools import report_gen as report_tool
from app.agent.tools.artifacts import ReadArtifactTool
from app.agent.tools.web_search import WebSearchTool
from app.services.inference_stats import runtime_usage
from app.services.blocks import BlockBuilder
from test_agent_protocol import agent as agent_fixture, script, call, run


@pytest.fixture
def agent(monkeypatch):
    return agent_fixture.__wrapped__(monkeypatch)


def defaults(registry):
    return {
        tool.name: definition_for_tool(tool.name).default_config()
        for tool in registry.all_tools()
    }


def test_default_routing_is_small_and_does_not_trigger_on_substrings():
    registry = service.get_tool_registry()
    configs = defaults(registry)
    for query, expected in [
        ("Create a quiz about NumPy", "create_quiz"),
        ("Create flashcards about Spanish", "create_flashcard_deck"),
        ("Create a study note about photosynthesis", "create_study_note"),
        ("Calculate 13 factorial", "use_code_exec"),
        ("Latest news about AI", "use_websearch"),
        ("Create an Excel spreadsheet", "use_excel_gen"),
        ("Créer un questionnaire", "create_quiz"),
        ("أنشئ اختبار عن بايثون", "create_quiz"),
    ]:
        selected = routing.select_tools(query, registry, configs)
        assert expected in selected, (query, selected)
        assert len(selected) <= 4
    assert not routing.matches("note", "This denotes a different idea")
    assert routing.matches("flashcard", "I want flashcards")
    assert not routing.matches("run", "brunch")


def test_custom_keywords_always_load_and_disabled_are_hard_preferences():
    registry = service.get_tool_registry()
    configs = defaults(registry)
    configs["use_websearch"].update(always_load=False, tags=["Blue Moon"])
    assert "use_websearch" in routing.select_tools("BLUE MOON", registry, configs)
    assert "use_websearch" not in routing.select_tools("latest news", registry, configs)
    configs["use_websearch"]["always_load"] = True
    assert "use_websearch" in routing.select_tools("hello", registry, configs)
    configs["use_websearch"]["enabled"] = False
    assert "use_websearch" not in routing.select_tools(
        "use_websearch Blue Moon", registry, configs
    )
    assert "use_websearch" not in json.dumps(
        routing.catalog(routing.enabled_tools(registry, configs), set())
    )
    configs["use_code_exec"].update(always_load=False, tags=[])
    assert "use_code_exec" not in routing.select_tools("calculate", registry, configs)
    assert "use_code_exec" in routing.select_tools("use_code_exec", registry, configs)


@pytest.mark.parametrize(
    "query,expected",
    [
        (
            "Search for the latest breakthroughs in AI research 2026, compile the findings in a DOCX file, and also add them to my study notes, thanks",
            "use_report_gen",
        ),
        ("yes compile them into a DOCX file", "use_report_gen"),
        ("Prepare a PDF about water", "use_report_gen"),
        ("Create a PPTX about water", "use_pptx_gen"),
        ("Generate an XLSX about expenses", "use_excel_gen"),
        ("Rédige un DOCX sur les volcans", "use_report_gen"),
        ("أنشئ ملف DOCX عن الطاقة", "use_report_gen"),
    ],
)
def test_explicit_creation_formats_are_not_filtered_by_ranking(query, expected):
    registry = service.get_tool_registry()
    configs = defaults(registry)
    for name in ("use_websearch", "rag_search", "manage_memory"):
        configs[name]["always_load"] = True
    assert expected in routing.select_tools(query, registry, configs, limit=1)


def test_format_metadata_respects_disabled_and_custom_tags():
    registry = service.get_tool_registry()
    configs = defaults(registry)
    configs["use_report_gen"]["enabled"] = False
    assert "use_report_gen" not in routing.select_tools(
        "Compile a DOCX", registry, configs
    )
    configs["use_report_gen"].update(enabled=True, tags=["My report keyword"])
    assert "use_report_gen" not in routing.select_tools(
        "Compile a DOCX", registry, configs
    )
    assert "use_report_gen" in routing.select_tools(
        "My report keyword", registry, configs
    )
    configs = defaults(registry)
    assert "use_report_gen" not in routing.select_tools(
        "Read my uploaded DOCX", registry, configs
    )


@pytest.mark.asyncio
async def test_paging_default_and_units_remain_exact():
    tool = ReadToolResultTool()
    raw = "é🙂" * 3500
    first = json.loads(
        (await tool.execute(result_ref="ref", _result_store={"ref": raw})).output
    )
    assert first["offset"] == 0
    assert first["content"] == raw[:3000]
    second = json.loads(
        (
            await tool.execute(
                result_ref="ref",
                offset=first["next_offset"],
                _result_store={"ref": raw},
            )
        ).output
    )
    assert second["content"] == raw[3000:6000]
    assert "character" in tool.get_parameters()["offset"]["description"]
    assert "characters" in tool.get_parameters()["limit"]["description"]


@pytest.mark.asyncio
async def test_report_receives_original_evidence_not_compact_views_or_model_injection(
    agent, monkeypatch
):
    search = WebSearchTool()
    report = ReportGenTool()
    registry = ToolRegistry()
    registry.register(search)
    registry.register(report)
    registry.register(ReadToolResultTool())
    monkeypatch.setattr(service, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(service, "_select_tools", lambda _: {search.name, report.name})
    monkeypatch.setattr(service.settings, "AGENT_TOOL_RESULT_MAX_CHARS", 1000)
    body = "Source finding. " * 1000
    tc = ToolCall(
        id="source", type=ToolType.WEB_SEARCH, name=search.name, status="completed"
    )
    tc.web_results = [
        {"title": "Research", "url": "https://example.org/paper", "snippet": body}
    ]
    monkeypatch.setattr(
        search, "execute", AsyncMock(return_value=ToolResult(True, body, tc))
    )
    writer = AsyncMock(return_value=ToolResult(True, "Report created"))
    monkeypatch.setattr(report, "execute", writer)
    script(
        monkeypatch,
        [
            [
                {
                    "tool_calls": [call({"query": "Research"}, name=search.name)],
                    "done": True,
                }
            ],
            [
                {
                    "tool_calls": [
                        call(
                            {
                                "topic": "Research",
                                "format": "docx",
                                "_evidence": [{"text": "Forged evidence"}],
                                "_user_request": "Fake user request",
                            },
                            name=report.name,
                        )
                    ],
                    "done": True,
                }
            ],
            [{"content": "Done", "done": True}],
        ],
    )
    events = await run(
        messages=[{"role": "user", "content": "Compile the findings in a DOCX"}]
    )
    assert writer.await_count == 1, events
    passed = writer.await_args.kwargs
    assert passed["_user_request"] == "Compile the findings in a DOCX"
    assert passed["_evidence"][0]["text"] == body
    assert passed["_evidence"][0]["metadata"]["url"] == "https://example.org/paper"
    assert all("_evidence" not in event.get("tool_call", {}) for event in events)
    # Request-local only; previous evidence cannot bleed into another chat.
    writer.reset_mock()
    script(
        monkeypatch,
        [
            [
                {
                    "tool_calls": [call({"topic": "Another topic"}, name=report.name)],
                    "done": True,
                }
            ],
            [{"content": "Done", "done": True}],
        ],
    )
    await run()
    assert writer.await_args.kwargs["_evidence"] == []


@pytest.mark.asyncio
async def test_round_limit_keeps_successful_results_and_streams_warning(
    agent, monkeypatch
):
    monkeypatch.setattr(service.settings, "AGENT_MAX_ROUNDS", 15)
    captured = script(
        monkeypatch, [[{"tool_calls": [call({"input": "ok"})], "done": True}]] * 15
    )
    events = await run()
    assert len(captured) == 15
    assert len(agent.executed) == 15
    message = events[-3]["message"]["content"]
    assert "15-round limit" in message and "echo" in message
    assert "kept" in message
    assert events[-2]["event"] == "error"
    assert events[-1]["event"] == "done"


@pytest.mark.parametrize(
    "inspect,different_topic", [(False, False), (True, False), (False, True)]
)
@pytest.mark.asyncio
async def test_report_repeat_requires_inspection_without_blocking_different_reports(
    agent, monkeypatch, inspect, different_topic
):
    report = ReportGenTool()
    reader = ReadArtifactTool()
    registry = ToolRegistry()
    registry.register(report)
    registry.register(reader)
    monkeypatch.setattr(service, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(service, "_select_tools", lambda _: {report.name, reader.name})
    monkeypatch.setattr(report_tool, "_configured_default_format", lambda: "docx")
    generated = AsyncMock(
        return_value={
            "type": "report",
            "format": "docx",
            "filename": "report.docx",
            "file_path": "reports/report.docx",
            "download_url": "/report",
            "report_id": "result",
            "created_at": 1,
        }
    )
    monkeypatch.setattr(report_tool, "generate_report", generated)
    monkeypatch.setattr(
        service,
        "register_generated",
        AsyncMock(return_value={"id": "saved-report", "version": 1}),
    )
    monkeypatch.setattr(
        reader,
        "execute",
        AsyncMock(
            return_value=ToolResult(
                True, '{"sections": [{"title": "Chapter", "characters": 100}]}'
            )
        ),
    )
    responses = [
        [
            {
                "tool_calls": [call({"topic": "Topic"}, name=report.name, id="first")],
                "done": True,
            }
        ]
    ]
    if inspect:
        responses.append(
            [
                {
                    "tool_calls": [
                        call(
                            {"artifact_id": "saved-report"},
                            name=reader.name,
                            id="inspect",
                        )
                    ],
                    "done": True,
                }
            ]
        )
    responses += [
        [
            {
                "tool_calls": [
                    call(
                        {
                            "topic": "Different topic" if different_topic else "Topic",
                            "format": "docx",
                            "outline": "New outline",
                        },
                        name=report.name,
                        id="second",
                    )
                ],
                "done": True,
            }
        ],
        [{"content": "Done", "done": True}],
    ]
    captured = script(monkeypatch, responses)
    events = await run()
    assert generated.await_count == (2 if inspect or different_topic else 1)
    assert events[-1]["event"] == "done"
    warnings = [
        event["tool_call"]
        for event in events
        if event["event"] == "tool_call" and event["tool_call"]["status"] == "warning"
    ]
    if not inspect and not different_topic:
        assert warnings and "inspection required" in warnings[0]["title"]
        # It is an application policy deferral, not a schema repair loop.
        feedback = next(
            message
            for message in captured[-1]["messages"]
            if message.get("tool_call_id") == "second"
        )
        assert "not an argument-formatting failure" in feedback["content"]
        assert "<<<UNTRUSTED_SOURCE_DATA>>>" not in feedback["content"]
    else:
        assert not warnings


@pytest.mark.asyncio
async def test_report_chapter_progress_is_streamed_on_the_existing_tool_call(
    agent, monkeypatch
):
    report = ReportGenTool()
    registry = ToolRegistry()
    registry.register(report)
    monkeypatch.setattr(service, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(service, "_select_tools", lambda _: {report.name})

    async def generate(**kwargs):
        for completed in (0, 1, 2):
            await kwargs["progress"](
                {"stage": "writing_chapters", "completed": completed, "total": 2}
            )
        return {
            "type": "report",
            "format": "docx",
            "filename": "report.docx",
            "file_path": "reports/report.docx",
            "download_url": "/report",
            "report_id": "result",
            "created_at": 1,
        }

    monkeypatch.setattr(report_tool, "generate_report", generate)
    monkeypatch.setattr(
        service,
        "register_generated",
        AsyncMock(return_value={"id": "saved", "version": 1}),
    )
    script(
        monkeypatch,
        [
            [
                {
                    "tool_calls": [call({"topic": "Topic"}, name=report.name)],
                    "done": True,
                }
            ],
            [{"content": "Done", "done": True}],
        ],
    )
    builder = BlockBuilder()
    events = await run(on_tool_call_update=builder.on_tool_call_update)
    progress = [
        event["tool_call"]
        for event in events
        if event["event"] == "tool_call" and "progress" in event["tool_call"]
    ]
    assert [item["progress"]["completed"] for item in progress[:3]] == [0, 1, 2]
    assert len({item["id"] for item in progress}) == 1
    persisted = next(
        block for block in builder.to_db_blocks() if block["type"] == "tool_call"
    )
    assert persisted["tool_call"]["progress"]["completed"] == 2


@pytest.mark.parametrize(
    "query, expected",
    [
        (
            "Create a quiz with exactly 5 multiple-choice questions about NumPy basics: array creation, reshape, indexing, axis sums, and broadcasting.",
            "create_quiz",
        ),
        ("Create flashcards about Spanish", "create_flashcard_deck"),
        ("Create a study note about photosynthesis", "create_study_note"),
        ("Create an Excel spreadsheet", "use_excel_gen"),
    ],
)
def test_always_loaded_tools_cannot_crowd_out_request_relevant_schemas(query, expected):
    registry = service.get_tool_registry()
    configs = defaults(registry)
    baseline = {
        "delegate_to_coder",
        "discover_tools",
        "manage_memory",
        "rag_search",
        "use_code_exec",
        "use_webfetch",
        "use_websearch",
    }
    for name in baseline:
        configs[name]["always_load"] = True
    original = deepcopy(configs)
    selected = routing.select_tools(query, registry, configs, limit=1)
    assert baseline <= selected
    assert expected in selected
    assert len(selected - baseline) == 1
    assert configs == original  # Selection never resets a user's preferences.


def test_disabled_and_custom_keyword_preferences_still_apply_with_full_baseline():
    registry = service.get_tool_registry()
    configs = defaults(registry)
    for name in ("use_websearch", "use_webfetch", "use_code_exec", "manage_memory"):
        configs[name]["always_load"] = True
    configs["create_quiz"].update(enabled=False, always_load=True)
    assert "create_quiz" not in routing.select_tools(
        "Create a quiz", registry, configs, limit=1
    )
    configs["create_quiz"].update(
        enabled=True, always_load=False, tags=["Practice Time"]
    )
    assert "create_quiz" not in routing.select_tools(
        "Create a quiz", registry, configs, limit=1
    )
    assert "create_quiz" in routing.select_tools(
        "Practice Time", registry, configs, limit=1
    )


@pytest.mark.parametrize(
    "custom", ["tags", "enabled", "model", "custom", "edited", "unknown_time"]
)
def test_default_migration_preserves_customized_or_uncertain_rows(custom):
    definition = definition_for_tool("use_websearch")
    old = {**definition.default_config(), "always_load": True}
    now = datetime(2026, 10, 14)
    row = SimpleNamespace(
        tool_name="use_websearch", config=deepcopy(old), created_at=now, updated_at=now
    )
    if custom == "edited":
        row.updated_at += timedelta(seconds=5)
    elif custom == "unknown_time":
        row.updated_at = None
    else:
        row.config[custom] = (
            {"changed": True}
            if custom == "custom"
            else {"tags": ["custom"], "enabled": False, "model": "local"}[custom]
        )
    original = deepcopy(row.config)
    assert config_store.migrate_routing_default(row, definition)
    assert {
        key: value for key, value in row.config.items() if not key.startswith("_")
    } == original
    assert not config_store.migrate_routing_default(row, definition)


def test_untouched_old_seed_resets_once_and_never_resets_a_subsequent_user_choice():
    definition = definition_for_tool("use_websearch")
    now = datetime(2026, 10, 14)
    row = SimpleNamespace(
        tool_name="use_websearch",
        config={**definition.default_config(), "always_load": True},
        created_at=now,
        updated_at=now + timedelta(microseconds=2),
    )
    assert config_store.migrate_routing_default(row, definition)
    assert row.config["always_load"] is False
    row.config["always_load"] = True
    assert not config_store.migrate_routing_default(row, definition)
    assert row.config["always_load"] is True
    assert "_routing_policy_version" not in config_store.merged_config(
        "use_websearch", row.config
    )


def test_result_compaction_preserves_references_and_can_be_recovered():
    raw = json.dumps(
        {
            "id": "item-id",
            "content": "é" * 10000,
            "artifact_id": "artifact-id",
            "version": 4,
            "offset": 600,
            "next_offset": None,
            "page": 7,
        }
    )
    view = compact_result("read_artifact", raw, result_ref="result-id", limit=1500)
    assert view.shortened
    data = json.loads(view.content)
    assert data["artifact_id"] == "artifact-id"
    assert data["version"] == 4 and data["page"] == 7
    assert data["next_offset"] == 600 + len(data["content"])
    assert data["result_ref"] == "result-id" and data["shortened"] is True
    assert len(view.content) < len(raw)


def test_citations_all_remain_in_compact_retrieval_and_web_views():
    sources = [
        {
            "document_id": "doc",
            "chunk_id": str(index),
            "document_filename": "Book.pdf",
            "page_number": index + 1,
            "line_start": 2,
            "line_end": 6,
            "text": "evidence " * 1000,
        }
        for index in range(5)
    ]
    tc = SimpleNamespace(rag_sources=sources, web_results=None)
    original = deepcopy(sources)
    data = json.loads(
        compact_result(
            "rag_search", "very long original " * 1000, tc, result_ref="ref", limit=2000
        ).content
    )
    assert sources == original
    assert [record["chunk_id"] for record in data["sources"]] == [
        str(index) for index in range(5)
    ]
    assert all(
        record["line_end"] == 6 and record["excerpt_shortened"]
        for record in data["sources"]
    )
    tc = SimpleNamespace(
        web_results=[
            {
                "title": "Title",
                "url": "https://example.com",
                "snippet": "evidence " * 1000,
            }
        ],
        rag_sources=None,
    )
    data = json.loads(
        compact_result(
            "use_websearch", "original " * 1000, tc, result_ref="ref", limit=1000
        ).content
    )
    assert data["results"][0]["url"] == "https://example.com"


def test_compaction_does_not_change_errors_skills_or_irrecoverable_outputs():
    text = "Full text\n" * 1000
    for name, success, reference in [
        ("load_skill", True, "ref"),
        ("read_tool_result", True, "ref"),
        ("example", False, "ref"),
        ("example", True, None),
    ]:
        view = compact_result(
            name, text, success=success, result_ref=reference, limit=1000
        )
        assert view.content == text and not view.shortened
    metadata_between_bodies = json.dumps(
        {"first": "a" * 3000, "artifact_id": "keep-me", "last": "b" * 3000}
    )
    view = json.loads(
        compact_result(
            "example", metadata_between_bodies, result_ref="ref", limit=1000
        ).content
    )
    assert view["metadata"]["artifact_id"] == "keep-me"


@pytest.mark.asyncio
async def test_result_paging_roundtrip_is_exact_and_request_local():
    tool = ReadToolResultTool()
    text = "النص الأصلي é\n" * 300
    store = {"ref": text}
    offset, pieces = 0, []
    while True:
        response = await tool.execute(
            result_ref="ref", offset=offset, limit=200, _result_store=store
        )
        assert response.success
        data = json.loads(response.output)
        pieces.append(data["content"])
        if data["next_offset"] is None:
            break
        offset = data["next_offset"]
    assert "".join(pieces) == text
    missing = await tool.execute(result_ref="ref", _result_store={})
    assert missing.failure_kind == "validation"


@pytest.mark.asyncio
async def test_output_pages_include_only_scoped_source_metadata():
    tool = ReadToolResultTool()
    page = await tool.execute(
        result_ref="ref",
        offset=0,
        limit=2,
        _result_store={"ref": "🙂abc"},
        _result_metadata={
            "ref": {
                "source_tool": "rag_search",
                "source_title": "Document search",
                "secret": "must not leak",
            }
        },
    )
    data = json.loads(page.output)
    assert data == {
        "result_ref": "ref",
        "content": "🙂a",
        "offset": 0,
        "next_offset": 2,
        "total_chars": 4,
        "source_tool": "rag_search",
        "source_title": "Document search",
    }
    absent = await tool.execute(
        result_ref="ref",
        _result_store={"ref": "text"},
        _result_metadata={"other": {"source_tool": "wrong"}},
    )
    assert "source_tool" not in json.loads(absent.output)


@pytest.mark.asyncio
async def test_discovery_exposes_then_executes_only_requested_enabled_tools(
    agent, monkeypatch
):
    registry = ToolRegistry()
    registry.register(agent)
    registry.register(DiscoverToolsTool())
    monkeypatch.setattr(service, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(service, "_select_tools", lambda _: {"discover_tools"})
    captured = script(
        monkeypatch,
        [
            [
                {
                    "tool_calls": [call({"names": ["echo"]}, name="discover_tools")],
                    "done": True,
                }
            ],
            [{"tool_calls": [call({"input": "one"})], "done": True}],
            [{"content": "Done", "done": True}],
        ],
    )
    events = await run()
    assert events[-1]["event"] == "done"
    assert [schema["function"]["name"] for schema in captured[0]["tools"]] == [
        "discover_tools"
    ]
    assert {schema["function"]["name"] for schema in captured[1]["tools"]} == {
        "discover_tools",
        "echo",
    }
    assert agent.executed == [{"input": "one"}]
    assert len({request["messages"][0]["content"] for request in captured}) == 1


@pytest.mark.asyncio
async def test_omitted_direct_call_loads_schema_but_never_executes_unseen_arguments(
    agent, monkeypatch
):
    monkeypatch.setattr(service, "_select_tools", lambda _: set())
    captured = script(
        monkeypatch,
        [
            [{"tool_calls": [call({"input": "unseen"})], "done": True}],
            [{"tool_calls": [call({"input": "validated"})], "done": True}],
            [{"content": "Done", "done": True}],
        ],
    )
    builder = BlockBuilder()
    events = await run(on_tool_call_update=builder.on_tool_call_update)
    assert events[-1]["event"] == "done"
    assert agent.executed == [{"input": "validated"}]
    assert not captured[0]["tools"] and captured[1]["tools"]
    assert "action was NOT attempted" in str(captured[1]["messages"])
    assert "schema loading SUCCEEDED" in str(captured[1]["messages"])
    feedback = next(m for m in captured[1]["messages"] if m.get("role") == "tool")
    assert "<<<UNTRUSTED_SOURCE_DATA>>>" not in feedback["content"]
    ledger = next(
        m for m in captured[1]["messages"] if "schema_loads" in m.get("content", "")
    )
    assert '"status":"schema_ready"' in ledger["content"]
    assert '"latest_failures":[]' in ledger["content"]
    access = [
        event["tool_call"]
        for event in events
        if event["event"] == "tool_call" and event["tool_call"]["type"] == "tool_access"
    ]
    assert [item["status"] for item in access] == ["warning", "completed"]
    assert access[0]["title"] == "echo"
    assert access[0]["id"] != access[1]["id"]
    assert json.loads(access[0]["output"]) == {
        "operation": "deferred_call",
        "requested": ["echo"],
        "reason": "schema_not_loaded",
        "action_executed": False,
    }
    assert json.loads(access[1]["output"])["loaded"] == ["echo"]
    assert access[1]["durationMs"] >= 0
    assert [block["tool_call"] for block in builder.to_db_blocks()[:2]] == access


@pytest.mark.asyncio
async def test_discovery_details_distinguish_new_schemas_from_existing_ones():
    loader = AsyncMock(return_value=["echo", "other"])
    result = await DiscoverToolsTool().execute(
        names=["echo", "other"], _tool_loader=loader, _loaded_tools={"echo"}
    )
    assert result.success
    assert json.loads(result.output) == {
        "operation": "load_schema",
        "requested": ["echo", "other"],
        "loaded": ["other"],
        "already_loaded": ["echo"],
        "action_executed": False,
        "message": "Schemas are now available. Call a loaded tool only if needed for the original request.",
    }
    loader.assert_awaited_once_with(["echo", "other"])


@pytest.mark.asyncio
async def test_disabled_and_notebook_forbidden_tools_cannot_be_discovered(
    agent, monkeypatch
):
    registry = ToolRegistry()
    registry.register(agent)
    registry.register(DiscoverToolsTool())
    monkeypatch.setattr(service, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(service, "_select_tools", lambda _: {"discover_tools"})
    monkeypatch.setattr(
        config_store, "get_all_tool_configs", lambda: {"echo": {"enabled": False}}
    )
    captured = script(
        monkeypatch,
        [
            [
                {
                    "tool_calls": [call({"names": ["echo"]}, name="discover_tools")],
                    "done": True,
                }
            ],
            [
                {
                    "content": json.dumps(
                        {"tool": "discover_tools", "args": {"names": ["echo"]}}
                    ),
                    "done": True,
                }
            ],
            [
                {
                    "content": json.dumps(
                        {"tool": "discover_tools", "args": {"names": ["echo"]}}
                    ),
                    "done": True,
                }
            ],
        ],
    )
    events = await run()
    assert events[-1]["event"] == "error"
    assert len(captured) == 3
    assert not agent.executed
    assert '"name":"echo"' not in str(captured[0]["messages"])
    assert "echo" not in routing.enabled_tools(registry, {}, notebook=True)


@pytest.mark.asyncio
async def test_large_result_is_full_in_ui_but_shortened_in_model_context(
    agent, monkeypatch
):
    registry = ToolRegistry()
    registry.register(agent)
    registry.register(ReadToolResultTool())
    monkeypatch.setattr(service, "get_tool_registry", lambda: registry)
    raw = "BEGIN\n" + "payload\n" * 3000 + "END"
    monkeypatch.setattr(agent, "execute", AsyncMock(return_value=ToolResult(True, raw)))
    monkeypatch.setattr(service.settings, "AGENT_TOOL_RESULT_MAX_CHARS", 1500)
    captured = script(
        monkeypatch,
        [
            [{"tool_calls": [call({"input": "one"})], "done": True}],
            [{"content": "Done", "done": True}],
        ],
    )
    events = await run()
    completed = [
        event["tool_call"]
        for event in events
        if event["event"] == "tool_call" and event["tool_call"]["status"] == "completed"
    ]
    assert completed[0]["output"] == raw
    result = next(
        message for message in captured[1]["messages"] if message["role"] == "tool"
    )
    assert raw not in result["content"] and '"shortened":true' in result["content"]
    assert "read_tool_result" in {
        schema["function"]["name"] for schema in captured[1]["tools"]
    }


@pytest.mark.asyncio
async def test_paged_output_metadata_is_emitted_for_ui_and_persistence(
    agent, monkeypatch
):
    registry = ToolRegistry()
    registry.register(agent)
    registry.register(ReadToolResultTool())
    monkeypatch.setattr(service, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(service.settings, "AGENT_TOOL_RESULT_MAX_CHARS", 1000)
    raw = "🙂" + "text " * 1000
    monkeypatch.setattr(agent, "execute", AsyncMock(return_value=ToolResult(True, raw)))
    first = call({"input": "one"})
    builder = BlockBuilder()
    script(
        monkeypatch,
        [
            [{"tool_calls": [first], "done": True}],
            [
                {
                    "tool_calls": [
                        call(
                            {"result_ref": first["id"], "offset": 0, "limit": 80},
                            name="read_tool_result",
                        )
                    ],
                    "done": True,
                }
            ],
            [{"content": "Done", "done": True}],
        ],
    )
    events = await run(on_tool_call_update=builder.on_tool_call_update)
    page = next(
        event["tool_call"]
        for event in events
        if event["event"] == "tool_call"
        and event["tool_call"]["type"] == "tool_output"
        and event["tool_call"]["status"] == "completed"
    )
    data = json.loads(page["output"])
    assert data["source_tool"] == agent.name
    assert data["source_title"] == agent.get_display_name()
    assert data["content"] == raw[:80]
    assert data["next_offset"] == 80
    assert (
        next(
            block["tool_call"]["output"]
            for block in builder.to_db_blocks()
            if block["tool_call"]["type"] == "tool_output"
        )
        == page["output"]
    )


@pytest.mark.asyncio
async def test_core_prompt_is_identical_across_tools_protocols_and_resource_metadata():
    first = await service._build_system_prompt(
        {"create_quiz"}, native_tools=True, skill_catalog="First skill"
    )
    second = await service._build_system_prompt(
        {"use_pptx_gen"}, native_tools=False, skill_catalog="Second skill"
    )
    assert first == second
    assert "First skill" not in first and "Second skill" not in second


def test_runtime_counts_are_reported_not_inferred():
    stats = runtime_usage(
        {"prompt_eval_count": 500, "eval_count": 20, "prompt_eval_duration": 120000}
    )
    assert stats["prompt_tokens"] == 500 and stats["cached_prompt_tokens"] is None
    stats = runtime_usage({"prompt_eval_count": 500, "prompt_eval_cached_count": 0})
    assert stats["cached_prompt_tokens"] == 0
    stats = runtime_usage(
        {"prompt_eval_cached_count": -1, "eval_duration": float("nan")}
    )
    assert (
        stats["cached_prompt_tokens"] is None
        and stats["generation_duration_ns"] is None
    )
    assert runtime_usage({"message": {"content": "Hello"}}) is None


def test_trace_preserves_raw_result_model_view_and_runtime_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(tracing.settings, "AGENT_TRACE_DIR", str(tmp_path))
    trace = tracing.Trace({"model": "local"})
    trace.begin_round(
        1, "local", [{"role": "system", "content": "Stable"}], [], [], [], {}
    )
    trace.begin_tool("example", {})
    raw = "raw output"
    trace.end_tool("completed", raw)
    trace.result_view("ref", "short view", True)
    trace.chunk(
        {
            "usage": runtime_usage(
                {"prompt_eval_count": 12, "prompt_eval_cached_count": 10}
            )
        }
    )
    assert trace.round["tool_calls"][0]["output"] == raw
    assert trace.round["tool_calls"][0]["model_view"]["output"] == "short view"
    assert trace.round["runtime_usage"]["cached_prompt_tokens"] == 10
    assert len(trace.round["prefix"]["system_sha256"]) == 64
