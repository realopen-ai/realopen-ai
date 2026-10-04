"""Tests for the Vision agent tool (app/agent/tools/vision.py).

Scope:
- Tool schema/defaults (get_parameters, required params, registry entry).
- execute() happy path against a FAKE Ollama /api/chat response, model
  resolution priority (explicit kwarg → config store override → profile
  role), data-URI prefix stripping.
- execute() error branches: non-200 responses (raise_for_status), invalid
  JSON payloads, connection errors, missing image argument.
- Config helpers: _configured_timeout_s (clamping + bad values),
  _configured_base_url, and the _validate_vision_custom validator.

Mocks:
- httpx.AsyncClient is replaced with an in-memory fake (NO network —
  Ollama at OLLAMA_BASE_URL is never contacted).
- app.agent.tools.config_store.resolve_tool_model / get_tool_config are
  patched where a persisted tool configuration is needed.
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Importing the tools package registers every tool in the global registry.
import app.agent.tools  # noqa: E402,F401
from app.agent.base import ToolType, get_tool_registry  # noqa: E402
from app.agent.tools.config_base import config_registry  # noqa: E402
from app.agent.tools.vision import (  # noqa: E402
    DEFAULT_TIMEOUT_S,
    VISION_CONFIG,
    VisionTool,
    _configured_base_url,
    _configured_timeout_s,
    _validate_vision_custom,
)
from app.config import settings  # noqa: E402


# ── Fake httpx layer (never any network) ────────────────────────────


class FakeResponse:
    """Stand-in for httpx.Response with canned behaviour."""

    def __init__(self, payload=None, status_error=None, json_error=None):
        self._payload = payload
        self._status_error = status_error
        self._json_error = json_error

    def raise_for_status(self):
        if self._status_error:
            raise self._status_error

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._payload


def _fake_httpx(response=None, post_error=None):
    """Build an httpx.AsyncClient substitute that records posts."""
    recorded = {"client_kwargs": None, "posts": []}

    class _Client:
        def __init__(self, **kwargs):
            recorded["client_kwargs"] = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json=None):
            recorded["posts"].append({"url": url, "json": json})
            if post_error:
                raise post_error
            return response

    return _Client, recorded


def _http_error() -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "http://ollama/api/chat")
    response = httpx.Response(500, request=request)
    return httpx.HTTPStatusError("server error", request=request, response=response)


# ── Schema / defaults ───────────────────────────────────────────────


class TestVisionSchema:
    def test_parameters_and_required_params(self):
        tool = VisionTool()
        params = tool.get_parameters()
        assert set(params) == {"prompt"}
        assert params["prompt"]["type"] == "string"
        assert tool.get_required_params() == []

    def test_tool_metadata_and_registry(self):
        tool = VisionTool()
        assert tool.name == "use_vision"
        assert tool.display_name == "Vision"
        assert tool.tool_type is ToolType.VISION
        registry = get_tool_registry()
        assert registry.has_tool("use_vision")

    def test_default_timeout_constant(self):
        assert DEFAULT_TIMEOUT_S == 120

    def test_config_definition_registered(self):
        definition = config_registry.get("use_vision")
        assert definition is VISION_CONFIG
        assert definition.model_fallback_role == "default_vision"
        assert definition.custom_defaults == {"timeout_s": 120, "base_url": None}
        schema = definition.schema_dict()
        assert schema and schema[0]["key"] == "inference"
        keys = {f["key"] for f in schema[0]["fields"]}
        assert keys == {"timeout_s", "base_url"}


# ── execute() ───────────────────────────────────────────────────────


class TestVisionExecute:
    @pytest.mark.asyncio
    async def test_happy_path_returns_description(self):
        client, recorded = _fake_httpx(
            FakeResponse(payload={"message": {"content": "A fluffy cat on a sofa"}})
        )
        with patch("app.agent.tools.vision.httpx.AsyncClient", client):
            result = await VisionTool().execute(
                image_base64="aW1hZ2VkYXRh", prompt="What is in it?", model="llava:7b"
            )

        assert result.success is True
        assert result.output == "Image analysis: A fluffy cat on a sofa"
        call = result.tool_call
        assert call.status == "completed"
        assert call.image_description == "A fluffy cat on a sofa"
        assert call.error is None
        assert call.completed_at is not None

        # Exactly one POST to the configured Ollama server.
        assert len(recorded["posts"]) == 1
        post = recorded["posts"][0]
        assert post["url"] == f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/chat"
        assert post["json"] == {
            "model": "llava:7b",
            "messages": [
                {
                    "role": "user",
                    "content": "What is in it?",
                    "images": ["aW1hZ2VkYXRh"],
                }
            ],
            "stream": False,
        }

    @pytest.mark.asyncio
    async def test_data_uri_prefix_is_stripped(self):
        client, recorded = _fake_httpx(
            FakeResponse(payload={"message": {"content": "desc"}})
        )
        with patch("app.agent.tools.vision.httpx.AsyncClient", client):
            result = await VisionTool().execute(
                image_base64="data:image/png;base64,Zm9v", model="llava:7b"
            )
        assert result.success is True
        sent = recorded["posts"][0]["json"]["messages"][0]["images"]
        assert sent == ["Zm9v"]

    @pytest.mark.asyncio
    async def test_default_prompt_used_when_omitted(self):
        client, recorded = _fake_httpx(
            FakeResponse(payload={"message": {"content": "d"}})
        )
        with patch("app.agent.tools.vision.httpx.AsyncClient", client):
            await VisionTool().execute(image_base64="zzz", model="llava:7b")
        message = recorded["posts"][0]["json"]["messages"][0]
        assert message["content"] == "Describe this image in detail."

    @pytest.mark.asyncio
    async def test_model_resolved_from_config_when_kwarg_missing(self):
        client, recorded = _fake_httpx(
            FakeResponse(payload={"message": {"content": "d"}})
        )
        resolver = AsyncMock(return_value="resolved-vision-model")
        with (
            patch("app.agent.tools.vision.httpx.AsyncClient", client),
            patch("app.agent.tools.config_store.resolve_tool_model", resolver),
        ):
            result = await VisionTool().execute(image_base64="zzz")

        assert result.success is True
        resolver.assert_awaited_once_with("use_vision", fallback_role="default_vision")
        assert recorded["posts"][0]["json"]["model"] == "resolved-vision-model"

    @pytest.mark.asyncio
    async def test_missing_image_argument_raises_type_error(self):
        with pytest.raises(TypeError):
            await VisionTool().execute(prompt="no image")

    @pytest.mark.asyncio
    async def test_non_200_response_returns_failure(self):
        client, _ = _fake_httpx(FakeResponse(status_error=_http_error()))
        with patch("app.agent.tools.vision.httpx.AsyncClient", client):
            result = await VisionTool().execute(
                image_base64="zzz", model="llava:7b", prompt="p"
            )
        assert result.success is False
        assert result.output.startswith("Vision analysis failed:")
        assert result.tool_call.status == "error"
        assert "server error" in result.tool_call.error

    @pytest.mark.asyncio
    async def test_invalid_json_payload_returns_failure(self):
        client, _ = _fake_httpx(FakeResponse(json_error=ValueError("bad json")))
        with patch("app.agent.tools.vision.httpx.AsyncClient", client):
            result = await VisionTool().execute(image_base64="zzz", model="m")
        assert result.success is False
        assert "bad json" in result.output

    @pytest.mark.asyncio
    async def test_connection_error_returns_failure(self):
        client, _ = _fake_httpx(post_error=httpx.ConnectError("connection refused"))
        with patch("app.agent.tools.vision.httpx.AsyncClient", client):
            result = await VisionTool().execute(image_base64="zzz", model="m")
        assert result.success is False
        assert "connection refused" in result.output
        assert result.tool_call.status == "error"

    @pytest.mark.asyncio
    async def test_missing_content_falls_back_to_placeholder(self):
        client, _ = _fake_httpx(FakeResponse(payload={"message": {}}))
        with patch("app.agent.tools.vision.httpx.AsyncClient", client):
            result = await VisionTool().execute(image_base64="zzz", model="m")
        assert result.success is True
        assert result.output == "Image analysis: No description available"

    @pytest.mark.asyncio
    async def test_configured_timeout_and_base_url_used(self):
        client, recorded = _fake_httpx(
            FakeResponse(payload={"message": {"content": "d"}})
        )
        cfg = {"custom": {"timeout_s": 90, "base_url": "http://vision-box:11434"}}
        with (
            patch("app.agent.tools.vision.httpx.AsyncClient", client),
            patch("app.agent.tools.config_store.get_tool_config", return_value=cfg),
        ):
            result = await VisionTool().execute(image_base64="zzz", model="m")
        assert result.success is True
        assert recorded["client_kwargs"] == {"timeout": 90.0}
        assert recorded["posts"][0]["url"] == "http://vision-box:11434/api/chat"


# ── Config helpers + validator ──────────────────────────────────────


class TestVisionConfigHelpers:
    def test_timeout_from_config(self):
        cfg = {"custom": {"timeout_s": 45}}
        with patch(
            "app.agent.tools.config_store.get_tool_config", return_value=cfg
        ):
            assert _configured_timeout_s() == 45

    def test_timeout_clamped_to_bounds(self):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"timeout_s": 5_000}},
        ):
            assert _configured_timeout_s() == 600
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"timeout_s": 1}},
        ):
            assert _configured_timeout_s() == 5

    def test_timeout_invalid_values_fall_back_to_default(self):
        for bad in ("abc", None, [30]):
            with patch(
                "app.agent.tools.config_store.get_tool_config",
                return_value={"custom": {"timeout_s": bad}},
            ):
                assert _configured_timeout_s() == DEFAULT_TIMEOUT_S

    def test_timeout_when_config_missing(self):
        for cfg in (None, {}, {"custom": None}):
            with patch(
                "app.agent.tools.config_store.get_tool_config", return_value=cfg
            ):
                assert _configured_timeout_s() == DEFAULT_TIMEOUT_S

    def test_base_url_from_config(self):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"base_url": "  http://ollama.example:11434  "}},
        ):
            assert _configured_base_url() == "http://ollama.example:11434"

    def test_base_url_none_when_unset_or_blank(self):
        for custom in (None, {}, {"base_url": None}, {"base_url": "   "}):
            with patch(
                "app.agent.tools.config_store.get_tool_config",
                return_value={"custom": custom} if custom is not None else None,
            ):
                assert _configured_base_url() is None

    def test_base_url_non_string_ignored(self):
        with patch(
            "app.agent.tools.config_store.get_tool_config",
            return_value={"custom": {"base_url": 123}},
        ):
            assert _configured_base_url() is None


class TestVisionCustomValidator:
    def test_rejects_non_object(self):
        with pytest.raises(ValueError, match="must be an object"):
            _validate_vision_custom(["not", "a", "dict"])

    def test_rejects_non_numeric_timeout(self):
        with pytest.raises(ValueError, match="'timeout_s' must be a number"):
            _validate_vision_custom({"timeout_s": "soon"})

    @pytest.mark.parametrize("value", [0, -5, 601, 10_000])
    def test_rejects_out_of_range_timeout(self, value):
        with pytest.raises(ValueError, match="between 1 and 600"):
            _validate_vision_custom({"timeout_s": value})

    def test_numeric_string_timeout_coerced_to_int(self):
        cleaned = _validate_vision_custom({"timeout_s": "90"})
        assert cleaned["timeout_s"] == 90

    def test_base_url_none_allowed(self):
        cleaned = _validate_vision_custom({"base_url": None})
        assert cleaned["base_url"] is None

    @pytest.mark.parametrize("base", ["", "   ", "ftp://host", "localhost:11434"])
    def test_rejects_invalid_base_url(self, base):
        with pytest.raises(ValueError, match="'base_url'"):
            _validate_vision_custom({"base_url": base})

    def test_accepts_http_base_url(self):
        cleaned = _validate_vision_custom({"base_url": "http://localhost:11434"})
        assert cleaned["base_url"] == "http://localhost:11434"

    def test_untouched_keys_pass_through(self):
        cleaned = _validate_vision_custom({"other": 1})
        assert cleaned == {"other": 1}
