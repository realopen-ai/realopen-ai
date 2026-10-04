"""Tests for the image generation service (app/services/image_gen.py).

Scope:
- generate_ollama(): the Ollama /api/generate provider — base64 "images"
  list decoding, raw-bytes passthrough, the x/flux2-klein single "image"
  field variant, non-image-model payloads, HTTP errors, request shape
  (URL building, base_url override, timeout forwarding).
- check_model_available(): delegation to ModuleConfig.check_model_downloaded.
- generate(): the provider-matrix dispatch — enabled provider with model
  resolution, base_url/timeout_s pass-through, disabled providers, no
  model configured, model not installed, and reading the persisted
  provider matrix via _providers_config().

Mocks:
- httpx.AsyncClient is replaced with an in-memory fake (NO network —
  Ollama at OLLAMA_BASE_URL is never contacted).
- check_model_available / generate_ollama are patched at the service
  module level for the dispatch tests.
- app.config.ModuleConfig.check_model_downloaded and
  config_store.get_tool_config are patched where needed.

The fake PNG payload is a REAL 1×1 PNG produced with PIL, so the base64
decoding round-trip is verified against actual image bytes.
"""

import base64
import io
import sys
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402
from app.services import image_gen  # noqa: E402


@pytest.fixture
def png_bytes():
    """A real 1×1 red PNG built with PIL."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (1, 1), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


# ── Fake httpx layer (never any network) ────────────────────────────


class FakeResponse:
    def __init__(self, payload=None, status_error=None):
        self._payload = payload
        self._status_error = status_error

    def raise_for_status(self):
        if self._status_error:
            raise self._status_error

    def json(self):
        return self._payload


def _fake_httpx(payload=None, status_error=None, post_error=None):
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
            return FakeResponse(payload, status_error)

    return _Client, recorded


def _http_error():
    request = httpx.Request("POST", "http://ollama/api/generate")
    response = httpx.Response(500, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


# ── generate_ollama (the Ollama provider) ───────────────────────────


class TestGenerateOllama:
    @pytest.mark.asyncio
    async def test_images_field_decoded_from_base64(self, png_bytes):
        client, recorded = _fake_httpx(
            {"images": [base64.b64encode(png_bytes).decode("ascii")]}
        )
        with patch("app.services.image_gen.httpx.AsyncClient", client):
            result = await image_gen.generate_ollama("a red square", "img-model")

        assert result == png_bytes
        assert len(recorded["posts"]) == 1
        post = recorded["posts"][0]
        assert post["url"] == f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/generate"
        assert post["json"] == {
            "model": "img-model",
            "prompt": "a red square",
            "stream": False,
        }

    @pytest.mark.asyncio
    async def test_raw_bytes_passthrough(self, png_bytes):
        client, _ = _fake_httpx({"images": [png_bytes]})
        with patch("app.services.image_gen.httpx.AsyncClient", client):
            result = await image_gen.generate_ollama("p", "m")
        assert result == png_bytes

    @pytest.mark.asyncio
    async def test_flux_single_image_field(self, png_bytes):
        encoded = base64.b64encode(png_bytes).decode("ascii")
        client, _ = _fake_httpx({"image": encoded})
        with patch("app.services.image_gen.httpx.AsyncClient", client):
            result = await image_gen.generate_ollama("p", "x/flux2-klein")
        assert result == png_bytes

    @pytest.mark.asyncio
    async def test_flux_model_without_image_field_raises(self):
        client, _ = _fake_httpx({"response": "text only"})
        with patch("app.services.image_gen.httpx.AsyncClient", client):
            with pytest.raises(ValueError, match="did not return image data"):
                await image_gen.generate_ollama("p", "x/flux2-klein")

    @pytest.mark.asyncio
    async def test_non_image_model_raises(self):
        client, _ = _fake_httpx({"response": "just text"})
        with patch("app.services.image_gen.httpx.AsyncClient", client):
            with pytest.raises(ValueError, match="may not support image generation"):
                await image_gen.generate_ollama("p", "llama3")

    @pytest.mark.asyncio
    async def test_empty_images_list_raises(self):
        client, _ = _fake_httpx({"images": []})
        with patch("app.services.image_gen.httpx.AsyncClient", client):
            with pytest.raises(ValueError, match="did not return image data"):
                await image_gen.generate_ollama("p", "m")

    @pytest.mark.asyncio
    async def test_http_error_propagates(self):
        client, _ = _fake_httpx(status_error=_http_error())
        with patch("app.services.image_gen.httpx.AsyncClient", client):
            with pytest.raises(httpx.HTTPStatusError):
                await image_gen.generate_ollama("p", "m")

    @pytest.mark.asyncio
    async def test_base_url_override_and_timeout(self):
        client, recorded = _fake_httpx({"images": ["aGk="]})
        with patch("app.services.image_gen.httpx.AsyncClient", client):
            await image_gen.generate_ollama(
                "p",
                "m",
                base_url="http://gpu-box:11434/",
                timeout_s=42.0,
            )
        assert recorded["client_kwargs"] == {"timeout": 42.0}
        assert recorded["posts"][0]["url"] == "http://gpu-box:11434/api/generate"


# ── check_model_available ───────────────────────────────────────────


class TestCheckModelAvailable:
    @pytest.mark.asyncio
    async def test_delegates_to_module_config(self):
        check = AsyncMock(return_value=True)
        with patch("app.config.ModuleConfig.check_model_downloaded", check):
            assert await image_gen.check_model_available("x/flux2-klein") is True
        check.assert_awaited_once_with("x/flux2-klein")

    @pytest.mark.asyncio
    async def test_propagates_false(self):
        with patch(
            "app.config.ModuleConfig.check_model_downloaded",
            AsyncMock(return_value=False),
        ):
            assert await image_gen.check_model_available("missing") is False


class SettingsStub:
    """Minimal stand-in for the settings object (pydantic models reject
    direct attribute patching, so we swap the whole reference)."""

    OLLAMA_BASE_URL = settings.OLLAMA_BASE_URL

    def __init__(self, resolved_model=""):
        self._resolved = resolved_model

    def resolve_model(self, role):
        return self._resolved


# ── Provider matrix dispatch ────────────────────────────────────────


class TestGenerateDispatch:
    @pytest.mark.asyncio
    async def test_enabled_ollama_provider(self, png_bytes):
        generate_ollama = AsyncMock(return_value=png_bytes)
        with (
            patch("app.services.image_gen.generate_ollama", generate_ollama),
            patch(
                "app.services.image_gen.check_model_available",
                AsyncMock(return_value=True),
            ),
        ):
            outcome = await image_gen.generate(
                "prompt", model="img-model", providers_cfg={"ollama": {"enabled": True}}
            )

        assert outcome == {"image": png_bytes, "provider": "Ollama"}
        generate_ollama.assert_awaited_once_with(
            "prompt",
            model="img-model",
            base_url=None,
            timeout_s=image_gen.DEFAULT_TIMEOUT_S,
        )

    @pytest.mark.asyncio
    async def test_provider_base_url_and_timeout_forwarded(self, png_bytes):
        generate_ollama = AsyncMock(return_value=png_bytes)
        providers = {
            "ollama": {
                "enabled": True,
                "base_url": "http://ollama-box:11434",
                "timeout_s": 90,
            }
        }
        with (
            patch("app.services.image_gen.generate_ollama", generate_ollama),
            patch(
                "app.services.image_gen.check_model_available",
                AsyncMock(return_value=True),
            ),
        ):
            await image_gen.generate("p", model="m", providers_cfg=providers)
        generate_ollama.assert_awaited_once_with(
            "p", model="m", base_url="http://ollama-box:11434", timeout_s=90
        )

    @pytest.mark.asyncio
    async def test_empty_provider_matrix_defaults_to_enabled(self, png_bytes):
        # No "ollama" entry → the provider defaults to enabled.
        generate_ollama = AsyncMock(return_value=png_bytes)
        with (
            patch("app.services.image_gen.generate_ollama", generate_ollama),
            patch(
                "app.services.image_gen.check_model_available",
                AsyncMock(return_value=True),
            ),
        ):
            outcome = await image_gen.generate("p", model="m", providers_cfg={})
        assert outcome["provider"] == "Ollama"
        generate_ollama.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_disabled_provider_raises(self):
        with pytest.raises(RuntimeError, match="No image generation provider enabled"):
            await image_gen.generate(
                "p", model="m", providers_cfg={"ollama": {"enabled": False}}
            )

    @pytest.mark.asyncio
    async def test_no_model_configured_raises(self):
        with patch.object(image_gen, "settings", SettingsStub(resolved_model="")):
            with pytest.raises(RuntimeError, match="No image generation model configured"):
                await image_gen.generate(
                    "p", providers_cfg={"ollama": {"enabled": True}}
                )

    @pytest.mark.asyncio
    async def test_model_not_installed_raises(self):
        with (
            patch(
                "app.services.image_gen.check_model_available",
                AsyncMock(return_value=False),
            ),
            patch.object(
                image_gen, "settings", SettingsStub(resolved_model="x/flux2-klein")
            ),
        ):
            with pytest.raises(RuntimeError, match="not installed"):
                await image_gen.generate(
                    "p", providers_cfg={"ollama": {"enabled": True}}
                )

    @pytest.mark.asyncio
    async def test_model_resolution_from_settings(self, png_bytes):
        generate_ollama = AsyncMock(return_value=png_bytes)
        with (
            patch("app.services.image_gen.generate_ollama", generate_ollama),
            patch(
                "app.services.image_gen.check_model_available",
                AsyncMock(return_value=True),
            ),
            patch.object(
                image_gen, "settings", SettingsStub(resolved_model="resolved-model")
            ),
        ):
            await image_gen.generate("p", providers_cfg={"ollama": {"enabled": True}})
        assert generate_ollama.await_args.kwargs["model"] == "resolved-model"

    @pytest.mark.asyncio
    async def test_providers_cfg_read_from_config_store_when_omitted(self, png_bytes):
        cfg = {"custom": {"providers": {"ollama": {"enabled": True}}}}
        generate_ollama = AsyncMock(return_value=png_bytes)
        with (
            patch("app.agent.tools.config_store.get_tool_config", return_value=cfg),
            patch("app.services.image_gen.generate_ollama", generate_ollama),
            patch(
                "app.services.image_gen.check_model_available",
                AsyncMock(return_value=True),
            ),
        ):
            outcome = await image_gen.generate("p", model="m")
        assert outcome["image"] == png_bytes
        generate_ollama.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_missing_config_defaults_to_enabled_ollama(self, png_bytes):
        generate_ollama = AsyncMock(return_value=png_bytes)
        with (
            patch("app.agent.tools.config_store.get_tool_config", return_value=None),
            patch("app.services.image_gen.generate_ollama", generate_ollama),
            patch(
                "app.services.image_gen.check_model_available",
                AsyncMock(return_value=True),
            ),
        ):
            outcome = await image_gen.generate("p", model="m")
        assert outcome["provider"] == "Ollama"


class TestProvidersConfigHelper:
    def test_reads_persisted_matrix(self):
        providers = {"ollama": {"enabled": False}}
        cfg = {"custom": {"providers": providers}}
        with patch("app.agent.tools.config_store.get_tool_config", return_value=cfg):
            assert image_gen._providers_config() == providers

    @pytest.mark.parametrize("cfg", [None, {}, {"custom": None}, {"custom": {}}])
    def test_missing_config_returns_empty(self, cfg):
        with patch("app.agent.tools.config_store.get_tool_config", return_value=cfg):
            assert image_gen._providers_config() == {}


class TestDefaultTimeout:
    def test_default_timeout_is_generous_for_cpu(self):
        assert image_gen.DEFAULT_TIMEOUT_S == 600.0
