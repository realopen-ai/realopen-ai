"""Tests for the Report Generation agent tool (app/agent/tools/report_gen.py).

Scope:
- Tool schema (parameters, required params, aliases) and registry entry.
- execute(): delegation to the report service with the right arguments
  (topic stripping, format resolution incl. fallback to the configured
  default, model override pass-through), deliverable metadata in
  gen_results, empty-topic rejection, and service failures.
- _log()'s defensive formatting branch.
- Config helpers: _configured_default_format and the
  _validate_report_custom validator.

Mocks:
- app.services.report_gen.generate_report is patched in the tool's
  namespace (no LLM, no WeasyPrint/python-docx run, no file writes).
- config_store.tool_model_override / get_tool_config are patched where
  a persisted tool configuration is needed.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import app.agent.tools  # noqa: E402,F401
from app.agent.base import ToolType, get_tool_registry  # noqa: E402
from app.agent.tools.config_base import config_registry  # noqa: E402
from app.agent.tools.report_gen import (  # noqa: E402
    DEFAULT_FORMAT,
    REPORT_GEN_CONFIG,
    ReportGenTool,
    _configured_default_format,
    _log,
    _validate_report_custom,
)


def _service_result(fmt="pdf"):
    return {
        "format": fmt,
        "filename": f"climate-report.{fmt}",
        "file_path": f"reports/rid.{fmt}",
        "download_url": "/api/reports/rid/download",
        "report_id": "rid",
        "created_at": 1_700_000_000,
    }


# ── Schema ──────────────────────────────────────────────────────────


class TestReportGenSchema:
    def test_parameters_and_required_params(self):
        tool = ReportGenTool()
        params = tool.get_parameters()
        assert set(params) == {"topic", "outline", "format"}
        assert params["format"]["enum"] == ["pdf", "docx"]
        assert tool.get_required_params() == ["topic"]

    def test_aliases(self):
        aliases = ReportGenTool.param_aliases
        assert aliases == {
            "topic": "topic",
            "outline": "outline",
            "format": "format",
            "requirements": "outline",
        }

    def test_tool_metadata_and_registry(self):
        tool = ReportGenTool()
        assert tool.name == "use_report_gen"
        assert tool.tool_type is ToolType.IMAGE_GEN
        assert get_tool_registry().has_tool("use_report_gen")

    def test_config_definition_registered(self):
        definition = config_registry.get("use_report_gen")
        assert definition is REPORT_GEN_CONFIG
        assert definition.model_task_slot == "report"
        assert definition.custom_defaults == {"default_format": DEFAULT_FORMAT}
        schema = definition.schema_dict()
        assert schema[0]["key"] == "output"


# ── execute() ───────────────────────────────────────────────────────


class TestReportGenExecute:
    @pytest.mark.asyncio
    async def test_pdf_happy_path(self):
        generate = AsyncMock(return_value=_service_result("pdf"))
        with (
            patch("app.agent.tools.report_gen.generate_report", generate),
            patch(
                "app.agent.tools.config_store.tool_model_override",
                AsyncMock(return_value=None),
            ),
        ):
            result = await ReportGenTool().execute(
                topic="  Climate change  ", outline="Intro; Impacts"
            )

        assert result.success is True
        generate.assert_awaited_once_with(
            topic="Climate change",
            outline="Intro; Impacts",
            format="pdf",
            model=None,
        )

        gen = result.tool_call.gen_results
        assert gen and gen[0]["type"] == "report"
        assert gen[0]["filename"] == "climate-report.pdf"
        assert gen[0]["download_url"] == "/api/reports/rid/download"
        assert gen[0]["report_id"] == "rid"
        assert gen[0]["created_at"] == 1_700_000_000
        assert result.tool_call.status == "completed"

        assert "climate-report.pdf" in result.output
        assert "PDF" in result.output
        assert "ready for download" in result.output

    @pytest.mark.asyncio
    async def test_docx_explicit_format(self):
        generate = AsyncMock(return_value=_service_result("docx"))
        with (
            patch("app.agent.tools.report_gen.generate_report", generate),
            patch(
                "app.agent.tools.config_store.tool_model_override",
                AsyncMock(return_value=None),
            ),
        ):
            result = await ReportGenTool().execute(topic="T", format="docx")
        assert generate.await_args.kwargs["format"] == "docx"
        assert result.success is True
        assert result.tool_call.gen_results[0]["format"] == "docx"

    @pytest.mark.asyncio
    async def test_model_override_forwarded(self):
        generate = AsyncMock(return_value=_service_result())
        with (
            patch("app.agent.tools.report_gen.generate_report", generate),
            patch(
                "app.agent.tools.config_store.tool_model_override",
                AsyncMock(return_value="custom-report-model"),
            ),
        ):
            await ReportGenTool().execute(topic="T")
        assert generate.await_args.kwargs["model"] == "custom-report-model"

    @pytest.mark.asyncio
    async def test_invalid_format_falls_back_to_configured_default(self):
        generate = AsyncMock(return_value=_service_result("docx"))
        cfg = {"custom": {"default_format": "docx"}}
        with (
            patch("app.agent.tools.report_gen.generate_report", generate),
            patch("app.agent.tools.config_store.get_tool_config", return_value=cfg),
            patch(
                "app.agent.tools.config_store.tool_model_override",
                AsyncMock(return_value=None),
            ),
        ):
            await ReportGenTool().execute(topic="T", format="pptx")
        assert generate.await_args.kwargs["format"] == "docx"

    @pytest.mark.asyncio
    async def test_missing_format_uses_configured_default(self):
        generate = AsyncMock(return_value=_service_result("docx"))
        cfg = {"custom": {"default_format": "docx"}}
        with (
            patch("app.agent.tools.report_gen.generate_report", generate),
            patch("app.agent.tools.config_store.get_tool_config", return_value=cfg),
            patch(
                "app.agent.tools.config_store.tool_model_override",
                AsyncMock(return_value=None),
            ),
        ):
            await ReportGenTool().execute(topic="T")
        assert generate.await_args.kwargs["format"] == "docx"

    @pytest.mark.asyncio
    async def test_empty_topic_rejected(self):
        generate = AsyncMock()
        with patch("app.agent.tools.report_gen.generate_report", generate):
            result = await ReportGenTool().execute(topic="   ")
        assert result.success is False
        assert "topic cannot be empty" in result.output
        assert result.tool_call.status == "error"
        assert result.tool_call.error == "Topic cannot be empty"
        generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_none_topic_rejected(self):
        with patch("app.agent.tools.report_gen.generate_report", AsyncMock()):
            result = await ReportGenTool().execute(topic=None)
        assert result.success is False
        assert "topic cannot be empty" in result.output

    @pytest.mark.asyncio
    async def test_service_failure_returns_error(self):
        generate = AsyncMock(side_effect=RuntimeError("LLM unavailable"))
        with (
            patch("app.agent.tools.report_gen.generate_report", generate),
            patch(
                "app.agent.tools.config_store.tool_model_override",
                AsyncMock(return_value=None),
            ),
        ):
            result = await ReportGenTool().execute(topic="T")
        assert result.success is False
        assert result.output == "Report generation failed: LLM unavailable"
        assert result.tool_call.status == "error"
        assert result.tool_call.error == "LLM unavailable"


# ── Config helpers + validator + misc ───────────────────────────────


class TestReportGenConfigHelpers:
    def test_default_format_from_config(self):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"default_format": "docx"}},
        ):
            assert _configured_default_format() == "docx"

    @pytest.mark.parametrize("value", ["txt", "PPTX", "word", 42, None])
    def test_invalid_default_format_falls_back(self, value):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"default_format": value}},
        ):
            assert _configured_default_format() == DEFAULT_FORMAT

    def test_missing_config_falls_back(self):
        for cfg in (None, {}, {"custom": None}):
            with patch("app.agent.tools.config_store.get_tool_config", return_value=cfg):
                assert _configured_default_format() == DEFAULT_FORMAT


class TestReportGenCustomValidator:
    def test_rejects_non_object(self):
        with pytest.raises(ValueError, match="must be an object"):
            _validate_report_custom("pdf")

    @pytest.mark.parametrize("value", ["txt", "word", "PDF", 1])
    def test_rejects_bad_default_format(self, value):
        with pytest.raises(ValueError, match="'default_format' must be 'pdf' or 'docx'"):
            _validate_report_custom({"default_format": value})

    @pytest.mark.parametrize("value", ["pdf", "docx", None])
    def test_accepts_valid_default_format(self, value):
        cleaned = _validate_report_custom({"default_format": value})
        assert cleaned["default_format"] == value

    def test_empty_custom_accepted(self):
        assert _validate_report_custom({}) == {}


class TestReportGenLogHelper:
    def test_log_formats_args(self, capsys):
        _log("start %s fmt=%s", "topic", "pdf")
        assert "[report_gen_tool] start topic fmt=pdf" in capsys.readouterr().out

    def test_log_survives_bad_format_string(self, capsys):
        _log("pct=%d%%", "lots")
        assert "lots" in capsys.readouterr().out
