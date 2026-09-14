"""Web search service — provider implementations and result merging.

This is the EXECUTION layer for the Web Search tool (use_websearch),
following the same layering as the other services (report_gen,
pptx_gen, excel_gen…):

    TOOL IMPLEMENTATION      → this service (app/services/web_search.py)
    TOOL CONFIG DEFINITION   → app/agent/tools/web_search.py (config_base)
    TOOL CONFIG DATA         → PostgreSQL (tool_configs, via config_store)

Providers (each independently enable/disable-able and configured via
the persisted tool configuration):

- SearXNG   — self-hosted metasearch (JSON API); base URL defaults to
              settings.SEARXNG_BASE_URL
- ArXiv     — scientific preprints, queried through the official
              ``arxiv`` Python package (https://pypi.org/project/arxiv/).
              arXiv's API is a FIELD-SUBJECT search, not a whole-
              sentence search: queries are built from a subject
              (category, e.g. ``cat:cs.AI``) plus keyword terms
              combined with AND — see ``build_arxiv_query``.
- Wikipedia — the MediaWiki search API (language-configurable)
- Google    — Programmable Search Engine JSON API (API key from the
              secret store + CSE id from the configuration)

``search()`` runs every enabled provider in parallel (a failing
provider is skipped; its peers still answer), round-robin interleaves
the results so every provider is represented, and caps the total so
prompts stay small for 4B/7B models.

The provider matrix is read from the persisted tool configuration
(PostgreSQL via config_store) — UI changes therefore change runtime
behavior immediately.
"""

import asyncio
import logging
import re
from typing import Any, Dict, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Total results cap across providers (keeps prompts small for 4B/7B
# models — the agent only needs the top hits).
MAX_TOTAL_RESULTS = 8

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

# ── arXiv: subject-based query construction ─────────────────────────
#
# Per the arXiv API user manual (info.arxiv.org/help/api/user-manual.html):
#
# - searches are FIELD-prefixed: ti / au / abs / cat / all …
# - the SUBJECT (category, e.g. cs.AI) is searched via ``cat:``
# - multiple terms are combined with AND / OR / ANDNOT
# - multi-word phrases must be double-quoted
# - a space in a search_query "extends the query to include multiple
#   fields" — a raw sentence after ``all:`` is NOT a sentence search
#
# So a whole-sentence query like "latest research on transformers" is
# translated into subject + keyword form instead of being forwarded.

# Filler words that carry no arXiv search value (query-language noise
# from the LLM, plus generic research-browsing words).
ARXIV_STOPWORDS = frozenset(
    {
        "a", "an", "the", "of", "on", "in", "for", "to", "and", "or",
        "with", "about", "into", "from", "by", "at", "as", "is", "are",
        "was", "were", "be", "been", "do", "does", "did", "can", "could",
        "will", "would", "should", "may", "might", "must", "shall",
        "what", "which", "who", "whom", "whose", "when", "where", "why",
        "how", "that", "this", "these", "those", "there", "here",
        "i", "me", "my", "we", "our", "you", "your", "he", "she", "it",
        "they", "them", "their", "us",
        "find", "search", "look", "look up", "lookup", "get", "give",
        "show", "tell", "please", "want", "need", "like",
        "latest", "recent", "new", "newest", "current", "today",
        "now", "top", "best", "good", "popular", "famous",
        "paper", "papers", "article", "articles", "study", "studies",
        "research", "researcher", "researchers", "work", "works",
        "publication", "publications", "preprint", "preprints",
        "arxiv", "pdf", "using", "use", "used", "based", "regarding",
        "some", "any", "all", "more", "most", "much", "many", "few",
    }
)

# A single arXiv subject/category code, e.g. "cs.AI", "cond-mat",
# "quant-ph", "cond-mat.mes-hall", "stat.ML", "physics.app-ph".
ARXIV_SUBJECT_RE = re.compile(
    r"^[a-z]+(?:-[a-z]+)*(?:\.[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*)?$"
)


def arxiv_keywords(query: str, limit: int = 5) -> List[str]:
    """Content keywords extracted from a natural-language query.

    Lowercased, deduplicated, stopword-stripped, capped at ``limit``.
    """
    words = re.findall(r"[A-Za-z][A-Za-z0-9\-']*", query or "")
    seen: set = set()
    out: List[str] = []
    for word in words:
        lower = word.lower().strip("'-")
        if not lower or lower in ARXIV_STOPWORDS or lower in seen:
            continue
        seen.add(lower)
        out.append(lower)
        if len(out) >= limit:
            break
    return out


def build_arxiv_query(query: str, subject: str = "") -> str:
    """Translate a user/LLM query into a well-formed arXiv search_query.

    Rules (see the arXiv API user manual):

    1. If the query is already wrapped in double quotes → exact phrase:
       ``all:"…"`` (e.g. paper-title searches).
    2. Otherwise the content keywords (stopwords stripped) are ANDed
       as single-word fields: ``all:kw1 AND all:kw2 …``.
    3. A configured subject (category) is prepended as
       ``cat:subject AND (…)`` — a subject alone browses that category.

    Returns "" when there is nothing searchable (no keywords and no
    subject) — the caller surfaces a clear error in that case.
    """
    subject = (subject or "").strip()
    stripped = (query or "").strip()

    terms: List[str] = []
    if (
        len(stripped) >= 2
        and stripped.startswith('"')
        and stripped.endswith('"')
    ):
        phrase = stripped[1:-1].strip()
        if phrase:
            terms = [f'all:"{phrase}"']
    else:
        terms = [f"all:{kw}" for kw in arxiv_keywords(stripped)]

    parts: List[str] = []
    if subject:
        parts.append(f"cat:{subject}")
    parts.extend(terms)
    return " AND ".join(parts)


def _arxiv_sort_criterion(sort_by: str):
    """Map a configured sort name onto the package's sort criterion."""
    import arxiv as arxiv_pkg

    if (sort_by or "").strip() == "submittedDate":
        return arxiv_pkg.SortCriterion.SubmittedDate
    return arxiv_pkg.SortCriterion.Relevance


def _run_arxiv_search(
    search_query: str, max_results: int, sort_by: str
) -> List[Dict[str, Any]]:
    """Blocking arXiv API call through the ``arxiv`` package.

    Runs inside asyncio.to_thread — the package is requests-based and
    sync. The client enforces arXiv's terms of use (one request per
    three seconds) and retries transient failures.
    """
    import arxiv as arxiv_pkg

    n = max(1, int(max_results))
    search = arxiv_pkg.Search(
        query=search_query,
        max_results=n,
        sort_by=_arxiv_sort_criterion(sort_by),
    )
    # page_size = max_results → exactly one small request.
    client = arxiv_pkg.Client(
        page_size=n, delay_seconds=3.0, num_retries=2
    )

    results: List[Dict[str, Any]] = []
    for res in client.results(search):
        title = re.sub(r"\s+", " ", (res.title or "")).strip()
        summary = re.sub(r"\s+", " ", (res.summary or "")).strip()
        authors = ", ".join(
            a.name for a in (res.authors or [])[:3]
        )
        published = ""
        if res.published and res.published.year > 1900:
            published = res.published.strftime("%Y-%m")
        snippet = summary
        if authors:
            snippet = f"{authors}. {snippet}"
        if published:
            snippet = f"[{published}] {snippet}"
        results.append(
            {
                "provider": "ArXiv",
                "title": title,
                # entry_id is the stable abs page (http://arxiv.org/abs/…)
                "url": res.entry_id,
                "snippet": snippet[:400],
                "pdf_url": res.pdf_url or "",
                "categories": list(res.categories or [])[:4],
            }
        )
    return results


async def search_arxiv(
    query: str,
    subject: str = "",
    max_results: int = 5,
    sort_by: str = "relevance",
) -> tuple:
    """Query arXiv (subject + keywords) via the ``arxiv`` package."""
    try:
        import arxiv as _arxiv_pkg  # noqa: F401 — availability probe
    except ImportError:
        raise RuntimeError(
            "The 'arxiv' Python package is not installed "
            "(pip install arxiv) — ArXiv provider disabled"
        )

    search_query = build_arxiv_query(query, subject)
    if not search_query:
        raise RuntimeError(
            "ArXiv search is subject-based — configure a subject "
            "(e.g. cs.AI) or provide keywords"
        )

    # Subject-only browse: relevance order is undefined for a bare
    # cat: query — newest-first is the sensible default.
    if (
        subject
        and not arxiv_keywords(query)
        and (sort_by or "relevance") == "relevance"
    ):
        sort_by = "submittedDate"

    results = await asyncio.to_thread(
        _run_arxiv_search, search_query, max_results, sort_by
    )
    return ("arxiv", results)


# ── Provider: SearXNG ──────────────────────────────────────────────


async def search_searxng(
    query: str,
    base_url: Optional[str] = None,
    max_results: int = 5,
    timeout_s: float = 15.0,
) -> tuple:
    """Query the SearXNG API and return parsed results."""
    url = f"{(base_url or settings.SEARXNG_BASE_URL).rstrip('/')}/search"
    params = {
        "q": query,
        "format": "json",
        "categories": "general",
        "language": "en",
    }

    async with httpx.AsyncClient(
        timeout=timeout_s,
        headers={
            **_HEADERS,
            "Accept-Encoding": "gzip, deflate",
            "Connection": "keep-alive",
            "X-Forwarded-For": "127.0.0.1",
            "X-Real-IP": "127.0.0.1",
        },
        follow_redirects=True,
    ) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    results = []
    for item in data.get("results", [])[: max(1, int(max_results))]:
        results.append(
            {
                "provider": "SearXNG",
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "snippet": item.get("content", ""),
            }
        )
    return ("searxng", results)


# ── Provider: Wikipedia ────────────────────────────────────────────


async def search_wikipedia(
    query: str, language: str = "en", max_results: int = 3
) -> tuple:
    """Query the MediaWiki search API."""
    lang = (language or "en").strip().lower() or "en"
    url = f"https://{lang}.wikipedia.org/w/api.php"
    params = {
        "action": "query",
        "list": "search",
        "srsearch": query,
        "srlimit": max(1, int(max_results)),
        "format": "json",
    }
    async with httpx.AsyncClient(
        timeout=15.0, headers=_HEADERS, follow_redirects=True
    ) as client:
        response = await client.get(url, params=params)
        response.raise_for_status()
        data = response.json()

    results = []
    for item in (
        (data.get("query") or {}).get("search") or []
    )[: max(1, int(max_results))]:
        title = item.get("title", "")
        snippet = re.sub(r"<[^>]+>", "", item.get("snippet", ""))[:400]
        if title:
            results.append(
                {
                    "provider": "Wikipedia",
                    "title": title,
                    "url": f"https://{lang}.wikipedia.org/wiki/"
                    + title.replace(" ", "_"),
                    "snippet": snippet,
                }
            )
    return ("wikipedia", results)


# ── Provider: Google (Programmable Search Engine) ──────────────────


def _google_error_detail(body: str) -> str:
    """Extract the human-readable message from a Google error payload."""
    try:
        import json

        data = json.loads(body)
        return (
            (data.get("error") or {}).get("message")
            or "invalid credentials"
        )
    except Exception:
        return "invalid credentials"


async def search_google(
    query: str, cse_id: str = "", max_results: int = 5
) -> tuple:
    """Query the Google Programmable Search JSON API.

    Requires the API key (secret store: use_websearch/google_api_key)
    and a CSE id (custom configuration).
    """
    from app.services import secrets as secret_store

    api_key = secret_store.get_secret("use_websearch", "google_api_key")
    if not api_key:
        raise RuntimeError(
            "Google Search is enabled but no API key is configured"
        )
    if not cse_id:
        raise RuntimeError(
            "Google Search is enabled but no CSE id is configured"
        )

    url = "https://www.googleapis.com/customsearch/v1"
    params = {
        "key": api_key,
        "cx": cse_id,
        "q": query,
        "num": min(10, max(1, int(max_results))),
    }
    async with httpx.AsyncClient(
        timeout=15.0, headers=_HEADERS, follow_redirects=True
    ) as client:
        response = await client.get(url, params=params)
        if response.status_code in (400, 401, 403):
            detail = _google_error_detail(response.text)
            raise RuntimeError(
                f"Google Search rejected the request: {detail}"
            )
        response.raise_for_status()
        data = response.json()

    results = []
    for item in (data.get("items") or [])[: max(1, int(max_results))]:
        results.append(
            {
                "provider": "Google",
                "title": item.get("title", ""),
                "url": item.get("link", ""),
                "snippet": item.get("snippet", ""),
            }
        )
    return ("google", results)


# ── Provider matrix: read config → run enabled providers ───────────


def _providers_config() -> Dict[str, Any]:
    """The persisted provider matrix (Brain ▸ Tools ▸ Web Search).

    Local import: keeps this service importable from the tool module
    during package initialization without circular imports.
    """
    from app.agent.tools import config_store

    cfg = config_store.get_tool_config("use_websearch") or {}
    return (cfg.get("custom") or {}).get("providers") or {}


async def search(
    query: str, providers_cfg: Optional[Dict[str, Any]] = None
) -> Dict[str, Any]:
    """Run every enabled provider in parallel and merge the results.

    Returns a dict::

        {
            "results":     [ capped, interleaved result dicts ],
            "by_provider": { provider_name: [result dicts] },
            "errors":      [ provider error messages ],
            "no_providers": bool,
        }

    ``providers_cfg`` overrides the persisted configuration (used by
    tests); when None the config store (PostgreSQL-backed) is read.
    """
    if providers_cfg is None:
        providers_cfg = _providers_config()
    providers_cfg = providers_cfg or {}

    search_tasks = []

    searxng = providers_cfg.get("searxng", {})
    if searxng.get("enabled", True):
        search_tasks.append(
            search_searxng(
                query,
                base_url=searxng.get("base_url"),
                max_results=searxng.get("max_results", 5),
                timeout_s=searxng.get("timeout_s", 15),
            )
        )

    arxiv = providers_cfg.get("arxiv", {})
    if arxiv.get("enabled", False):
        search_tasks.append(
            search_arxiv(
                query,
                subject=arxiv.get("subject", ""),
                max_results=arxiv.get("max_results", 5),
                sort_by=arxiv.get("sort_by", "relevance"),
            )
        )

    wikipedia = providers_cfg.get("wikipedia", {})
    if wikipedia.get("enabled", False):
        search_tasks.append(
            search_wikipedia(
                query,
                language=wikipedia.get("language", "en"),
                max_results=wikipedia.get("max_results", 3),
            )
        )

    google = providers_cfg.get("google", {})
    if google.get("enabled", False):
        search_tasks.append(
            search_google(
                query,
                cse_id=google.get("cse_id", ""),
                max_results=google.get("max_results", 5),
            )
        )

    if not search_tasks:
        return {
            "results": [],
            "by_provider": {},
            "errors": [],
            "no_providers": True,
        }

    # Run all enabled providers concurrently; a failing provider is
    # skipped (its peers still answer). When EVERY provider fails the
    # errors are surfaced to the agent.
    gathered = await asyncio.gather(*search_tasks, return_exceptions=True)
    by_provider: Dict[str, List[Dict]] = {}
    errors: List[str] = []
    for outcome in gathered:
        if isinstance(outcome, Exception):
            logger.warning("Web search provider failed: %s", outcome)
            errors.append(str(outcome))
            continue
        provider_name, results = outcome
        if results:
            by_provider[provider_name] = results

    results = interleave(by_provider)[:MAX_TOTAL_RESULTS]
    return {
        "results": results,
        "by_provider": by_provider,
        "errors": errors,
        "no_providers": False,
    }


# ── Result merging / formatting ────────────────────────────────────


def interleave(by_provider: Dict[str, List[Dict]]) -> List[Dict]:
    """Round-robin merge so every enabled provider is represented."""
    queues = [list(results) for results in by_provider.values()]
    merged: List[Dict] = []
    i = 0
    while any(queues):
        queue = queues[i % len(queues)]
        if queue:
            merged.append(queue.pop(0))
        i += 1
    return merged


def format_results(
    query: str, results: List[Dict], by_provider: Dict[str, List[Dict]]
) -> str:
    """Format search results into a string for the LLM."""
    lines = [f'Web search results for "{query}":\n']
    for i, r in enumerate(results, 1):
        source = f" ({r['provider']})" if r.get("provider") else ""
        lines.append(
            f"{i}. {r['title']}{source}\n   URL: {r['url']}\n   {r['snippet']}\n"
        )
    return "\n".join(lines)
