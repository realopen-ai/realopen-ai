from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.services import artifacts, report_gen
from app.services.artifact_sources import sections_for
from app.services.artifact_summary import summarize_sections
from app.agent.tools import report_gen as report_tool


def test_section_replacement_without_final_newline_preserves_next_heading():
    source = "# Title\nIntro\n\n## Summary\nFacts.\n"
    version = SimpleNamespace(source=source, sections=sections_for(source, "report"))
    edited = artifacts.edited_source(
        version, "report", [{"section_id": "section-1", "content": "# New\nBeta."}]
    )
    assert "Beta.\n\n## Summary" in edited
    assert len(sections_for(edited, "report")) == 2


@pytest.mark.asyncio
async def test_source_provenance_survives_model_dropping_markers():
    result = await summarize_sections(
        [
            {"id": "page-4", "page": 4, "content": "A grounded definition."},
        ],
        "One bullet",
        AsyncMock(return_value="- A definition."),
    )
    assert result["source_references"] == [{"id": "page-4", "page": 4}]
    assert "page 4" in result["summary"]


@pytest.mark.asyncio
async def test_report_constraints_are_required_not_suggested(monkeypatch):
    chat = AsyncMock(return_value={"message": {"content": "# QA\n## Overview\nTest."}})
    monkeypatch.setattr(report_gen.providers, "chat_once", chat)
    await report_gen._generate_markdown("QA", "Exactly three short sections", "test")
    prompt = chat.call_args.args[1][1]["content"]
    assert "Required structure" in prompt
    assert "Exactly three short sections" in prompt
    assert "you may adapt" not in prompt
    assert "Do not add an executive summary" in chat.call_args.args[1][0]["content"]


@pytest.mark.asyncio
async def test_report_keeps_original_user_constraints(monkeypatch):
    from app.agent.tools import config_store

    generate = AsyncMock(
        return_value={
            "report_id": "qa",
            "filename": "qa.docx",
            "format": "docx",
            "file_path": "reports/qa.docx",
            "download_url": "/api/reports/qa/download",
            "created_at": "2026-10-08T00:00:00Z",
        }
    )
    monkeypatch.setattr(report_tool, "generate_report", generate)
    monkeypatch.setattr(config_store, "tool_model_override", AsyncMock(return_value=None))
    result = await report_tool.ReportGenTool().execute(
        topic="QA",
        outline="Overview | Plan | Results",
        format="docx",
        _user_request="Under 100 words, no executive summary.",
    )
    assert result.success
    assert "Under 100 words" in generate.call_args.kwargs["outline"]


@pytest.mark.asyncio
async def test_truncated_summary_is_not_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("REALOPEN_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(artifacts.model_prefs, "resolve_task_model", AsyncMock(return_value="test"))

    async def stream(*args, **kwargs):
        yield {"content": "An incomplete", "done": True, "finish_reason": "length"}

    monkeypatch.setattr(artifacts.providers, "stream_chat", stream)
    with pytest.raises(RuntimeError, match="output limit"):
        await artifacts.summarize(
            None,
            SimpleNamespace(id="test"),
            SimpleNamespace(
                number=1,
                source="Facts",
                sections=[{"id": "one", "content": "Facts"}],
                settings={},
            ),
        )
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.asyncio
async def test_cutoff_omits_unfinished_sentence_and_marks_abridged(tmp_path, monkeypatch):
    monkeypatch.setenv("REALOPEN_DATA_DIR", str(tmp_path))

    async def stream(*args, **kwargs):
        assert args[0] == "override"
        yield {
            "content": "- Complete fact. Unfinished ending",
            "done": True,
            "finish_reason": "length",
        }

    monkeypatch.setattr(artifacts.providers, "stream_chat", stream)
    result = await artifacts.summarize(
        None,
        SimpleNamespace(id="test"),
        SimpleNamespace(
            number=1,
            source="Facts",
            sections=[{"id": "one", "content": "Facts"}],
            settings={},
        ),
        model="override",
    )
    assert "Unfinished ending" not in result["summary"]
    assert "abbreviated" in result["summary"]
    assert result["warnings"]
