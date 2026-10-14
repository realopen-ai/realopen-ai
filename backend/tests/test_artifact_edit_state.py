"""Agent edits require actual version-specific reads, not schema loading or guesses."""

import json
from unittest.mock import AsyncMock

import pytest

from app.agent import service
from app.agent.artifact_edit_state import ArtifactEditState
from app.agent.base import ToolRegistry, ToolResult
from app.agent.tools.artifacts import ReadArtifactTool, UpdateArtifactTool
from app.agent.tools import config_store
from app.services.blocks import BlockBuilder
from test_agent_protocol import agent as agent_fixture, script, call, run

ARTIFACT = "42555820-22d0-4827-beaf-f0792cf8a81b"
OTHER = "a82d5051-7a67-42df-98bb-70e1f0e20b45"


def outline(identifier=ARTIFACT, version=1):
    return json.dumps(
        {
            "artifact_id": identifier,
            "version": version,
            "sections": [
                {"id": "section-1", "title": "Overview"},
                {"id": "section-2", "title": "Conclusion"},
            ],
        }
    )


def edit(section="section-2", version=1, operation="delete", identifier=ARTIFACT):
    change = {"section_id": section, "operation": operation}
    if operation == "replace":
        change["content"] = "## Conclusion\nUpdated body."
    return {"artifact_id": identifier, "expected_version": version, "changes": [change]}


@pytest.mark.parametrize("section", ["Conclusion", "conclusion_section", "5buh1p4i"])
def test_invented_section_ids_do_not_pass_after_outline_read(section):
    state = ArtifactEditState()
    state.record_read(outline())
    assert state.missing_read(edit(section))
    assert state.missing_read(edit()) is None


def test_receipts_are_version_artifact_and_request_specific():
    state = ArtifactEditState()
    assert state.missing_read(edit())
    state.record_read(outline())
    assert state.missing_read(edit()) is None
    assert state.missing_read(edit(version=2))
    assert state.missing_read(edit(identifier=OTHER))
    assert ArtifactEditState().missing_read(edit())
    state.record_read(outline(OTHER))
    state.invalidate(ARTIFACT)
    assert state.missing_read(edit())
    assert state.missing_read(edit(identifier=OTHER)) is None


def test_replacements_require_target_content_not_just_outline():
    state = ArtifactEditState()
    state.record_read(outline())
    assert state.missing_read(edit(operation="replace"))
    state.record_read(
        json.dumps(
            {
                "artifact_id": ARTIFACT,
                "version": 1,
                "id": "section-2",
                "content": "## Conclusion\nOld body.",
            }
        )
    )
    assert state.missing_read(edit(operation="replace")) is None
    assert state.missing_read(edit("section-1", operation="replace"))


@pytest.mark.parametrize(
    "output",
    [
        "malformed",
        "null",
        "[]",
        json.dumps(
            {
                "artifact_id": ARTIFACT,
                "version": True,
                "sections": [{"id": "section-2"}],
            }
        ),
        json.dumps({"artifact_id": ARTIFACT, "version": 1}),
    ],
)
def test_invalid_or_metadata_only_output_does_not_authorize_edit(output):
    state = ArtifactEditState()
    state.record_read(output)
    assert state.missing_read(edit())


@pytest.fixture
def agent(monkeypatch):
    return agent_fixture.__wrapped__(monkeypatch)


def setup_tools(monkeypatch, read_result=None):
    registry = ToolRegistry()
    reader, updater = ReadArtifactTool(), UpdateArtifactTool()
    reader.execute = AsyncMock(return_value=read_result or ToolResult(True, outline()))
    updater.execute = AsyncMock(
        return_value=ToolResult(True, json.dumps({"id": ARTIFACT, "version": 2}))
    )
    registry.register(reader)
    registry.register(updater)
    monkeypatch.setattr(service, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(service, "_select_tools", lambda _: {"update_artifact"})
    return reader, updater


def frame(name, args, native):
    return (
        {"tool_calls": [call(args, name=name)], "done": True}
        if native
        else {
            "content": "```tool\n" + json.dumps({"tool": name, "args": args}) + "\n```",
            "done": True,
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_direct_update_loads_reader_then_successful_read_allows_delete(
    agent, monkeypatch, native
):
    reader, updater = setup_tools(monkeypatch)
    monkeypatch.setattr(service, "_model_supports_native_tools", lambda _: native)
    captured = script(
        monkeypatch,
        [
            [frame("update_artifact", edit("Conclusion"), native)],
            [frame("read_artifact", {"artifact_id": ARTIFACT}, native)],
            [frame("update_artifact", edit(), native)],
            [{"content": "Done", "done": True}],
        ],
    )
    builder = BlockBuilder()
    events = await run(on_tool_call_update=builder.on_tool_call_update)
    assert events[-1]["event"] == "done"
    reader.execute.assert_awaited_once()
    updater.execute.assert_awaited_once()
    assert updater.execute.await_args.kwargs["changes"] == edit()["changes"]
    feedback = next(
        m["content"]
        for m in captured[1]["messages"]
        if m["content"].startswith("This update_artifact call was NOT executed.")
    )
    assert "NOT executed" in feedback and "read_artifact" in feedback
    assert "UNTRUSTED_SOURCE_DATA" not in feedback
    assert not any("error" == event["event"] for event in events)
    warning = next(
        event["tool_call"]
        for event in events
        if event.get("tool_call", {}).get("status") == "warning"
    )
    assert "Read artifact" in warning["title"]
    assert warning["type"] == "tool_access"
    assert json.loads(warning["output"]) == {
        "operation": "read_required",
        "requested": ["update_artifact"],
        "required_tools": ["read_artifact"],
        "action_executed": False,
    }
    assert (
        "NOT executed" not in warning["output"]
    )  # Internal repair guidance is model-only.
    persisted = next(
        block["tool_call"]
        for block in builder.to_db_blocks()
        if block.get("tool_call", {}).get("id") == warning["id"]
    )
    assert persisted["output"] == warning["output"]
    if native:
        assert "read_artifact" in {
            item["function"]["name"] for item in captured[1]["tools"]
        }


@pytest.mark.asyncio
async def test_schema_loading_and_failed_reads_do_not_unlock_updates(
    agent, monkeypatch
):
    reader, updater = setup_tools(monkeypatch, ToolResult(False, "Read failed"))
    script(
        monkeypatch,
        [
            [frame("read_artifact", {"artifact_id": ARTIFACT}, True)],  # schema only
            [frame("update_artifact", edit(), True)],
            [frame("read_artifact", {"artifact_id": ARTIFACT}, True)],  # fails
            [frame("update_artifact", edit(), True)],
            [{"content": "Cannot edit", "done": True}],
        ],
    )
    await run()
    reader.execute.assert_awaited_once()
    updater.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_json_encoded_changes_cannot_bypass_unknown_target_guard(
    agent, monkeypatch
):
    reader, updater = setup_tools(monkeypatch)
    monkeypatch.setattr(
        service, "_select_tools", lambda _: {"update_artifact", "read_artifact"}
    )
    args = edit("invented")
    args["changes"] = json.dumps(args["changes"])
    script(
        monkeypatch,
        [
            [frame("read_artifact", {"artifact_id": ARTIFACT}, True)],
            [frame("update_artifact", args, True)],
            [{"content": "Need exact ID", "done": True}],
        ],
    )
    await run()
    updater.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_disabled_reader_is_not_enabled_to_satisfy_prerequisite(
    agent, monkeypatch
):
    reader, updater = setup_tools(monkeypatch)
    configs = {"read_artifact": {"enabled": False}}
    monkeypatch.setattr(config_store, "get_all_tool_configs", lambda: configs)
    monkeypatch.setattr(
        config_store, "get_tool_config", lambda name: configs.get(name, {})
    )
    captured = script(
        monkeypatch,
        [
            [frame("update_artifact", edit(), True)],
            [{"content": "Reading is disabled", "done": True}],
        ],
    )
    await run()
    reader.execute.assert_not_awaited()
    updater.execute.assert_not_awaited()
    assert "read_artifact" not in {
        item["function"]["name"] for item in captured[1]["tools"]
    }


@pytest.mark.asyncio
async def test_updated_version_requires_fresh_read_before_another_edit(
    agent, monkeypatch
):
    reader, updater = setup_tools(monkeypatch)
    monkeypatch.setattr(
        service, "_select_tools", lambda _: {"update_artifact", "read_artifact"}
    )
    reader.execute.side_effect = [
        ToolResult(True, outline()),
        ToolResult(True, outline(version=2)),
    ]
    script(
        monkeypatch,
        [
            [frame("read_artifact", {"artifact_id": ARTIFACT}, True)],
            [frame("update_artifact", edit(), True)],
            [frame("update_artifact", edit(version=2), True)],  # blocked until reread
            [frame("update_artifact", edit(), True)],  # old receipt invalidated too
            [frame("read_artifact", {"artifact_id": ARTIFACT}, True)],
            [frame("update_artifact", edit(version=2), True)],
            [{"content": "Done", "done": True}],
        ],
    )
    await run()
    assert reader.execute.await_count == 2
    assert updater.execute.await_count == 2
    assert [
        item.kwargs["expected_version"] for item in updater.execute.await_args_list
    ] == [1, 2]


@pytest.mark.asyncio
async def test_previous_conversation_tool_text_is_not_a_current_read_receipt(
    agent, monkeypatch
):
    reader, updater = setup_tools(monkeypatch)
    script(
        monkeypatch,
        [
            [frame("update_artifact", edit(), True)],
            [{"content": "Need a fresh read", "done": True}],
        ],
    )
    await run(
        messages=[
            {
                "role": "tool",
                "tool_name": "read_artifact",
                "tool_call_id": "old",
                "content": outline(),
            },
            {"role": "user", "content": "Delete the conclusion"},
        ]
    )
    updater.execute.assert_not_awaited()
    reader.execute.assert_not_awaited()
