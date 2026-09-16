"""Tool configuration store (Brain ▸ Tools) — PostgreSQL persistence.

The tool_configs table is the source of truth for every tool's
configuration after initialization. This module:

- seeds missing rows on startup from the config definitions
  (config_base) — existing rows are NEVER overwritten when defaults
  change (no automatic versioning/migration: a changed default only
  affects tools that were never configured, and missing keys are
  filled at read time, so partial rows self-heal)
- keeps a write-through in-memory cache so the agent runtime can read
  configurations without a DB round-trip per request
- validates + persists updates coming from the API
- resolves per-tool model overrides against the existing
  provider/model architecture (model_prefs)
- migrates legacy rows on read: the old ``keyword_gate`` comma string
  becomes the ``tags`` list (once a row is saved again the legacy key
  is dropped from the stored JSON)

The cache is intentionally simple (a dict guarded by a lock): the app
is single-process and writes are rare. If the process restarts, the
cache is rebuilt from the database — nothing is lost.
"""

import logging
import threading
from typing import Any, Dict, List, Optional

from sqlalchemy import select

from app.agent.base import get_tool_registry
from app.agent.tools.config_base import definition_for_tool
from app.services import model_prefs
from app.services import providers

logger = logging.getLogger(__name__)

# Guards the synchronous cache mutations only (loop-agnostic — safe
# across event loops, which matters for tests and REPL usage). The DB
# writes themselves are awaited outside the lock; PostgreSQL remains
# the source of truth for concurrent writers.
_lock = threading.Lock()
_cache: Dict[str, Dict[str, Any]] = {}
_loaded = False


# ── Defaults / merging ─────────────────────────────────────────────


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """Merge override INTO base (override wins); nested dicts merge too."""
    out = dict(base)
    for key, value in override.items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def tags_from_gate(gate: Any) -> List[str]:
    """Parse a legacy comma-separated keyword_gate string into tags."""
    if gate is None:
        return []
    if isinstance(gate, list):  # already a tag list
        return [str(t).strip() for t in gate if str(t).strip()]
    return [t.strip() for t in str(gate).split(",") if t.strip()]


def merged_config(tool_name: str, stored: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Effective config: stored JSON with missing keys filled from defaults.

    Read-time only — the stored row is not rewritten (a changed default
    therefore never clobbers user settings, but new keys appear with
    their defaults until the user edits them).

    Legacy migration: a stored ``keyword_gate`` string (pre-tags rows)
    is converted to the ``tags`` list on read — the stored tags win
    over the defaults; the stale key is hidden from every reader.
    """
    definition = definition_for_tool(tool_name)
    defaults = definition.default_config()
    if not stored:
        return defaults
    merged = _deep_merge(defaults, stored)
    if "keyword_gate" in stored and "tags" not in stored:
        merged["tags"] = tags_from_gate(stored.get("keyword_gate"))
    merged.pop("keyword_gate", None)
    return merged


# ── Startup seeding ────────────────────────────────────────────────


async def seed_and_load(force: bool = False) -> Dict[str, int]:
    """Discover tools, seed missing DB rows, load the cache.

    Called from the app lifespan after Alembic migrations. Safe to call
    repeatedly (idempotent). Returns {"seeded": n, "loaded": m}.

    Behavior matrix:
    - new tool added            → seeded from its default JSON
    - default config changed    → existing rows untouched (missing keys
                                  filled at read time instead)
    - tool removed              → its row stays (harmless; the API only
                                  returns discovered tools)
    - config partially missing  → read-time merge fills the gaps
    - database empty            → every discovered tool seeded
    """
    global _loaded, _cache  # noqa

    from app.db.session import async_session_factory
    from app.db.models import ToolConfig

    registry = get_tool_registry()
    tools = registry.all_tools()

    seeded = 0
    rows: Dict[str, Dict[str, Any]] = {}
    db_ok = True
    try:
        async with async_session_factory() as db:
            existing = {
                row.tool_name: (row.config or {})
                for row in (await db.execute(select(ToolConfig))).scalars()
            }

            for tool in tools:
                definition = definition_for_tool(tool.name)
                if tool.name not in existing:
                    db.add(
                        ToolConfig(
                            tool_name=tool.name,
                            config=definition.default_config(),
                        )
                    )
                    rows[tool.name] = definition.default_config()
                    seeded += 1
                    logger.info("🧰 Seeded tool config: %s", tool.name)
                else:
                    rows[tool.name] = existing[tool.name]

            if seeded:
                await db.commit()
    except Exception as e:  # DB unavailable at startup
        db_ok = False
        logger.error("Tool config seeding failed: %s", e)

    with _lock:
        if db_ok:
            _cache.clear()
            _cache.update(rows)
            _loaded = True
            return {"seeded": seeded, "loaded": len(rows)}
        if not _loaded or force:
            # Fall back to code defaults so the agent still works;
            # this is logged, not silent, and the DB remains the
            # source of truth once reachable (writes still persist).
            _cache.clear()
            _cache.update(
                {
                    tool.name: definition_for_tool(tool.name).default_config()
                    for tool in tools
                }
            )
            _loaded = True
            return {"seeded": 0, "loaded": len(_cache)}
    return {"seeded": 0, "loaded": len(_cache)}


# ── Runtime reads (cache-backed, DB is source of truth) ────────────


def get_tool_config(tool_name: str) -> Optional[Dict[str, Any]]:
    """Effective config for one tool (merged with defaults), or None.

    Reads the write-through cache — populated at startup from the DB and
    updated on every API write. Never falls back to defaults for a tool
    that HAS a database row.
    """
    if tool_name in _cache:
        return merged_config(tool_name, _cache[tool_name])

    # Tool discovered after startup (or cache not loaded yet):
    # lazily fall back to the definition default. The GET /api/tools
    # endpoint re-seeds on read so this window is tiny.
    registry = get_tool_registry()
    if registry.has_tool(tool_name):
        return definition_for_tool(tool_name).default_config()
    return None


def cached_tool_names() -> set:
    """Tool names that have a database-backed cache entry.

    Used by the API's self-heal check to detect tools discovered after
    startup (get_tool_config falls back to code defaults for those, so
    the cache keys are the source of truth for "has a DB row").
    """
    with _lock:
        return set(_cache.keys())


def get_all_tool_configs() -> Dict[str, Dict[str, Any]]:
    """Effective configs for every discovered tool (registry order)."""
    registry = get_tool_registry()
    return {tool.name: get_tool_config(tool.name) for tool in registry.all_tools()}


def is_tool_enabled(tool_name: str) -> bool:
    cfg = get_tool_config(tool_name)
    return bool(cfg and cfg.get("enabled", True))


# ── Model override resolution ──────────────────────────────────────


async def _validate_override_model(model_id: str) -> bool:
    """A tool model override must reference a usable model.

    Same rules as model_prefs._is_valid_model: Groq models need a saved
    (validated) key; local models must be installed in Ollama or be a
    known profile/module model. When Ollama is unreachable the override
    is accepted (cannot disprove — mirrors model_prefs behavior).
    """
    if not model_id:
        return False
    if providers.is_groq_model(model_id):
        return providers.get_groq_key() is not None
    tags = await providers._fetch_ollama_tags()
    installed_ids = {m["id"] for m in tags}
    if model_id in installed_ids:
        return True
    if not providers._tags_cache.get("reachable", False):
        return True
    from app.config import settings as app_settings

    return model_id in {m.get("id") for m in app_settings.get_available_models()}


async def tool_model_override(tool_name: str) -> Optional[str]:
    """The tool's model override, or None when unset/invalid (inherit)."""
    cfg = get_tool_config(tool_name)
    model = (cfg or {}).get("model")
    if not model or not isinstance(model, str):
        return None
    model = model.strip()
    if not model:
        return None
    if not await _validate_override_model(model):
        logger.warning(
            "Tool %s model override %r is not usable — ignoring (inherit)",
            tool_name,
            model,
        )
        return None
    return model


async def resolve_tool_model(
    tool_name: str,
    task: Optional[str] = None,
    fallback_role: Optional[str] = None,
) -> Optional[str]:
    """Effective model for a tool: override when valid, else the
    model_prefs task slot / profile role (existing architecture).

    Used by tools that internally call an LLM (vision, image gen,
    report/pptx/excel generation) so a tool-level override always wins
    over the general Settings ▸ AI ▸ Models selection.
    """
    override = await tool_model_override(tool_name)
    if override:
        return override
    if task:
        return await model_prefs.resolve_task_model(task)
    if fallback_role:
        from app.config import settings as app_settings

        return app_settings.resolve_model(fallback_role)
    return None


# ── Updates (API layer) ────────────────────────────────────────────


class ConfigUpdateError(ValueError):
    """Validation failure with a user-meaningful message."""


def _validate_tags(raw: Any) -> List[str]:
    """Normalize + validate a tags patch value (list of strings).

    Accepts a list of strings (the canonical form) or a legacy
    comma-separated string. Deduplicates case-insensitively, strips
    blanks, and enforces sane limits. Raises ConfigUpdateError with a
    user-facing message on invalid input.
    """
    if raw is None:
        return []
    if isinstance(raw, str):  # legacy comma-separated form
        values = raw.split(",")
    elif isinstance(raw, list):
        values = raw
        for v in raw:
            if not isinstance(v, str):
                raise ConfigUpdateError("'tags' entries must be strings")
    else:
        raise ConfigUpdateError("'tags' must be a list of strings")

    out: List[str] = []
    seen: set = set()
    for v in values:
        v = v.strip()
        if not v:
            continue
        if len(v) > 60:
            raise ConfigUpdateError("Each tag must be 60 characters or fewer")
        if v.lower() not in seen:
            seen.add(v.lower())
            out.append(v)
    if len(out) > 50:
        raise ConfigUpdateError("Too many tags (max 50)")
    return out


async def update_tool_config(tool_name: str, patch: Dict[str, Any]) -> Dict[str, Any]:
    """Validate + persist a configuration patch for one tool.

    ``patch`` may contain universal keys (``enabled`` / ``always_load``
    / ``tags`` / ``model`` — plus the legacy ``keyword_gate`` string,
    accepted for backward compatibility) and a ``custom`` object; both
    are merged into the stored config (partial updates allowed). The
    database and the in-memory cache are updated together
    (write-through). Persisting a row also drops any legacy
    ``keyword_gate`` key.

    Raises ConfigUpdateError with a user-facing message on bad input.
    """
    from app.db.session import async_session_factory
    from app.db.models import ToolConfig
    from datetime import datetime

    registry = get_tool_registry()
    if not registry.has_tool(tool_name):
        raise ConfigUpdateError(f"Unknown tool: {tool_name!r}")

    current = (
        get_tool_config(tool_name) or definition_for_tool(tool_name).default_config()
    )

    # ── Validate universal keys ──
    updates: Dict[str, Any] = {}
    if "enabled" in patch:
        if not isinstance(patch["enabled"], bool):
            raise ConfigUpdateError("'enabled' must be a boolean")
        updates["enabled"] = patch["enabled"]
    if "always_load" in patch:
        if not isinstance(patch["always_load"], bool):
            raise ConfigUpdateError("'always_load' must be a boolean")
        updates["always_load"] = patch["always_load"]
    if "tags" in patch or "keyword_gate" in patch:
        # Legacy clients may still send the comma-separated string.
        raw = patch.get("tags", patch.get("keyword_gate"))
        updates["tags"] = _validate_tags(raw)
    if "model" in patch:
        model = patch["model"]
        if model is not None:
            if not isinstance(model, str):
                raise ConfigUpdateError("'model' must be a string or null")
            model = model.strip()
            if model and not await _validate_override_model(model):
                raise ConfigUpdateError(
                    f"Model {model!r} is not installed in Ollama "
                    "(pull it first or pick another model)."
                )
            if not model:
                model = None
        updates["model"] = model

    # ── Validate custom section ──
    if "custom" in patch:
        custom = patch["custom"]
        if custom is None:
            custom = {}
        if not isinstance(custom, dict):
            raise ConfigUpdateError("'custom' must be an object")
        validator = _custom_validator(tool_name)
        if validator:
            try:
                custom = validator(custom)
            except ValueError as e:
                raise ConfigUpdateError(str(e))
        updates["custom"] = custom

    # ── Merge + persist ──
    with _lock:
        # Base = the MIGRATED effective config (legacy keyword_gate
        # already converted to tags) so a partial update on a legacy
        # row persists a complete, new-format config.
        stored = dict(current)
        merged = _deep_merge(stored, updates)

    try:
        async with async_session_factory() as db:
            row = (
                await db.execute(
                    select(ToolConfig).where(ToolConfig.tool_name == tool_name)
                )
            ).scalar_one_or_none()
            if row is None:
                row = ToolConfig(tool_name=tool_name, config=merged)
                db.add(row)
            else:
                row.config = merged
                row.updated_at = datetime.utcnow()
            await db.commit()
    except Exception as e:
        logger.error("Failed to persist tool config %s: %s", tool_name, e)
        raise ConfigUpdateError("Could not save — database unavailable")

    with _lock:
        _cache[tool_name] = merged

    return merged_config(tool_name, merged)


def _custom_validator(tool_name: str):
    """The per-tool custom config validator, when the definition declares one.

    Validators live with the tool (config definition) — NOT in the
    service implementations. See web_search.py for the example.
    """
    from app.agent.tools.config_base import config_registry

    definition = config_registry.get(tool_name)
    if definition is None:
        return None
    return getattr(definition, "validate_custom", None)


# ── API view builder ──────────────────────────────────────────────


async def tool_api_view(tool_name: str) -> Optional[Dict[str, Any]]:
    """The GET /api/tools view of one tool: metadata + config + secrets."""
    registry = get_tool_registry()
    tool = registry.get(tool_name)
    if tool is None:
        return None

    from app.agent.tools.config_base import config_registry

    definition = config_registry.get(tool_name) or definition_for_tool(tool_name)
    cfg = get_tool_config(tool_name) or definition.default_config()

    view: Dict[str, Any] = {
        "tool": tool_name,
        "display_name": definition.display_name or tool.get_display_name(),
        "description": definition.description or tool.description,
        "tool_type": tool.tool_type.value,
        "config": {
            "enabled": cfg.get("enabled", True),
            "always_load": cfg.get("always_load", True),
            "tags": cfg.get("tags", []) or [],
            "model": cfg.get("model"),
            "custom": cfg.get("custom", {}),
        },
        # The tag defaults derived from the tool-loading policy — the
        # frontend uses them to re-arm the gate when Always load is
        # turned on with an empty tag list.
        "default_tags": definition.universal_defaults()["tags"],
        "custom_schema": definition.schema_dict(),
        "has_custom": bool(definition.custom_defaults),
    }

    # Effective model (what the tool actually uses right now)
    task = getattr(definition, "model_task_slot", None)
    fallback = getattr(definition, "model_fallback_role", None)
    view["effective_model"] = await resolve_tool_model(
        tool_name, task=task, fallback_role=fallback
    )

    # Secret statuses (masked, never plaintext)
    if definition.secrets:
        from app.services import secrets as secret_store

        view["secrets"] = {
            s["field"]: secret_store.secret_status(tool_name, s["field"])
            for s in definition.secrets
        }
    else:
        view["secrets"] = {}

    return view


async def all_tools_api_view() -> List[Dict[str, Any]]:
    """API views for every discovered tool (registry order)."""
    registry = get_tool_registry()
    views = []
    for tool in registry.all_tools():
        view = await tool_api_view(tool.name)
        if view:
            views.append(view)
    return views
