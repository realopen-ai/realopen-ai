"""Distinguish unavailable upstream engines from genuine empty searches."""

from pathlib import Path

import httpx
import pytest
import yaml

from app.services import web_search


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "results, failures, raises",
    [
        ([], [["duckduckgo", "CAPTCHA"], ["brave", "too many requests"]], True),
        ([], [], False),
        (
            [{"title": "NumPy", "url": "https://numpy.org", "content": "Arrays"}],
            [["google", "CAPTCHA"]],
            False,
        ),
    ],
)
async def test_upstream_failure_is_not_an_empty_success(
    monkeypatch, results, failures, raises
):
    original_client = httpx.AsyncClient

    def respond(request):
        assert request.url.path == "/search"
        assert request.url.params["format"] == "json"
        return httpx.Response(
            200, json={"results": results, "unresponsive_engines": failures}
        )

    monkeypatch.setattr(
        web_search.httpx,
        "AsyncClient",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(respond),
            **kwargs,
        ),
    )
    if raises:
        with pytest.raises(RuntimeError, match="SearXNG.*duckduckgo: CAPTCHA"):
            await web_search.search_searxng("NumPy")
    else:
        provider, hits = await web_search.search_searxng("NumPy")
        assert provider == "searxng"
        assert len(hits) == len(results)


def test_engine_overrides_are_effective_top_level_configuration():
    config = yaml.safe_load(
        (
            Path(__file__).resolve().parents[2] / "monitoring/searxng-settings.yml"
        ).read_text()
    )
    assert "engines" not in config["outgoing"]
    engines = {engine["name"]: engine for engine in config["engines"]}
    assert engines["bing"]["disabled"] is False
    assert engines["mwmbl"]["disabled"] is False
    assert "json" in config["search"]["formats"]
