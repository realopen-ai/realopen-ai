"""Tests for the Image Generation agent tool (app/agent/tools/image_gen.py).

Scope:
- Tool schema (parameters, required params) and registry entry.
- execute(): model resolution via the config store, delegation to the
  image generation service, base64 deliverable in gen_results (verified
  by decoding it back into a REAL PNG file opened with PIL), and the
  error branch when the service fails.
- The _validate_image_gen_custom provider-matrix validator (every error
  class + accepted shapes).

Mocks:
- app.services.image_gen.generate is patched through the tool module's
  service reference — no Ollama call, no model availability check.
- app.agent.tools.config_store.resolve_tool_model is patched.
- PIL is used for the fake image fixture and to verify the round trip
  into a tmp file.
"""

import base64
import io
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
from app.agent.tools.image_gen import (  # noqa: E402
    IMAGE_GEN_CONFIG,
    ImageGenTool,
    _validate_image_gen_custom,
)


@pytest.fixture
def png_bytes():
    """A real 1×1 blue PNG built with PIL."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), (0, 0, 255)).save(buffer, format="PNG")
    return buffer.getvalue()


# ── Schema ──────────────────────────────────────────────────────────


class TestImageGenSchema:
    def test_parameters_and_required_params(self):
        tool = ImageGenTool()
        params = tool.get_parameters()
        assert set(params) == {"prompt"}
        assert params["prompt"]["type"] == "string"
        assert tool.get_required_params() == ["prompt"]

    def test_tool_metadata_and_registry(self):
        tool = ImageGenTool()
        assert tool.name == "use_image_gen"
        assert tool.display_name == "Image Generation"
        assert tool.tool_type is ToolType.IMAGE_GEN
        assert get_tool_registry().has_tool("use_image_gen")

    def test_config_definition_registered(self):
        definition = config_registry.get("use_image_gen")
        assert definition is IMAGE_GEN_CONFIG
        assert definition.model_task_slot == "image"
        defaults = definition.custom_defaults["providers"]["ollama"]
        assert defaults["enabled"] is True
        assert defaults["timeout_s"] == 600
        assert defaults["base_url"] is None


# ── execute() ───────────────────────────────────────────────────────


class TestImageGenExecute:
    @pytest.mark.asyncio
    async def test_happy_path_returns_base64_deliverable(self, png_bytes):
        generate = AsyncMock(return_value={"image": png_bytes, "provider": "Ollama"})
        with (
            patch("app.agent.tools.image_gen.image_gen_service.generate", generate),
            patch(
                "app.agent.tools.config_store.resolve_tool_model",
                AsyncMock(return_value="img-model"),
            ),
        ):
            result = await ImageGenTool().execute(prompt="a blue dot")

        assert result.success is True
        assert result.output == "Image generated for: a blue dot"

        generate.assert_awaited_once_with("a blue dot", model="img-model")

        gen = result.tool_call.gen_results
        assert gen and gen[0]["type"] == "image"
        assert gen[0]["data"] == base64.b64encode(png_bytes).decode("utf-8")
        call = result.tool_call
        assert call.status == "completed"
        assert call.image_description == "a blue dot"
        assert call.error is None

    @pytest.mark.asyncio
    async def test_generated_image_decodes_to_valid_png(self, png_bytes, tmp_path):
        """The deliverable's base64 payload must be a real openable image."""
        from PIL import Image

        generate = AsyncMock(return_value={"image": png_bytes, "provider": "Ollama"})
        with (
            patch("app.agent.tools.image_gen.image_gen_service.generate", generate),
            patch(
                "app.agent.tools.config_store.resolve_tool_model",
                AsyncMock(return_value="m"),
            ),
        ):
            result = await ImageGenTool().execute(prompt="p")

        data = base64.b64decode(result.tool_call.gen_results[0]["data"])
        saved = tmp_path / "generated.png"
        saved.write_bytes(data)

        with Image.open(saved) as image:
            image.load()
            assert image.format == "PNG"
            assert image.size == (1, 1)

    @pytest.mark.asyncio
    async def test_model_resolution_args(self):
        generate = AsyncMock(return_value={"image": b"x", "provider": "Ollama"})
        resolver = AsyncMock(return_value="resolved-img-model")
        with (
            patch("app.agent.tools.image_gen.image_gen_service.generate", generate),
            patch("app.agent.tools.config_store.resolve_tool_model", resolver),
        ):
            await ImageGenTool().execute(prompt="p")

        resolver.assert_awaited_once_with("use_image_gen", task="image")
        assert generate.await_args.kwargs["model"] == "resolved-img-model"

    @pytest.mark.asyncio
    async def test_service_failure_returns_error(self):
        generate = AsyncMock(side_effect=RuntimeError("No provider enabled"))
        with (
            patch("app.agent.tools.image_gen.image_gen_service.generate", generate),
            patch(
                "app.agent.tools.config_store.resolve_tool_model",
                AsyncMock(return_value="m"),
            ),
        ):
            result = await ImageGenTool().execute(prompt="p")

        assert result.success is False
        assert result.output == "Image generation failed: No provider enabled"
        call = result.tool_call
        assert call.status == "error"
        assert call.error == "No provider enabled"
        assert call.gen_results is None

    @pytest.mark.asyncio
    async def test_missing_prompt_raises_type_error(self):
        with pytest.raises(TypeError):
            await ImageGenTool().execute()


# ── Provider matrix validator ───────────────────────────────────────


class TestImageGenCustomValidator:
    def test_rejects_non_object(self):
        with pytest.raises(ValueError, match="'custom' must be an object"):
            _validate_image_gen_custom("nope")

    def test_rejects_non_object_providers(self):
        with pytest.raises(ValueError, match="'custom.providers' must be an object"):
            _validate_image_gen_custom({"providers": ["ollama"]})

    def test_rejects_unknown_provider(self):
        with pytest.raises(ValueError, match="Unknown image generation provider"):
            _validate_image_gen_custom({"providers": {"comfyui": {}}})

    def test_rejects_non_object_provider_settings(self):
        with pytest.raises(ValueError, match="settings must be an object"):
            _validate_image_gen_custom({"providers": {"ollama": "http://x"}})

    def test_rejects_non_bool_enabled(self):
        with pytest.raises(ValueError, match="'enabled' must be a boolean"):
            _validate_image_gen_custom(
                {"providers": {"ollama": {"enabled": "yes"}}}
            )

    def test_rejects_non_numeric_timeout(self):
        with pytest.raises(ValueError, match="'timeout_s' must be a number"):
            _validate_image_gen_custom(
                {"providers": {"ollama": {"timeout_s": "soon"}}}
            )

    @pytest.mark.parametrize("value", [0, -1, 7201])
    def test_rejects_out_of_range_timeout(self, value):
        with pytest.raises(ValueError, match="between 1 and 7200"):
            _validate_image_gen_custom(
                {"providers": {"ollama": {"timeout_s": value}}}
            )

    @pytest.mark.parametrize("value", [1, 600, 7200, "90"])
    def test_accepts_valid_timeout(self, value):
        cleaned = _validate_image_gen_custom(
            {"providers": {"ollama": {"timeout_s": value}}}
        )
        assert "timeout_s" in cleaned["providers"]["ollama"]

    def test_base_url_none_allowed(self):
        cleaned = _validate_image_gen_custom(
            {"providers": {"ollama": {"base_url": None}}}
        )
        assert cleaned["providers"]["ollama"]["base_url"] is None

    @pytest.mark.parametrize("base", ["", "   ", "ftp://host", "just-text"])
    def test_rejects_invalid_base_url(self, base):
        with pytest.raises(ValueError, match="'base_url' must"):
            _validate_image_gen_custom(
                {"providers": {"ollama": {"base_url": base}}}
            )

    @pytest.mark.parametrize("base", ["http://localhost:11434", "https://gpu.box"])
    def test_accepts_valid_base_url(self, base):
        cleaned = _validate_image_gen_custom(
            {"providers": {"ollama": {"base_url": base}}}
        )
        assert cleaned["providers"]["ollama"]["base_url"] == base

    def test_custom_without_providers_passes_through(self):
        cleaned = _validate_image_gen_custom({"anything": 1})
        assert cleaned == {"anything": 1}

    def test_full_valid_matrix(self):
        matrix = {
            "providers": {
                "ollama": {
                    "enabled": True,
                    "base_url": "http://ollama:11434",
                    "timeout_s": 120,
                }
            }
        }
        assert _validate_image_gen_custom(matrix) == matrix
