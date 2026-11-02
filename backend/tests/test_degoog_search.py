"""Degoog's native API contract and optional-provider isolation."""

import httpx
import pytest

from app.services import web_search


def mock_api(monkeypatch, handler):
    client = httpx.AsyncClient
    monkeypatch.setattr(
        web_search.httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs),
    )


@pytest.mark.asyncio
async def test_native_results_and_limit(monkeypatch):
    def respond(request):
        assert request.url.path == "/prefix/api/search"
        assert dict(request.url.params) == {"q": "numpy & arrays", "type": "web"}
        assert request.extensions["timeout"]["read"] == 7
        return httpx.Response(
            200,
            json={
                "results": [
                    {"title": "Bad", "url": "javascript:alert(1)"},
                    None,
                    {
                        "title": "NumPy",
                        "url": "https://numpy.org/doc/",
                        "snippet": "Arrays",
                        "sources": ["DuckDuckGo", "Google"],
                    },
                    {"title": "Other", "url": "https://example.org", "snippet": None},
                ]
            },
        )

    mock_api(monkeypatch, respond)
    name, results = await web_search.search_degoog(
        "numpy & arrays",
        base_url="http://degoog:4444/prefix/",
        max_results=1,
        timeout_s=7,
    )
    assert name == "degoog"
    assert results == [
        {
            "provider": "Degoog",
            "title": "NumPy",
            "url": "https://numpy.org/doc/",
            "snippet": "Arrays",
            "engines": ["DuckDuckGo", "Google"],
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, message",
    [
        ({"results": [], "engineTimings": []}, "no active web engines"),
        ({"results": [], "engineTimings": [{"status": "blocked"}]}, "engines failed"),
        (
            {
                "results": [],
                "engineTimings": [{"status": "ok", "errorReason": "missing API key"}],
            },
            "engines failed",
        ),
        ({"error": "unavailable"}, "unavailable"),
        ({"results": None}, "invalid search response"),
        ([], "invalid search response"),
    ],
)
async def test_invalid_or_unconfigured_instance(monkeypatch, payload, message):
    mock_api(monkeypatch, lambda _: httpx.Response(200, json=payload))
    with pytest.raises(RuntimeError, match=message):
        await web_search.search_degoog("test")


@pytest.mark.asyncio
async def test_real_empty_search_is_not_configuration_error(monkeypatch):
    mock_api(
        monkeypatch,
        lambda _: httpx.Response(
            200,
            json={
                "results": [],
                "engineTimings": [{"status": "ok", "resultCount": 0}],
            },
        ),
    )
    assert await web_search.search_degoog("no hits") == ("degoog", [])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "http", "json"])
async def test_failure_does_not_hide_other_provider_results(monkeypatch, failure):
    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("Degoog timed out", request=request)
        if failure == "http":
            return httpx.Response(503)
        return httpx.Response(200, text="not JSON")

    mock_api(monkeypatch, respond)

    async def searxng(*args, **kwargs):
        return "searxng", [{"title": "Peer result", "url": "https://example.org"}]

    monkeypatch.setattr(web_search, "search_searxng", searxng)
    outcome = await web_search.search("test", {"degoog": {"enabled": True}})
    assert outcome["results"][0]["title"] == "Peer result"
    assert len(outcome["errors"]) == 1


@pytest.mark.asyncio
async def test_disabled_by_default_and_legacy_configs(monkeypatch):
    async def must_not_run(*args, **kwargs):
        pytest.fail("Disabled Degoog was called")

    monkeypatch.setattr(web_search, "search_degoog", must_not_run)
    outcome = await web_search.search("test", {"searxng": {"enabled": False}})
    assert outcome["no_providers"]
