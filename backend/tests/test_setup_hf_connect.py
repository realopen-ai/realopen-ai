"""Setup authentication validates credentials before writing any secret."""

import os
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from app.api import setup
from app.voice import hf_cache


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [200, 401, 403, 503])
async def test_connect_validates_before_saving(tmp_path, monkeypatch, status):
    monkeypatch.setenv("HF_HOME", str(tmp_path))
    monkeypatch.setenv("HF_TOKEN", "hf_old")
    monkeypatch.setattr(hf_cache, "_HF_HOME_OVERRIDE", None)
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.return_value = httpx.Response(status)
    monkeypatch.setattr(setup.httpx, "AsyncClient", lambda **kwargs: client)
    request = setup.HuggingFaceConnectRequest(token=" hf_new_read_token ")
    if status == 200:
        assert await setup.connect_huggingface(request) == {"connected": True}
        assert (tmp_path / "token").read_text() == "hf_new_read_token"
        assert (tmp_path / "token").stat().st_mode & 0o777 == 0o600
        assert os.environ["HF_TOKEN"] == "hf_new_read_token"
    else:
        with pytest.raises(HTTPException) as error:
            await setup.connect_huggingface(request)
        assert error.value.status_code in {400, 502}
        assert not (tmp_path / "token").exists()
        assert os.environ["HF_TOKEN"] == "hf_old"


@pytest.mark.asyncio
async def test_connect_reports_network_failure_without_secret(tmp_path, monkeypatch):
    client = AsyncMock()
    client.__aenter__.return_value = client
    client.get.side_effect = httpx.ConnectError("private diagnostic")
    monkeypatch.setattr(setup.httpx, "AsyncClient", lambda **kwargs: client)
    with pytest.raises(HTTPException) as error:
        await setup.connect_huggingface(setup.HuggingFaceConnectRequest(token="hf_secret"))
    assert error.value.status_code == 502
    assert "secret" not in error.value.detail
    assert "private diagnostic" not in error.value.detail


@pytest.mark.asyncio
async def test_public_model_listing_recovers_from_expired_login(monkeypatch):
    from types import SimpleNamespace
    import huggingface_hub
    from huggingface_hub.errors import HfHubHTTPError
    from app.services.voice_model_installer import _list_hf_files

    calls = []

    class API:
        def model_info(self, repo_id, **kwargs):
            calls.append(kwargs)
            if kwargs.get("token") is not False:
                response = httpx.Response(
                    401, request=httpx.Request("GET", "https://huggingface.co")
                )
                raise HfHubHTTPError("Expired login", response=response)
            return SimpleNamespace(siblings=[SimpleNamespace(rfilename="config.json", size=10)])

    monkeypatch.setattr(huggingface_hub, "HfApi", API)
    assert await _list_hf_files("Qwen/Qwen3-ASR-0.6B", "main") == [("config.json", 10)]
    assert calls[-1]["token"] is False


@pytest.mark.asyncio
async def test_lens_network_failure_is_actionable_and_redacts_urls(monkeypatch):
    from app.services.lens import install

    monkeypatch.setattr(install, "model_paths", lambda *args: {})

    async def download(*args):
        raise httpx.ReadTimeout("Timeout at https://example.com/file?token=secret hf_private")
        yield

    monkeypatch.setattr(install.LensCatalog, "download", download)
    events = [
        event
        async for event in install.stream_install_lens(
            install.lens_setup_entries("apple_medium")[:1], index_offset=0, total_models=1
        )
    ]
    error = events[-1]
    assert error["event"] == "pull_error"
    assert "timed out" in error["error"]
    assert "ReadTimeout" in error["error_details"]
    assert "secret" not in error["error_details"]
    assert "hf_private" not in error["error_details"]
