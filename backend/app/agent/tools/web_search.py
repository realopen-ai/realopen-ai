"""Web search tool (use_websearch) — thin execution wrapper.

The provider IMPLEMENTATIONS (SearXNG / ArXiv / Wikipedia / Google)
live in app/services/web_search.py, like the other services; this
module holds the tool itself and its CONFIGURATION definition
(Brain ▸ Tools), which follows the standard layering:

    TOOL IMPLEMENTATION      → app/services/web_search.py
    TOOL CONFIG DEFINITION   → this file (config_base)
    TOOL CONFIG DATA         → PostgreSQL (tool_configs)

ArXiv note: the arXiv API is a subject-based, field-prefixed search
(cat:cs.AI, all:term, AND-combined, quoted phrases) — NOT a whole-
sentence search. The service's ``build_arxiv_query`` translates the
LLM query accordingly, and the provider configuration exposes the
subject (category) the user wants to browse.
"""

import logging
import re
import time
from typing import Any, Dict, List

import httpx

from app.agent.base import BaseTool, ToolResult, ToolCall, ToolType, tool_registry
from app.agent.tools.config_base import (
    ConfigField,
    ToolConfigDefinition,
    register_config,
)
from app.agent.tools import config_store
from app.services import web_search as web_search_service

logger = logging.getLogger(__name__)


class WebSearchTool(BaseTool):
    name = "use_websearch"
    display_name = "Web Search"
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
        """Run every enabled provider (service) and merge results."""
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
            outcome = await web_search_service.search(query)

            if outcome["no_providers"]:
                tool_call.status = "error"
                tool_call.completed_at = time.time()
                tool_call.error = "No search providers enabled"
                return ToolResult(
                    success=False,
                    output=(
                        "Web search is enabled but has no active providers. "
                        "Enable at least one provider in Brain → Tools → "
                        "Web Search."
                    ),
                    tool_call=tool_call,
                )

            results = outcome["results"]
            errors = outcome["errors"]

            tool_call.status = "completed"
            tool_call.completed_at = time.time()
            tool_call.web_results = results

            if not results and errors:
                # All enabled providers failed (e.g. Google enabled but
                # missing its API key) — tell the agent why.
                tool_call.status = "error"
                tool_call.error = "; ".join(errors)
                output = (
                    "Web search failed — all enabled providers errored: "
                    + "; ".join(errors)
                )
                return ToolResult(success=False, output=output, tool_call=tool_call)

            output = (
                web_search_service.format_results(
                    query, results, outcome["by_provider"]
                )
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


# ── Configuration definition (Brain ▸ Tools ▸ Web Search) ──────────


def _validate_web_search_custom(custom: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the custom provider matrix; returns the cleaned object.

    Raises ValueError with a user-facing message on invalid input.
    """
    if not isinstance(custom, dict):
        raise ValueError("'custom' must be an object")
    providers_cfg = custom.get("providers")
    if providers_cfg is not None:
        if not isinstance(providers_cfg, dict):
            raise ValueError("'custom.providers' must be an object")

        known = ("searxng", "arxiv", "wikipedia", "google")
        for key, value in providers_cfg.items():
            if key not in known:
                raise ValueError(
                    f"Unknown web search provider: {key!r} "
                    f"(known: {', '.join(known)})"
                )
            if not isinstance(value, dict):
                raise ValueError(f"Provider {key!r} settings must be an object")
            if "enabled" in value and not isinstance(value["enabled"], bool):
                raise ValueError(f"Provider {key!r} 'enabled' must be a boolean")
            for numeric in ("max_results", "timeout_s"):
                if numeric in value:
                    try:
                        v = float(value[numeric])
                    except (TypeError, ValueError):
                        raise ValueError(
                            f"Provider {key!r} '{numeric}' must be a number"
                        )
                    if v <= 0 or v > 60:
                        raise ValueError(
                            f"Provider {key!r} '{numeric}' must be between " "1 and 60"
                        )
            if key == "searxng" and "base_url" in value:
                base = value["base_url"]
                if base is not None:
                    if not isinstance(base, str) or not base.strip():
                        raise ValueError(
                            "SearXNG 'base_url' must be a non-empty URL "
                            "(or empty to use the default)"
                        )
                    if not base.strip().startswith(("http://", "https://")):
                        raise ValueError(
                            "SearXNG 'base_url' must start with http:// or " "https://"
                        )
            if key == "arxiv" and "subject" in value:
                subject = value["subject"]
                if subject is None:
                    subject = ""
                    value["subject"] = subject
                if not isinstance(subject, str):
                    raise ValueError("ArXiv 'subject' must be a string")
                subject = subject.strip()
                if subject and not web_search_service.ARXIV_SUBJECT_RE.match(subject):
                    raise ValueError(
                        "ArXiv 'subject' must be an arXiv category code "
                        "(e.g. cs.AI, cs.CL, stat.ML, quant-ph)"
                    )
            if key == "arxiv" and "sort_by" in value:
                sort_by = value["sort_by"]
                if sort_by is None:
                    value["sort_by"] = "relevance"
                elif sort_by not in ("relevance", "submittedDate"):
                    raise ValueError(
                        "ArXiv 'sort_by' must be 'relevance' or " "'submittedDate'"
                    )
            if key == "wikipedia" and "language" in value:
                lang = value["language"]
                if not isinstance(lang, str) or not re.match(
                    r"^[a-z]{2,3}(-[a-z]{2})?$", (lang or "").strip().lower()
                ):
                    raise ValueError(
                        "Wikipedia 'language' must be a language code "
                        "(e.g. en, fr, pt-br)"
                    )
            if key == "google" and "cse_id" in value:
                cse = value["cse_id"]
                if cse is not None and not isinstance(cse, str):
                    raise ValueError("Google 'cse_id' must be a string")
    return custom


async def validate_google_secret(value: str) -> tuple:
    """Validate a Google API key + the stored CSE id with a mini request.

    Mirrors the Groq key flow: a cheap query (num=1) is sent with the
    key; Google must accept it. Network-unreachable is ACCEPTED as
    unverified (the app runs fully offline by default — the user may
    configure cloud features while offline). Rejections (4xx) fail.

    Returns (ok, user_message).
    """
    cfg = config_store.get_tool_config("use_websearch") or {}
    google_cfg = ((cfg.get("custom") or {}).get("providers") or {}).get("google", {})
    cse_id = google_cfg.get("cse_id", "")
    if not cse_id:
        # The key may arrive together with a cse_id in the same request;
        # config_store updates config BEFORE secrets are stored, so the
        # pending cse_id is usually already visible here.
        return False, "Save the Google CSE id before (or together with) the API key"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                "https://www.googleapis.com/customsearch/v1",
                params={"key": value, "cx": cse_id, "q": "test", "num": 1},
            )
            if response.status_code == 200:
                return True, "ok"
            if response.status_code in (400, 401, 403):
                return False, _google_error_detail(response.text)
            return False, f"Google returned HTTP {response.status_code}"
    except (httpx.TimeoutException, httpx.ConnectError):
        # Offline / unreachable — accept the key unverified
        return True, "unverified (Google unreachable)"
    except Exception as e:  # pragma: no cover - defensive
        return False, f"Unexpected error validating key: {e}"


def _google_error_detail(body: str) -> str:
    """Extract the human-readable message from a Google error payload."""
    try:
        import json

        data = json.loads(body)
        return (data.get("error") or {}).get("message") or "invalid credentials"
    except Exception:
        return "invalid credentials"


# Curated arXiv subjects (the full taxonomy lives at
# https://arxiv.org/category_taxonomy — these are the popular ones).
ARXIV_SUBJECT_OPTIONS = [
    {"value": "", "label": "All subjects"},
    {"value": "cs.AI", "label": "cs.AI — Artificial Intelligence"},
    {"value": "cs.CL", "label": "cs.CL — Computation & Language (NLP)"},
    {"value": "cs.CV", "label": "cs.CV — Computer Vision"},
    {"value": "cs.LG", "label": "cs.LG — Machine Learning"},
    {"value": "cs.NE", "label": "cs.NE — Neural & Evolutionary Comp."},
    {"value": "cs.RO", "label": "cs.RO — Robotics"},
    {"value": "cs.IR", "label": "cs.IR — Information Retrieval"},
    {"value": "cs.SD", "label": "cs.SD — Sound"},
    {"value": "cs.SE", "label": "cs.SE — Software Engineering"},
    {"value": "cs.CR", "label": "cs.CR — Cryptography & Security"},
    {"value": "cs.DC", "label": "cs.DC — Distributed Computing"},
    {"value": "cs.HC", "label": "cs.HC — Human-Computer Interaction"},
    {"value": "eess.AS", "label": "eess.AS — Audio & Speech"},
    {"value": "eess.IV", "label": "eess.IV — Image & Video"},
    {"value": "eess.SP", "label": "eess.SP — Signal Processing"},
    {"value": "math.ST", "label": "math.ST — Statistics Theory"},
    {"value": "math.OC", "label": "math.OC — Optimization & Control"},
    {"value": "math.PR", "label": "math.PR — Probability"},
    {"value": "stat.ML", "label": "stat.ML — ML (Statistics)"},
    {"value": "stat.AP", "label": "stat.AP — Statistical Applications"},
    {"value": "q-bio.NC", "label": "q-bio.NC — Neurons & Cognition"},
    {"value": "q-bio.QM", "label": "q-bio.QM — Quantitative Methods"},
    {"value": "q-fin.ST", "label": "q-fin.ST — Statistical Finance"},
    {"value": "quant-ph", "label": "quant-ph — Quantum Physics"},
    {"value": "cond-mat.mes-hall", "label": "cond-mat.mes-hall — Mesoscale"},
    {"value": "cond-mat.mtrl-sci", "label": "cond-mat.mtrl-sci — Materials"},
    {"value": "astro-ph", "label": "astro-ph — Astrophysics"},
    {"value": "hep-ph", "label": "hep-ph — High Energy Physics"},
    {"value": "physics.app-ph", "label": "physics.app-ph — Applied Physics"},
    {"value": "nlin.CD", "label": "nlin.CD — Chaos & Nonlinear"},
]

WEB_SEARCH_CONFIG = ToolConfigDefinition(
    tool_name="use_websearch",
    display_name="Web Search",
    description=(
        "Search the web. Use for current info, facts, "
        "or topics beyond your training data."
    ),
    model_task_slot="chat",
    custom_defaults={
        "providers": {
            "searxng": {
                "enabled": True,
                "base_url": None,  # None → settings.SEARXNG_BASE_URL
                "max_results": 5,
                "timeout_s": 15,
            },
            "arxiv": {
                # Off by default (SearXNG remains the default backend):
                # every enabled provider joins EVERY search, and arXiv
                # adds latency — users opt in per subject in the UI.
                "enabled": False,
                "subject": "",  # "" → all subjects
                "sort_by": "relevance",
                "max_results": 5,
            },
            "wikipedia": {"enabled": False, "language": "en", "max_results": 3},
            "google": {"enabled": False, "cse_id": "", "max_results": 5},
        },
    },
    custom_schema=[
        {
            "key": "providers",
            "label": "Providers",
            "fields": [],
        },
    ],
    secrets=[
        {
            "field": "google_api_key",
            "label": "Google API key",
            "help": (
                "Google Programmable Search key. Validated with a quick "
                "request, then saved locally — never shown again."
            ),
            "placeholder": "AIza…",
            # Mini-request validation (same flow as the Groq key):
            # the key is persisted only when Google accepts it (or is
            # unreachable — the app is offline-first).
            "validate": None,  # attached after the function is defined
        },
    ],
    validate_custom=_validate_web_search_custom,
)

# Per-provider sub-schemas, exposed for the frontend's dedicated Web
# Search component AND the generic renderer. Field keys are the FULL
# dotted paths into the stored custom object
# ("providers.arxiv.subject") so the generic renderer nests them at
# exactly the location the service reads.
WEB_SEARCH_PROVIDER_SCHEMAS: Dict[str, List[ConfigField]] = {
    "searxng": [
        ConfigField("providers.searxng.enabled", "Enabled", "bool"),
        ConfigField(
            "providers.searxng.base_url",
            "Base URL",
            "string",
            help="Self-hosted SearXNG instance (empty = default)",
            placeholder="http://searxng:8080",
        ),
        ConfigField("providers.searxng.max_results", "Max results", "int", default=5),
        ConfigField("providers.searxng.timeout_s", "Timeout (s)", "int", default=15),
    ],
    "arxiv": [
        ConfigField("providers.arxiv.enabled", "Enabled", "bool"),
        ConfigField(
            "providers.arxiv.subject",
            "Subject",
            "select",
            options=ARXIV_SUBJECT_OPTIONS,
            help=(
                "arXiv searches are subject-based — pick the category "
                "to browse/search (e.g. cs.AI)"
            ),
        ),
        ConfigField(
            "providers.arxiv.sort_by",
            "Sort by",
            "select",
            options=[
                {"value": "relevance", "label": "Relevance"},
                {"value": "submittedDate", "label": "Newest first"},
            ],
        ),
        ConfigField("providers.arxiv.max_results", "Max results", "int", default=5),
    ],
    "wikipedia": [
        ConfigField("providers.wikipedia.enabled", "Enabled", "bool"),
        ConfigField(
            "providers.wikipedia.language",
            "Language",
            "select",
            options=[
                {"value": "en", "label": "English"},
                {"value": "fr", "label": "French"},
                {"value": "de", "label": "German"},
                {"value": "es", "label": "Spanish"},
                {"value": "pt-br", "label": "Portuguese (BR)"},
                {"value": "ar", "label": "Arabic"},
            ],
        ),
        ConfigField("providers.wikipedia.max_results", "Max results", "int", default=3),
    ],
    "google": [
        ConfigField("providers.google.enabled", "Enabled", "bool"),
        ConfigField(
            "providers.google.cse_id",
            "CSE id",
            "string",
            help="Programmable Search Engine id (cx parameter)",
            placeholder="0123456789abcdef:xyz",
        ),
        ConfigField("providers.google.max_results", "Max results", "int", default=5),
    ],
}


def _attach_provider_schemas() -> None:
    """Fold the per-provider field schemas into the custom schema."""
    section = WEB_SEARCH_CONFIG.custom_schema[0]
    fields = []
    for provider, schema_fields in WEB_SEARCH_PROVIDER_SCHEMAS.items():
        fields.extend(f.to_dict() for f in schema_fields)
    section["fields"] = fields


_attach_provider_schemas()
# Attach the mini-request validator (defined above) to the secret
# descriptor — the tools API calls it before persisting a new key.
WEB_SEARCH_CONFIG.secrets[0]["validate"] = validate_google_secret
register_config(WEB_SEARCH_CONFIG)


# Register the tool
tool_registry.register(WebSearchTool())
