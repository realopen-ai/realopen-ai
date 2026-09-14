"""Tool configuration API (Brain ▸ Tools).

Endpoints:
    GET  /api/tools                  — every discovered tool + its config
    GET  /api/tools/{tool_name}      — one tool's config view
    PUT  /api/tools/{tool_name}      — update config (+ secrets)

Conventions follow the existing routers (Pydantic request models,
HTTPException(400/404) for user-facing errors). Configuration changes
are persisted to PostgreSQL via app.agent.tools.config_store and take
effect at runtime immediately (write-through cache).

Secrets (e.g. the Google Search API key) are validated where possible
before saving and stored via app/services/secrets.py — never returned
to the frontend (only a masked preview / set flag).
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.agent.base import get_tool_registry
from app.agent.tools import config_store
from app.agent.tools.config_base import config_registry
from app.services import secrets as secret_store

logger = logging.getLogger(__name__)

router = APIRouter()


# ─── Request/Response Models ────────────────────────────────────────


class ToolConfigUpdateRequest(BaseModel):
    # Partial configuration: any subset of the universal keys
    # (enabled / always_load / tags / model) and/or a "custom"
    # object with the tool-specific settings. Merged into the stored
    # config — omitted keys keep their current values. May be omitted
    # entirely when only updating secrets.
    config: Dict[str, Any] = {}

    # Secret fields → new values (null / "" clears the stored secret).
    # Values are validated where possible and NEVER returned.
    secrets: Optional[Dict[str, Optional[str]]] = None


# ─── Helpers ────────────────────────────────────────────────────────


def _require_tool(tool_name: str):
    """404 unless the tool is currently discovered (registered)."""
    registry = get_tool_registry()
    tool = registry.get(tool_name)
    if tool is None:
        raise HTTPException(
            status_code=404,
            detail=f"Tool '{tool_name}' not found",
        )
    return tool


async def _ensure_seeded() -> None:
    """Self-heal: seed any tool that appeared after startup.

    Covers "a new tool was added while the backend was running" — the
    GET endpoints trigger a (cheap, idempotent) seeding pass so every
    discovered tool always has a database row.
    """
    registry = get_tool_registry()
    cached = config_store.cached_tool_names()
    missing = [t.name for t in registry.all_tools() if t.name not in cached]
    if missing:
        await config_store.seed_and_load()


async def _apply_secrets(tool_name: str, secrets: Dict[str, Optional[str]]) -> None:
    """Validate + persist secret updates for a tool.

    Raises HTTPException(400) with a user-facing message when a value
    is rejected (e.g. Google declining an API key).
    """
    definition = config_registry.get(tool_name)
    declared = {s["field"]: s for s in (definition.secrets if definition else [])}

    for field, value in secrets.items():
        if field not in declared:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown secret field '{field}' for tool '{tool_name}'",
            )

        if value is None or not str(value).strip():
            secret_store.clear_secret(tool_name, field)
            logger.info("🧰 Cleared secret %s/%s", tool_name, field)
            continue

        value = str(value).strip()

        # Definition-provided validator (mini request, Groq-key style).
        validator = declared[field].get("validate")
        if validator is not None:
            try:
                ok, message = await validator(value)
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("Secret validation error: %s", e)
                ok, message = True, "unverified"
            if not ok:
                raise HTTPException(
                    status_code=400,
                    detail=message or "The key was rejected — check it and try again",
                )

        if not secret_store.set_secret(tool_name, field, value):
            raise HTTPException(
                status_code=500,
                detail="Could not store the credential",
            )
        logger.info("🧰 Stored secret %s/%s", tool_name, field)


# ─── Endpoints ──────────────────────────────────────────────────────


@router.get("/tools")
async def list_tools():
    """Every discovered tool with its effective configuration.

    The list is generated from the tool registry (the discovery
    mechanism is the source of truth) — nothing is hardcoded here.
    Each row carries the universal settings, the tool's custom
    configuration + schema, and masked secret statuses.
    """
    await _ensure_seeded()
    return {"tools": await config_store.all_tools_api_view()}


@router.get("/tools/{tool_name}")
async def get_tool(tool_name: str):
    """One tool's configuration view."""
    _require_tool(tool_name)
    await _ensure_seeded()
    view = await config_store.tool_api_view(tool_name)
    if view is None:  # pragma: no cover - guarded by _require_tool
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    return view


@router.put("/tools/{tool_name}")
async def update_tool(tool_name: str, request: ToolConfigUpdateRequest):
    """Update one tool's configuration (and secrets).

    The config patch is validated, merged into the stored configuration
    and persisted to PostgreSQL; the runtime picks it up immediately
    (tool list / schema / gating / model selection all read the config
    store). Secret values are validated where possible, stored in the
    secret store, and never echoed back.
    """
    _require_tool(tool_name)

    try:
        await config_store.update_tool_config(tool_name, request.config)
    except config_store.ConfigUpdateError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:  # pragma: no cover - defensive
        logger.exception("Tool config update failed: %s", e)
        raise HTTPException(status_code=500, detail="Could not save the configuration")

    if request.secrets:
        # Config first (so a cse_id submitted together with an API key
        # is already visible to the key validator), then secrets.
        await _apply_secrets(tool_name, request.secrets)

    view = await config_store.tool_api_view(tool_name)
    if view is None:  # pragma: no cover - guarded by _require_tool
        raise HTTPException(status_code=404, detail=f"Tool '{tool_name}' not found")
    return view
