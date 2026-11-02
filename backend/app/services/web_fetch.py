"""Bounded web downloads and browser-free Markdown extraction."""

import asyncio
from dataclasses import dataclass
import logging
import re
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup, Tag
import httpx
from markdownify import MarkdownConverter
import trafilatura

MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024
TEXT_TYPES = {"text/html", "application/xhtml+xml", "text/plain", "text/markdown"}
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FetchedPage:
    requested_url: str
    url: str
    title: str
    content: str
    extractor: str
    author: str = ""
    date: str = ""

    def render(self) -> str:
        lines = [f"# {self.title}", "", f"Source: {self.url}"]
        if self.requested_url != self.url:
            lines.append(f"Requested URL: {self.requested_url}")
        if self.author:
            lines.append(f"Author: {self.author}")
        if self.date:
            lines.append(f"Page date (metadata): {self.date}")
        lines.extend([f"Extraction: {self.extractor}", "", self.content])
        return "\n".join(lines)


def _language(element: Tag) -> str:
    candidates = [element, element.find("code"), element.parent]
    for candidate in candidates:
        if not isinstance(candidate, Tag):
            continue
        for value in candidate.get("class", []):
            match = re.fullmatch(r"(?:language-|lang-|highlight-)([\w+.-]+)", value)
            if match:
                return match[1].lower()
    return ""


class _MarkdownConverter(MarkdownConverter):
    def convert_pre(self, el, text, parent_tags):
        # Read the literal code, not inline Markdown conversions of <code>/<a>.
        # Longer fences keep embedded triple backticks inside the code block.
        code = el.get_text().strip("\n")
        if not code.strip():
            return ""
        fence = "`" * max(
            3, max((len(m.group()) + 1 for m in re.finditer(r"`{3,}", code)), default=3)
        )
        return f"\n\n{fence}{_language(el)}\n{code}\n{fence}\n\n"


def _root(html: str, url: str) -> Tag:
    soup = BeautifulSoup(html, "html.parser")
    for node in soup.select(
        "script, style, noscript, nav, footer, aside, form, [hidden], [aria-hidden='true']"
    ):
        node.decompose()
    root = soup.select_one("main, [role='main'], article") or soup.body or soup
    for anchor in root.find_all("a", href=True):
        try:
            target = urljoin(url, anchor["href"])
            if urlsplit(target).scheme not in {"http", "https"}:
                raise ValueError("Unsupported link scheme")
        except ValueError:
            del anchor["href"]
        else:
            anchor["href"] = target
    return root


def _lost_structure(root: Tag, markdown: str) -> bool:
    fenced = list(
        re.finditer(r"(?ms)^(`{3,}|~{3,})([^\n]*)\n(.*?)^\1[ \t]*(?:\n|$)", markdown)
    )
    for block in root.find_all("pre"):
        code = block.get_text().strip("\n")
        language = _language(block)
        if code.strip() and not any(
            code in match[3] and (not language or match[2].strip().lower() == language)
            for match in fenced
        ):
            return True
    tables = root.find_all("table")
    if tables and "|" not in markdown:
        return True
    for table in tables:
        for cell in table.find_all(["th", "td"]):
            text = cell.get_text(" ", strip=True)
            if text and text not in markdown.replace("\\|", "|"):
                return True
    return False


def extract(html: str, url: str, requested_url: str | None = None) -> FetchedPage:
    """Use Trafilatura first; recover absent content/structure with Markdownify."""
    root = _root(html, url)
    try:
        metadata = trafilatura.extract_metadata(html, default_url=url, extensive=False)
    except Exception:
        logger.debug("Web page metadata extraction failed", exc_info=True)
        metadata = None
    title = (
        (metadata.title if metadata else None)
        or (root.find("h1").get_text(" ", strip=True) if root.find("h1") else None)
        or urlsplit(url).hostname
        or "Web page"
    )
    try:
        content = (
            trafilatura.extract(
                html,
                url=url,
                output_format="markdown",
                include_comments=False,
                include_links=True,
                include_tables=True,
                favor_recall=True,
            )
            or ""
        )
    except Exception:
        logger.debug("Trafilatura extraction failed; trying Markdownify", exc_info=True)
        content = ""
    extractor = "trafilatura"
    if not content.strip() or _lost_structure(root, content):
        content = (
            _MarkdownConverter(heading_style="ATX", bullets="-", wrap=False)
            .convert(str(root))
            .strip()
        )
        extractor = "markdownify"
    if not content.strip():
        raise ValueError(
            "The page contained no readable content. It may require JavaScript or authentication."
        )
    return FetchedPage(
        requested_url=requested_url or url,
        url=url,
        title=title,
        content=content,
        extractor=extractor,
        author=(metadata.author or "") if metadata else "",
        date=(metadata.date or "") if metadata else "",
    )


async def fetch(url: str, timeout_s: int = 20) -> FetchedPage:
    async with asyncio.timeout(timeout_s):
        async with httpx.AsyncClient(
            timeout=timeout_s, follow_redirects=True, max_redirects=5
        ) as client:
            async with client.stream(
                "GET",
                url,
                headers={
                    "Accept": "text/html,application/xhtml+xml,text/plain,text/markdown"
                },
            ) as response:
                response.raise_for_status()
                kind = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .lower()
                    .strip()
                )
                if kind and kind not in TEXT_TYPES:
                    raise ValueError(
                        f"Unsupported page content type: {kind}. Web Fetch reads HTML and text; upload documents for document extraction."
                    )
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_DOWNLOAD_BYTES:
                        raise ValueError(
                            "Page exceeds the 5 MiB Web Fetch download limit."
                        )
                    chunks.append(chunk)
                # Decode using httpx's declared charset handling after bounded streaming.
                decoded = httpx.Response(
                    200,
                    content=b"".join(chunks),
                    headers={"content-type": response.headers.get("content-type", "")},
                )
                text, final_url = decoded.text, str(response.url)
        if kind in {"text/plain", "text/markdown"}:
            if not text.strip():
                raise ValueError("The page contained no readable content.")
            return FetchedPage(
                url, final_url, urlsplit(final_url).hostname or "Web page", text, "text"
            )
        return await asyncio.to_thread(extract, text, final_url, url)
