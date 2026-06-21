"""
Web search tool using SearXNG.

The agent can call this tool to search the web via the self-hosted
SearXNG instance. Results are returned as a formatted string for
the LLM to process.
"""

import logging
import time
from typing import Dict, List

import httpx

from app.agent.base import BaseTool, ToolResult, ToolCall, ToolType, tool_registry
from app.config import settings

logger = logging.getLogger(__name__)


class WebSearchTool(BaseTool):
    name = "use_websearch"
    description = (
        "Search the web. Use for current info, facts, "
        "or topics beyond your training data."
    )
    tool_type = ToolType.WEB_SEARCH

    param_aliases = {
        "query": "query",
        "search": "query",
        "q": "query",
    }

    def get_parameters(self) -> dict:
        return {
            "query": {
                "type": "string",
                "description": "The search query",
            },
        }

    def get_required_params(self) -> List[str]:
        return ["query"]

    async def execute(self, *, query: str, **kwargs) -> ToolResult:
        """Search SearXNG and return formatted results."""
        start = time.time()
        tool_call = ToolCall(
            id=f"tc-{int(start * 1000)}",
            type=self.tool_type,
            name=self.name,
            status="running",
            title="Searching the web",
            query=query,
            started_at=start,
        )

        try:
            results = await self._search_searxng(query)
            tool_call.status = "completed"
            tool_call.completed_at = time.time()
            tool_call.web_results = results

            output = (
                self._format_results(query, results)
                if results
                else f"No results for: {query}"
            )
            return ToolResult(success=True, output=output, tool_call=tool_call)

        except Exception as e:
            logger.error("Web search failed: %s", e)
            tool_call.status = "error"
            tool_call.completed_at = time.time()
            tool_call.error = str(e)
            return ToolResult(
                success=False,
                output=f"Web search failed: {e}",
                tool_call=tool_call,
            )

    async def _search_searxng(self, query: str) -> List[Dict]:
        """Query SearXNG API and return parsed results."""
        url = f"{settings.SEARXNG_BASE_URL}/search"
        params = {
            "q": query,
            "format": "json",
            "categories": "general",
            "language": "en",
        }

        async with httpx.AsyncClient(
            timeout=15.0,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/91.0.4472.124 Safari/537.36"
                ),
                "Accept": (
                    "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "en-US,en;q=0.5",
                "Accept-Encoding": "gzip, deflate",
                "Connection": "keep-alive",
                "X-Forwarded-For": "127.0.0.1",
                "X-Real-IP": "127.0.0.1",
            },
            follow_redirects=True,
        ) as client:
            response = await client.get(
                url,
                params=params,
            )
            response.raise_for_status()
            data = response.json()

        results = []
        for item in data.get("results", [])[:5]:
            results.append(
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "snippet": item.get("content", ""),
                }
            )
        return results

    def _format_results(self, query: str, results: List[Dict]) -> str:
        """Format search results into a string for the LLM."""
        lines = [f'Web search results for "{query}":\n']
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}\n   URL: {r['url']}\n   {r['snippet']}\n")
        return "\n".join(lines)


# Register the tool
tool_registry.register(WebSearchTool())
