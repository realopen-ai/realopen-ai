"""
Web fetch tool using BeautifulSoup.

The agent can call this tool to fetch and extract readable text content
from a webpage URL. This is useful for getting full context after a web search,
allowing the agent to read and understand the content of relevant pages.

TODO: This is a naive but effective first version. Future improvements could include:
- Smarter content extraction (e.g. readability algorithms)
- Caching results to avoid repeated fetches
"""

from typing import List

import httpx
from bs4 import BeautifulSoup

from app.agent.base import BaseTool, ToolResult, ToolType, tool_registry


class WebFetchTool(BaseTool):
    name = "use_webfetch"
    description = (
        "Fetch and extract readable text from a URL. "
        "Use after web search for full context, or when the user provides "
        "a URL and asks about its content."
    )
    tool_type = ToolType.WEB_FETCH

    param_aliases = {
        "url": "url",
        "link": "url",
        "page": "url",
    }

    def get_parameters(self) -> dict:
        return {
            "url": {
                "type": "string",
                "description": "The URL to fetch and extract text from",
            },
        }

    def get_required_params(self) -> List[str]:
        return ["url"]

    async def execute(self, *, url: str, **kwargs) -> ToolResult:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()

        html = r.text

        text = self._extract_text(html)

        return ToolResult(
            success=True,
            output=text[:6000],  # IMPORTANT: cap context
        )

    def _extract_text(self, html: str) -> str:
        soup = BeautifulSoup(html, "html.parser")

        # remove junk
        for tag in soup(["script", "style", "noscript", "nav", "footer", "header"]):
            tag.decompose()

        return soup.get_text(separator="\n", strip=True)


# Register the tool
tool_registry.register(WebFetchTool())
