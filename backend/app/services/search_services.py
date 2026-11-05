"""Reconcile DB-backed provider preferences with the host Docker runtime."""

import asyncio
import logging
from urllib.parse import urlsplit

import httpx

from app.config import settings
from app.services import sandbox_host

logger = logging.getLogger(__name__)
LOCAL_URLS = {"searxng": "http://searxng:8080", "degoog": "http://degoog:4444"}
_wake: asyncio.Event | None = None
_status: dict = {}


def provider_url(name, configured=None):
    """Translate only bundled Docker aliases; preserve custom network URLs."""
    default = getattr(settings, f"{name.upper()}_BASE_URL")
    if settings.REALOPEN_RUNTIME == "native" and (
        not configured or configured.rstrip("/") == LOCAL_URLS[name]
    ):
        return default
    return configured or default


def provider_choices() -> list[dict]:
    from app.agent.tools import config_store
    from app.agent.tools.config_base import definition_for_tool

    defaults = definition_for_tool("use_websearch").custom_defaults["providers"]
    current = (
        (config_store.get_tool_config("use_websearch") or {})
        .get("custom", {})
        .get("providers", {})
    )
    labels = {
        "searxng": "SearXNG",
        "degoog": "Degoog",
        "arxiv": "arXiv",
        "wikipedia": "Wikipedia",
        "google": "Google",
    }
    return [
        {
            "id": name,
            "name": labels.get(name, name),
            "enabled": current.get(name, values).get("enabled", False),
            "recommended": values.get("enabled", False),
            "local": name in LOCAL_URLS,
        }
        for name, values in defaults.items()
    ]


def desired_services() -> dict:
    # Imported lazily because web_search tool imports its service at discovery.
    from app.agent.tools import config_store

    config = config_store.get_tool_config("use_websearch") or {}
    providers = (config.get("custom") or {}).get("providers", {})
    result = {}
    for name, default in LOCAL_URLS.items():
        provider = providers.get(name, {})
        url = provider_url(name, provider.get("base_url"))
        expected = (
            getattr(settings, f"{name.upper()}_BASE_URL")
            if settings.REALOPEN_RUNTIME == "native"
            else default
        )
        managed = urlsplit(url.rstrip("/")) == urlsplit(expected)
        if settings.REALOPEN_RUNTIME == "native":
            managed = (
                managed and getattr(settings, f"NATIVE_MANAGED_{name.upper()}")
            )
        result[name] = {
            "enabled": bool(
                config.get("enabled", True)
                and provider.get("enabled", name == "searxng")
            ),
            "managed": managed,
        }
    return result


def request_reconcile():
    if _wake is not None:
        _wake.set()


def status() -> dict:
    result = {}
    for name, desired in desired_services().items():
        previous = _status.get(name, {})
        state = previous.get("status", "pending")
        if previous.get("desired") != desired:
            state = "starting" if desired["enabled"] else "stopping"
        if not desired["managed"]:
            state = "external" if desired["enabled"] else "stopped"
        result[name] = {**previous, **desired, "status": state}
    return result


async def reconcile_once():
    desired = desired_services()
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            response = await client.post(
                f"{sandbox_host.base_url()}/v1/search-services/reconcile",
                json={
                    name: choice["enabled"] and choice["managed"]
                    for name, choice in desired.items()
                },
            )
            response.raise_for_status()
            states = response.json()
            for name, choice in desired.items():
                if (
                    choice["enabled"]
                    and choice["managed"]
                    and states[name].get("status") == "ready"
                ):
                    path = "/healthz" if name == "searxng" else "/readyz"
                    try:
                        probe = await client.get(provider_url(name) + path, timeout=3)
                        if probe.status_code != 200:
                            states[name] = {"status": "starting"}
                    except httpx.HTTPError:
                        states[name] = {"status": "starting"}
        for name, choice in desired.items():
            _status[name] = {**states[name], "desired": choice}
    except Exception:
        logger.debug("Search services reconciliation failed", exc_info=True)
        for name, choice in desired.items():
            _status[name] = {
                "desired": choice,
                "status": "failed",
                "error": (
                    "Host runtime unavailable or outdated. Restart the native launcher and check data/logs/native/."
                    if settings.REALOPEN_RUNTIME == "native"
                    else "Host runtime unavailable or outdated. Start/restart the host runtime and check Docker."
                ),
            }


async def monitor():
    global _wake
    from app.agent.tools import config_store

    _wake = asyncio.Event()
    loaded = False
    try:
        while True:
            _wake.clear()
            try:
                # Never act on fallback defaults when the database is unavailable.
                if not loaded:
                    await config_store.seed_and_load(require_database=True)
                    loaded = True
                await reconcile_once()
            except Exception:
                logger.debug("Search preferences not yet available", exc_info=True)
                for name, choice in desired_services().items():
                    _status[name] = {
                        "desired": choice,
                        "status": "failed",
                        "error": "Saved provider preferences are unavailable. Services were left unchanged; retrying automatically.",
                    }
            try:
                await asyncio.wait_for(_wake.wait(), timeout=15)
            except asyncio.TimeoutError:
                pass
    finally:
        _wake = None
