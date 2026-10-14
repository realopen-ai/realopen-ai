"""Argument repairs must not reinterpret service, permission or I/O failures."""

import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException

from app.agent.base import ToolCall, ToolResult, ToolType
from app.agent.coder.agent import CoderAgent
from app.agent.tools import (
    artifacts,
    code_exec,
    excel_gen,
    flashcards,
    image_gen,
    load_skill,
    manage_memory,
    notes,
    pptx_gen,
    quizzes,
    rag_search,
    report_gen,
    search_past_conversations,
    vision,
    web_fetch,
    web_search,
    config_store,
)
from app.services import skills


@asynccontextmanager
async def session():
    yield SimpleNamespace(commit=AsyncMock())


@pytest.mark.parametrize("kind", [None, "execution", "validation"])
def test_unspecified_failure_is_operational_and_visible(kind):
    call = ToolCall("id", ToolType.WEB_SEARCH, "test", status="completed")
    result = ToolResult(False, "failure", call, kind)
    assert result.failure_kind == (kind or "execution")
    assert call.status == "error"
    assert call.error == "failure"
    assert call.completed_at is not None
    success = ToolResult(True, "ok")
    assert success.failure_kind is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool,arguments",
    [
        (web_search.WebSearchTool(), {"query": "  "}),
        (rag_search.RagSearchTool(), {"query": "  "}),
        (search_past_conversations.SearchPastConversationsTool(), {"query": "  "}),
        (report_gen.ReportGenTool(), {"topic": "  "}),
        (pptx_gen.PptxGenTool(), {"topic": "  "}),
        (excel_gen.ExcelGenTool(), {"brief": "  "}),
        (image_gen.ImageGenTool(), {"prompt": "  "}),
        (load_skill.LoadSkillTool(), {"name": "  "}),
        (CoderAgent(), {"task": "  "}),
        (code_exec.CodeExecTool(), {"code": "  "}),
        (code_exec.CodeExecTool(), {"code": "print(", "language": "python"}),
        (code_exec.CodeExecTool(), {"code": "puts 'hi'", "language": "ruby"}),
        (manage_memory.ManageMemoryTool(), {"action": "edit"}),
        (manage_memory.ManageMemoryTool(), {"action": "edit", "memory_id": "abcdef"}),
        (
            manage_memory.ManageMemoryTool(),
            {"action": "add", "text": "hi", "category": "made-up"},
        ),
        (artifacts.ReadArtifactTool(), {"artifact_id": "made-up"}),
        (artifacts.ListArtifactsTool(), {"offset": "invalid"}),
        (
            artifacts.UpdateArtifactTool(),
            {"artifact_id": str(uuid.uuid4()), "expected_version": 1, "changes": []},
        ),
    ],
)
async def test_invalid_inputs_are_rejected_before_side_effects(
    tool, arguments, monkeypatch
):
    def unexpected(*args, **kwargs):
        pytest.fail("Rejected arguments reached a service, database, or subprocess")

    for module in (artifacts, manage_memory, rag_search, search_past_conversations):
        monkeypatch.setattr(module, "async_session_factory", unexpected)
    for module, function in (
        (report_gen, "generate_report"),
        (pptx_gen, "generate_presentation"),
        (excel_gen, "generate_spreadsheet"),
    ):
        monkeypatch.setattr(module, function, unexpected)
    monkeypatch.setattr(web_search.web_search_service, "search", unexpected)
    monkeypatch.setattr(image_gen.image_gen_service, "generate", unexpected)
    monkeypatch.setattr(config_store, "resolve_tool_model", unexpected)
    monkeypatch.setattr(code_exec.CodeExecTool, "_run_code", unexpected)
    result = await tool.execute(**arguments)
    assert not result.success
    assert result.failure_kind == "validation"
    assert result.tool_call.status == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url", ["", "relative/path", "ftp://example.com", "http://", "http://[broken"]
)
async def test_bad_fetch_url_is_validation_without_network(url, monkeypatch):
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: pytest.fail("Invalid URL reached network"),
    )
    result = await web_fetch.WebFetchTool().execute(url=url)
    assert not result.success
    assert result.failure_kind == "validation"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,tool,function,arguments",
    [
        (
            report_gen,
            report_gen.ReportGenTool(),
            "generate_report",
            {"topic": "Report"},
        ),
        (
            pptx_gen,
            pptx_gen.PptxGenTool(),
            "generate_presentation",
            {"topic": "Slides"},
        ),
        (
            excel_gen,
            excel_gen.ExcelGenTool(),
            "generate_spreadsheet",
            {"brief": "Workbook"},
        ),
    ],
)
@pytest.mark.parametrize(
    "error",
    [
        TimeoutError("Timed out"),
        PermissionError("Denied"),
        ValueError("Invalid renderer output"),
    ],
)
async def test_generation_service_errors_are_not_caller_validation(
    module, tool, function, arguments, error, monkeypatch
):
    monkeypatch.setattr(module, function, AsyncMock(side_effect=error))
    monkeypatch.setattr(
        config_store, "tool_model_override", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(
        pptx_gen, "_get_available_templates_from_db", AsyncMock(return_value=["modern"])
    )
    result = await tool.execute(**arguments)
    assert not result.success
    assert result.failure_kind == "execution"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,kind",
    [
        (400, "validation"),
        (422, "validation"),
        (403, "execution"),
        (404, "execution"),
        (409, "execution"),
        (503, "execution"),
    ],
)
async def test_artifact_http_failures_preserve_operation_boundaries(
    status, kind, monkeypatch
):
    monkeypatch.setattr(artifacts, "async_session_factory", session)
    monkeypatch.setattr(
        artifacts.service,
        "get_artifact",
        AsyncMock(side_effect=HTTPException(status, "Failure")),
    )
    result = await artifacts.ReadArtifactTool().execute(artifact_id=str(uuid.uuid4()))
    assert not result.success
    assert result.failure_kind == kind


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ValueError("Invalid internal data"),
        KeyError("missing output"),
        TimeoutError("Timed out"),
    ],
)
async def test_artifact_untyped_internal_failures_are_execution(error, monkeypatch):
    monkeypatch.setattr(artifacts, "async_session_factory", session)
    monkeypatch.setattr(artifacts.service, "get_artifact", AsyncMock(side_effect=error))
    result = await artifacts.ReadArtifactTool().execute(artifact_id=str(uuid.uuid4()))
    assert result.failure_kind == "execution"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "module,tool,function,arguments",
    [
        (
            quizzes,
            quizzes.CreateQuizTool(),
            "save_quiz",
            {
                "title": "Quiz",
                "questions": [
                    {
                        "type": "multiple_choice",
                        "prompt": "Two plus two?",
                        "options": ["Four", "Five"],
                        "correct_option": 0,
                    }
                ],
            },
        ),
        (
            flashcards,
            flashcards.CreateFlashcardDeckTool(),
            "create_deck",
            {"title": "Deck", "cards": [{"front": "Question?", "back": "Answer."}]},
        ),
        (
            notes,
            notes.CreateStudyNoteTool(),
            "create_note",
            {"title": "Note", "content": "## Topic\nExplanation"},
        ),
    ],
)
@pytest.mark.parametrize(
    "status,kind", [(422, "validation"), (403, "execution"), (503, "execution")]
)
async def test_learn_tools_do_not_repair_permissions_or_server_errors(
    module, tool, function, arguments, status, kind, monkeypatch
):
    monkeypatch.setattr(module, "async_session_factory", session)
    save = AsyncMock(side_effect=HTTPException(status, "Failure"))
    monkeypatch.setattr(module, function, save)
    result = await tool.execute(**arguments)
    save.assert_awaited_once()
    assert result.failure_kind == kind
    assert not result.success


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,kind",
    [
        (skills.SkillReferenceError("Bad reference"), "validation"),
        (skills.SkillReferenceNotFound("Missing resource"), "validation"),
        (ValueError("Malformed stored skill metadata"), "execution"),
        (PermissionError("Denied"), "execution"),
    ],
)
async def test_skill_reference_errors_are_distinct_from_storage_errors(
    error, kind, monkeypatch
):
    def fail(*args):
        raise error

    monkeypatch.setattr(skills, "load_for_agent", fail)
    result = await load_skill.LoadSkillTool().execute(name="example")
    assert result.failure_kind == kind


@pytest.mark.asyncio
async def test_search_and_image_provider_failures_remain_execution(monkeypatch):
    monkeypatch.setattr(
        web_search.web_search_service,
        "search",
        AsyncMock(side_effect=ValueError("Invalid provider response")),
    )
    assert (
        await web_search.WebSearchTool().execute(query="Weather")
    ).failure_kind == "execution"
    monkeypatch.setattr(
        config_store, "resolve_tool_model", AsyncMock(return_value="local")
    )
    monkeypatch.setattr(
        image_gen.image_gen_service,
        "generate",
        AsyncMock(side_effect=TimeoutError("Timeout")),
    )
    assert (
        await image_gen.ImageGenTool().execute(prompt="Landscape")
    ).failure_kind == "execution"
    monkeypatch.setattr(
        vision.VisionTool,
        "_query_vision_model",
        AsyncMock(side_effect=ValueError("Invalid provider response")),
    )
    assert (
        await vision.VisionTool().execute(image_base64="attachment", model="local")
    ).failure_kind == "execution"


def test_invalid_sheet_replacement_is_validation_not_internal_source_failure():
    version = SimpleNamespace(
        source='{"sheets":[{"name":"Sheet"}]}', sections=[{"id": "sheet-1"}]
    )
    with pytest.raises(HTTPException) as error:
        artifacts.service.edited_source(
            version, "excel", [{"section_id": "sheet-1", "content": "not JSON"}]
        )
    assert error.value.status_code == 422
    version.source = "Corrupt stored data"
    with pytest.raises(ValueError):
        artifacts.service.edited_source(
            version, "excel", [{"section_id": "sheet-1", "content": "{}"}]
        )
