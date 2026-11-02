"""Browser-free extraction, bounded downloads, and lossless result paging."""

import re
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent import service
from app.agent.base import ToolRegistry
from app.agent.tools.discovery import ReadToolResultTool
from app.agent.tools.web_fetch import WebFetchTool
from app.services import web_fetch
from test_agent_protocol import agent as agent_fixture, call, run


@pytest.fixture
def agent(monkeypatch):
    return agent_fixture.__wrapped__(monkeypatch)


def mock_http(monkeypatch, handler):
    original = httpx.AsyncClient
    monkeypatch.setattr(
        web_fetch.httpx,
        "AsyncClient",
        lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
    )


def test_article_extraction_removes_boilerplate_and_preserves_metadata():
    paragraphs = "".join(
        f"<p>Research finding {i}: plants use sunlight and water to produce energy through photosynthesis. This process supports growth and ecosystems.</p>"
        for i in range(8)
    )
    html = f"""<html><head><title>Photosynthesis explained</title><meta name="author" content="A. Researcher"><meta property="article:published_time" content="2025-02-12"></head><body><nav>BUY NOW NAVIGATION</nav><article><h1>Photosynthesis explained</h1>{paragraphs}<a href="https://example.org/research">Read the study</a></article><footer>FOOTER ADVERTISING</footer></body></html>"""
    page = web_fetch.extract(html, "https://example.org/article")
    assert page.extractor == "trafilatura"
    assert "Research finding" in page.content
    assert "BUY NOW" not in page.content and "FOOTER ADVERTISING" not in page.content
    assert page.author == "A Researcher"
    assert page.date == "2025-02-12"
    assert page.url == "https://example.org/article"


def test_fallback_preserves_code_indentation_language_tables_and_relative_links(
    monkeypatch,
):
    monkeypatch.setattr(
        web_fetch.trafilatura, "extract", lambda *a, **k: "Use an array."
    )
    html = """<nav>Ignore navigation</nav><main><h1>Arrays</h1><h2>Example</h2><pre><code class="language-python">if True:\n    print("hello")\n    # ``` inside code</code></pre><a href="../reference">Reference</a><table><tr><th>Name</th><th>Type</th></tr><tr><td>count</td><td>int</td></tr></table></main>"""
    page = web_fetch.extract(html, "https://example.org/docs/arrays")
    assert page.extractor == "markdownify"
    assert (
        '````python\nif True:\n    print("hello")\n    # ``` inside code\n````'
        in page.content
    )
    assert "## Example" in page.content
    assert "[Reference](https://example.org/reference)" in page.content
    assert "| Name | Type |" in page.content and "| count | int |" in page.content
    assert "Ignore navigation" not in page.content


@pytest.mark.parametrize("primary", [None, "", "   "])
def test_empty_primary_uses_markdownify(monkeypatch, primary):
    monkeypatch.setattr(web_fetch.trafilatura, "extract", lambda *a, **k: primary)
    page = web_fetch.extract(
        "<main><h1>Guide</h1><p>Bonjour مرحباً 🙂</p></main>", "https://example.org"
    )
    assert page.extractor == "markdownify"
    assert "Bonjour مرحباً 🙂" in page.content


def test_primary_error_falls_back_and_malformed_links_do_not_break_page(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("parser failed")

    monkeypatch.setattr(web_fetch.trafilatura, "extract", broken)
    page = web_fetch.extract(
        '<main><h1>Guide</h1><p>Useful text.</p><a href="http://[broken">Bad link</a><a href="javascript:alert(1)">Other link</a></main>',
        "https://example.org",
    )
    assert page.extractor == "markdownify"
    assert "Bad link" in page.content and "javascript:" not in page.content


def test_empty_script_only_page_fails_instead_of_becoming_evidence():
    with pytest.raises(ValueError, match="no readable content"):
        web_fetch.extract("<script>renderApp()</script>", "https://example.org")


def test_code_in_prose_does_not_count_as_preserved_code_block(monkeypatch):
    monkeypatch.setattr(
        web_fetch.trafilatura,
        "extract",
        lambda *a, **k: "Run print(42).\n\n```\nunrelated example\n```\n",
    )
    page = web_fetch.extract("<main><pre>print(42)</pre></main>", "https://example.org")
    assert page.extractor == "markdownify"
    assert "```\nprint(42)\n```" in page.content


@pytest.mark.asyncio
async def test_redirect_metadata_and_text_encoding(monkeypatch):
    def response(request):
        if request.url.path == "/old":
            return httpx.Response(302, headers={"Location": "/guide"})
        return httpx.Response(
            200,
            headers={"content-type": "text/plain; charset=utf-8"},
            content="Bonjour مرحباً 🙂".encode(),
        )

    mock_http(monkeypatch, response)
    page = await web_fetch.fetch("https://example.org/old")
    assert page.url == "https://example.org/guide"
    assert page.content == "Bonjour مرحباً 🙂"
    assert "Requested URL: https://example.org/old" in page.render()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["application/pdf", "image/png", "application/octet-stream"]
)
async def test_binary_content_is_not_parsed_as_html(monkeypatch, kind):
    mock_http(
        monkeypatch,
        lambda _: httpx.Response(
            200, headers={"content-type": kind}, content=b"binary"
        ),
    )
    result = await WebFetchTool().execute(url="https://example.org/file")
    assert not result.success and result.failure_kind == "execution"
    assert "Unsupported page content type" in result.output


@pytest.mark.asyncio
async def test_download_limit_applies_to_streamed_body(monkeypatch):
    monkeypatch.setattr(web_fetch, "MAX_DOWNLOAD_BYTES", 10)
    mock_http(
        monkeypatch,
        lambda _: httpx.Response(
            200, headers={"content-type": "text/html"}, content=b"a" * 11
        ),
    )
    with pytest.raises(ValueError, match="download limit"):
        await web_fetch.fetch("https://example.org")


@pytest.mark.asyncio
async def test_operational_errors_are_tool_failures(monkeypatch):
    async def timeout(*args):
        raise TimeoutError()

    monkeypatch.setattr(web_fetch, "fetch", timeout)
    result = await WebFetchTool().execute(url="https://example.org")
    assert not result.success and result.tool_call.status == "error"
    assert "TimeoutError" in result.output


@pytest.mark.asyncio
async def test_extraction_runs_off_event_loop(monkeypatch):
    mock_http(
        monkeypatch,
        lambda _: httpx.Response(
            200, headers={"content-type": "text/html"}, text="<main>Text</main>"
        ),
    )
    calls = []

    async def to_thread(fn, *args):
        calls.append(fn)
        return fn(*args)

    monkeypatch.setattr(web_fetch.asyncio, "to_thread", to_thread)
    await web_fetch.fetch("https://example.org")
    assert calls == [web_fetch.extract]


@pytest.mark.asyncio
async def test_agent_retains_full_page_and_pages_omitted_middle(agent, monkeypatch):
    tool = WebFetchTool()
    registry = ToolRegistry()
    registry.register(tool)
    registry.register(ReadToolResultTool())
    monkeypatch.setattr(service, "get_tool_registry", lambda: registry)
    monkeypatch.setattr(service, "_select_tools", lambda _: {tool.name})
    monkeypatch.setattr(service.settings, "AGENT_TOOL_RESULT_MAX_CHARS", 6000)
    monkeypatch.setattr(
        "app.agent.tools.web_fetch._configured_limits", lambda: (20, 2000)
    )
    page = web_fetch.FetchedPage(
        "https://example.org",
        "https://example.org",
        "Long document",
        "Beginning. " * 800 + "MIDDLE_MARKER\n" + "End. " * 1500,
        "trafilatura",
    )
    raw = page.render()
    monkeypatch.setattr(web_fetch, "fetch", AsyncMock(return_value=page))
    captures = []

    async def stream(model, messages, **kwargs):
        captures.append(messages)
        if len(captures) == 1:
            yield {
                "tool_calls": [call({"url": page.url}, name=tool.name)],
                "done": True,
            }
        elif len(captures) == 2:
            joined = "\n".join(m.get("content", "") for m in messages)
            assert "MIDDLE_MARKER" not in joined
            ref = re.search(r'"result_ref"\s*:\s*"([^"]+)"', joined).group(1)
            yield {
                "tool_calls": [
                    call(
                        {
                            "result_ref": ref,
                            "offset": raw.index("MIDDLE_MARKER"),
                            "limit": 100,
                        },
                        name="read_tool_result",
                        id="read",
                    )
                ],
                "done": True,
            }
        else:
            assert any(
                "MIDDLE_MARKER" in m.get("content", "")
                for m in messages
                if m.get("role") == "tool"
            )
            yield {"content": "Read the omitted section.", "done": True}

    monkeypatch.setattr(service.providers, "stream_chat", stream)
    events = await run(
        messages=[{"role": "user", "content": "Read https://example.org"}]
    )
    assert len(captures) == 3
    assert any(event.get("tool_call", {}).get("output") == raw for event in events)
