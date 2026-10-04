"""Tests for the Presentation Generation agent tool (app/agent/tools/pptx_gen.py).

Scope:
- Tool schema (parameters, required params, aliases) and registry entry.
- get_dynamic_description(): DB-driven template list with descriptions
  and the hardcoded fallback when the DB has none.
- execute(): topic validation, template resolution against the runtime
  DB template list (requested template → configured default → service
  default), delegation to generate_presentation with model override and
  max_slides, deliverable metadata in gen_results, service failures.
- Config helpers: _configured_max_slides (clamping), _configured_default_template,
  and the _validate_pptx_custom validator.

Mocks:
- app.services.pptx_gen.generate_presentation, _get_available_templates_from_db
  and _get_templates_with_descriptions are patched in the tool's namespace —
  no LLM call, no DB query, no file generation happens here (the real
  rendering pipeline is covered by tests/test_pptx_gen_service.py).
- config_store.tool_model_override / get_tool_config are patched where a
  persisted tool configuration is needed.
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
from app.agent.tools.pptx_gen import (  # noqa: E402
    PPTX_GEN_CONFIG,
    PptxGenTool,
    _configured_default_template,
    _configured_max_slides,
    _validate_pptx_custom,
)
from app.services.pptx_gen import (  # noqa: E402
    AVAILABLE_TEMPLATES,
    DEFAULT_TEMPLATE,
    MAX_SLIDES,
)


def _service_result(**overrides):
    result = {
        "filename": "my_topic.pptx",
        "file_path": "reports/rid.pptx",
        "download_url": "/api/reports/rid/download",
        "report_id": "rid",
        "created_at": 1_700_000_000,
        "slide_count": 4,
        "template": "modern",
    }
    result.update(overrides)
    return result


def _patched_service(result=None, error=None):
    generate = AsyncMock(return_value=result) if error is None else AsyncMock(
        side_effect=error
    )
    return patch("app.agent.tools.pptx_gen.generate_presentation", generate), generate


def _patched_db_templates(slugs):
    return patch(
        "app.agent.tools.pptx_gen._get_available_templates_from_db",
        AsyncMock(return_value=slugs),
    )


def _patched_model_override(model=None):
    return patch(
        "app.agent.tools.config_store.tool_model_override",
        AsyncMock(return_value=model),
    )


# ── Schema ──────────────────────────────────────────────────────────


class TestPptxGenSchema:
    def test_parameters_and_required_params(self):
        tool = PptxGenTool()
        params = tool.get_parameters()
        assert set(params) == {"topic", "outline", "template"}
        assert DEFAULT_TEMPLATE in params["template"]["description"]
        assert tool.get_required_params() == ["topic"]

    def test_aliases(self):
        assert PptxGenTool.param_aliases == {
            "topic": "topic",
            "outline": "outline",
            "template": "template",
        }

    def test_tool_metadata_and_registry(self):
        tool = PptxGenTool()
        assert tool.name == "use_pptx_gen"
        assert tool.tool_type is ToolType.IMAGE_GEN
        assert get_tool_registry().has_tool("use_pptx_gen")

    def test_config_definition_registered(self):
        definition = config_registry.get("use_pptx_gen")
        assert definition is PPTX_GEN_CONFIG
        assert definition.model_task_slot == "report"
        assert definition.custom_defaults == {
            "max_slides": MAX_SLIDES,
            "default_template": DEFAULT_TEMPLATE,
        }


# ── get_dynamic_description() ───────────────────────────────────────


class TestDynamicDescription:
    @pytest.mark.asyncio
    async def test_lists_db_templates_with_descriptions(self):
        templates = [("corporate", "Navy blue professional"), ("brand-x", "")]
        with patch(
            "app.agent.tools.pptx_gen._get_templates_with_descriptions",
            AsyncMock(return_value=templates),
        ):
            description = await PptxGenTool().get_dynamic_description()

        assert description.startswith(PptxGenTool.description)
        assert "Available templates:" in description
        assert "  - corporate: Navy blue professional" in description
        # Empty description → bare slug without the colon separator.
        assert description.endswith("  - brand-x")

    @pytest.mark.asyncio
    async def test_falls_back_to_builtin_templates_when_db_empty(self):
        with patch(
            "app.agent.tools.pptx_gen._get_templates_with_descriptions",
            AsyncMock(return_value=[]),
        ):
            description = await PptxGenTool().get_dynamic_description()
        for slug in AVAILABLE_TEMPLATES:
            assert f"  - {slug}" in description
        assert "Navy blue professional" not in description


# ── execute() ───────────────────────────────────────────────────────


class TestPptxGenExecute:
    @pytest.mark.asyncio
    async def test_happy_path_with_valid_template(self):
        db_slugs = ["corporate", "modern", "elegant", "brand-x"]
        generate_patch, generate = _patched_service(_service_result())
        with (
            generate_patch,
            _patched_db_templates(db_slugs),
            _patched_model_override(None),
        ):
            result = await PptxGenTool().execute(
                topic="  Quarterly review  ", outline="A; B", template="brand-x"
            )

        assert result.success is True
        generate.assert_awaited_once_with(
            topic="Quarterly review",
            outline="A; B",
            template="brand-x",
            model=None,
            max_slides=MAX_SLIDES,
        )

        gen = result.tool_call.gen_results
        assert gen and gen[0]["type"] == "presentation"
        assert gen[0]["format"] == "pptx"
        assert gen[0]["filename"] == "my_topic.pptx"
        assert gen[0]["slide_count"] == 4
        assert gen[0]["template"] == "modern"
        assert result.tool_call.status == "completed"
        assert "my_topic.pptx" in result.output
        assert "4 slides" in result.output
        assert "ready for download" in result.output

    @pytest.mark.asyncio
    async def test_unknown_template_falls_back_to_configured_default(self):
        cfg = {"custom": {"default_template": "modern", "max_slides": 25}}
        generate_patch, generate = _patched_service(_service_result())
        with (
            generate_patch,
            _patched_db_templates(["corporate", "modern"]),
            _patched_model_override(None),
            patch("app.agent.tools.config_store.get_tool_config", return_value=cfg),
        ):
            await PptxGenTool().execute(topic="T", template="does-not-exist")
        assert generate.await_args.kwargs["template"] == "modern"

    @pytest.mark.asyncio
    async def test_falls_back_to_service_default_when_config_invalid(self):
        # Configured default is not in the DB list → service DEFAULT_TEMPLATE.
        cfg = {"custom": {"default_template": "not-in-db", "max_slides": 25}}
        generate_patch, generate = _patched_service(_service_result())
        with (
            generate_patch,
            _patched_db_templates(["corporate", "modern"]),
            _patched_model_override(None),
            patch("app.agent.tools.config_store.get_tool_config", return_value=cfg),
        ):
            await PptxGenTool().execute(topic="T", template="nope")
        assert generate.await_args.kwargs["template"] == DEFAULT_TEMPLATE

    @pytest.mark.asyncio
    async def test_requested_template_case_sensitive(self):
        # "Modern" (capitalized) is not in the DB slug list → falls back.
        generate_patch, generate = _patched_service(_service_result())
        with (
            generate_patch,
            _patched_db_templates(["corporate", "modern"]),
            _patched_model_override(None),
        ):
            await PptxGenTool().execute(topic="T", template="Modern")
        assert generate.await_args.kwargs["template"] == DEFAULT_TEMPLATE

    @pytest.mark.asyncio
    async def test_model_override_and_max_slides_forwarded(self):
        cfg = {"custom": {"max_slides": 5, "default_template": "corporate"}}
        generate_patch, generate = _patched_service(_service_result())
        with (
            generate_patch,
            _patched_db_templates(["corporate"]),
            _patched_model_override("deck-model"),
            patch("app.agent.tools.config_store.get_tool_config", return_value=cfg),
        ):
            await PptxGenTool().execute(topic="T")
        assert generate.await_args.kwargs["model"] == "deck-model"
        assert generate.await_args.kwargs["max_slides"] == 5

    @pytest.mark.asyncio
    async def test_empty_topic_rejected(self):
        generate_patch, generate = _patched_service(_service_result())
        with generate_patch:
            result = await PptxGenTool().execute(topic="   ")
        assert result.success is False
        assert result.output == "Topic cannot be empty."
        assert result.tool_call.status == "error"
        assert result.tool_call.error == "Topic cannot be empty"
        generate.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_service_failure_returns_error(self):
        generate_patch, _ = _patched_service(error=RuntimeError("no LLM configured"))
        with (
            generate_patch,
            _patched_db_templates(["corporate"]),
            _patched_model_override(None),
        ):
            result = await PptxGenTool().execute(topic="T")
        assert result.success is False
        assert result.output == "Presentation generation failed: no LLM configured"
        assert result.tool_call.status == "error"

    @pytest.mark.asyncio
    async def test_result_without_optional_fields_uses_defaults(self):
        minimal = {
            "filename": "deck.pptx",
            "file_path": "reports/r.pptx",
            "download_url": "/api/reports/r/download",
            "report_id": "r",
            "created_at": 1,
        }
        generate_patch, _ = _patched_service(minimal)
        with (
            generate_patch,
            _patched_db_templates(["corporate"]),
            _patched_model_override(None),
        ):
            result = await PptxGenTool().execute(topic="T", template="corporate")
        assert result.success is True
        gen = result.tool_call.gen_results[0]
        assert gen["slide_count"] == 0
        assert gen["template"] == "corporate"
        assert "0 slides" in result.output


# ── Config helpers + validator ──────────────────────────────────────


class TestPptxGenConfigHelpers:
    def test_max_slides_from_config(self):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"max_slides": 12}},
        ):
            assert _configured_max_slides() == 12

    def test_max_slides_clamped_to_bounds(self):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"max_slides": 500}},
        ):
            assert _configured_max_slides() == 100
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"max_slides": 1}},
        ):
            assert _configured_max_slides() == 3

    def test_max_slides_invalid_or_missing_falls_back(self):
        for cfg in (
            None,
            {},
            {"custom": None},
            {"custom": {}},
            {"custom": {"max_slides": "many"}},
        ):
            with patch(
                "app.agent.tools.config_store.get_tool_config", return_value=cfg
            ):
                assert _configured_max_slides() == MAX_SLIDES

    def test_default_template_from_config(self):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"default_template": "elegant"}},
        ):
            assert _configured_default_template() == "elegant"

    @pytest.mark.parametrize("value", [None, "", "   ", 42])
    def test_default_template_invalid_falls_back(self, value):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"default_template": value}},
        ):
            assert _configured_default_template() == DEFAULT_TEMPLATE

    def test_default_template_missing_config(self):
        for cfg in (None, {}, {"custom": None}):
            with patch(
                "app.agent.tools.config_store.get_tool_config", return_value=cfg
            ):
                assert _configured_default_template() == DEFAULT_TEMPLATE


class TestPptxGenCustomValidator:
    def test_rejects_non_object(self):
        with pytest.raises(ValueError, match="must be an object"):
            _validate_pptx_custom("deck")

    def test_rejects_non_numeric_max_slides(self):
        with pytest.raises(ValueError, match="'max_slides' must be a number"):
            _validate_pptx_custom({"max_slides": "lots"})

    @pytest.mark.parametrize("value", [2, 0, 101, 1000])
    def test_rejects_out_of_range_max_slides(self, value):
        with pytest.raises(ValueError, match="between 3 and 100"):
            _validate_pptx_custom({"max_slides": value})

    @pytest.mark.parametrize("value", [3, 25, 100, "10"])
    def test_accepts_valid_max_slides(self, value):
        cleaned = _validate_pptx_custom({"max_slides": value})
        assert cleaned["max_slides"] == int(value)

    def test_default_template_none_allowed(self):
        cleaned = _validate_pptx_custom({"default_template": None})
        assert cleaned["default_template"] is None

    @pytest.mark.parametrize("value", ["", "   ", 7, ["corporate"]])
    def test_rejects_invalid_default_template(self, value):
        with pytest.raises(ValueError, match="'default_template' must be a template slug"):
            _validate_pptx_custom({"default_template": value})

    def test_accepts_slug_default_template(self):
        cleaned = _validate_pptx_custom({"default_template": "corporate"})
        assert cleaned["default_template"] == "corporate"

    def test_empty_custom_accepted(self):
        assert _validate_pptx_custom({}) == {}
