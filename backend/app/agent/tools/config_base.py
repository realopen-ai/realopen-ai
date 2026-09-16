"""Tool configuration definitions (Brain ▸ Tools).

This is the declarative layer that lives ALONGSIDE the tool definitions
under app/agent/tools/. A tool configuration definition describes:

- the tool's identity for the UI (display name, description)
- its DEFAULT JSON configuration (universal settings + custom settings)
- a lightweight SCHEMA of its custom settings so the frontend (or any
  generic renderer) can understand what exists
- which config fields are SECRETS (API keys — stored via
  app/services/secrets.py, never inside the config JSON)

Principle (task spec):

    TOOL IMPLEMENTATION      → existing tool / service execution layer
    TOOL CONFIG DEFINITION   → this package (app/agent/tools/)
    TOOL CONFIG DATA         → PostgreSQL (tool_configs table)

Adding configuration for a new tool:

1. (optional) declare a ``ToolConfigDefinition`` next to your tool class
   and ``register_config`` it — only tools with CUSTOM settings need one.
   Every registered tool automatically gets the universal settings
   (enabled / always_load / tags / model) even without a
   definition.
2. Define its default JSON (``custom_defaults``).
3. Define its schema/metadata (``custom_schema``).
4. The backend discovers it at startup (config_store seeds the DB row).
5. The frontend automatically knows the tool exists (GET /api/tools).
6. If the tool deserves a dedicated settings UI, add a tool-specific
   React component (frontend tools registry); otherwise the frontend
   renders the universal settings + a generic form from custom_schema.
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from app.agent.tools import tool_defaults

logger = logging.getLogger(__name__)


# ── Schema field descriptor types ──────────────────────────────────
# ``bool``    → toggle
# ``string``  → text input
# ``int``     → number input (whole numbers)
# ``float``   → number input (decimals, e.g. thresholds 0–1)
# ``secret``  → password-style input (value lives in the secret store;
#               the config JSON only carries a null placeholder)
# ``select``  → dropdown (``options``: [{value, label}])

FIELD_TYPES = ("bool", "string", "int", "float", "secret", "select")


class ConfigField:
    """One custom settings field descriptor (for the frontend)."""

    def __init__(
        self,
        key: str,
        label: str,
        type: str = "string",
        help: str = "",
        placeholder: str = "",
        options: Optional[List[Dict[str, str]]] = None,
        default: Any = None,
    ):
        if type not in FIELD_TYPES:
            raise ValueError(f"Unknown config field type: {type!r}")
        self.key = key
        self.label = label
        self.type = type
        self.help = help
        self.placeholder = placeholder
        self.options = options or []
        self.default = default

    def to_dict(self) -> Dict[str, Any]:
        d = {
            "key": self.key,
            "label": self.label,
            "type": self.type,
        }
        if self.help:
            d["help"] = self.help
        if self.placeholder:
            d["placeholder"] = self.placeholder
        if self.options:
            d["options"] = self.options
        if self.default is not None:
            d["default"] = self.default
        return d


class ToolConfigDefinition:
    """Declarative configuration definition for one tool.

    ``custom_defaults``: the default JSON for the tool-specific part of
    the configuration (nested under the ``custom`` key of the stored
    config). Universal settings defaults are derived automatically from
    the tool-loading policy (tool_defaults).

    ``custom_schema``: a description of the custom settings — sections
    of fields — so a generic frontend renderer (or a future dedicated
    component) knows what exists. Structure::

        [
            {
                "key": "providers",
                "label": "Providers",
                "fields": [ConfigField(...), ...],
            },
            ...
        ]

    ``secrets``: fields whose VALUES are stored in the secret store
    (state dir, 0600) instead of the config JSON. A secret descriptor
    may declare ``validate`` — an async callable run before saving a
    new value; the value is persisted only when validation passes
    (network-unreachable counts as accepted-but-unverified).
    """

    def __init__(
        self,
        tool_name: str,
        display_name: str = "",
        description: str = "",
        custom_defaults: Optional[Dict[str, Any]] = None,
        custom_schema: Optional[List[Dict[str, Any]]] = None,
        secrets: Optional[List[Dict[str, Any]]] = None,
        validate_custom: Optional[Callable[[Dict[str, Any]], Dict[str, Any]]] = None,
        # Which model_prefs task slot this tool's LLM work belongs to
        # (drives the "effective model" display and the inherit
        # fallback). None → the general chat model.
        model_task_slot: Optional[str] = None,
        model_fallback_role: Optional[str] = None,
    ):
        self.tool_name = tool_name
        self.display_name = display_name
        self.description = description
        self.custom_defaults = custom_defaults or {}
        self.custom_schema = custom_schema or []
        self.secrets = secrets or []
        self.validate_custom = validate_custom
        self.model_task_slot = model_task_slot
        self.model_fallback_role = model_fallback_role

    def universal_defaults(self) -> Dict[str, Any]:
        """Universal settings defaults, preserving current behavior."""
        return {
            "enabled": True,
            "always_load": tool_defaults.default_always_load(self.tool_name),
            "tags": tool_defaults.default_tags(self.tool_name),
            "model": None,  # None → inherit the task-slot model
        }

    def default_config(self) -> Dict[str, Any]:
        """Full default JSON (universal + custom) used for seeding."""
        cfg = self.universal_defaults()
        cfg["custom"] = _deep_copy(self.custom_defaults)
        return cfg

    def schema_dict(self) -> Optional[List[Dict[str, Any]]]:
        """Custom schema for the frontend (None when no custom config)."""
        if not self.custom_schema and not self.custom_defaults:
            return None
        out = []
        for section in self.custom_schema:
            out.append(
                {
                    "key": section.get("key", ""),
                    "label": section.get("label", ""),
                    "fields": [
                        f.to_dict() if isinstance(f, ConfigField) else dict(f)
                        for f in section.get("fields", [])
                    ],
                }
            )
        return out

    def secret_fields(self) -> List[Dict[str, Any]]:
        """Secret descriptors for the frontend (labels/descriptions only)."""
        out = []
        for s in self.secrets:
            out.append(
                {
                    "field": s["field"],
                    "label": s.get("label", s["field"]),
                    "help": s.get("help", ""),
                    "placeholder": s.get("placeholder", ""),
                    "required_when": s.get("required_when"),
                }
            )
        return out


# ── Config registry ────────────────────────────────────────────────


class _ConfigRegistry:
    """tool_name → ToolConfigDefinition.

    Mirrors the ToolRegistry pattern: definitions register themselves at
    import time (from the tool modules), and everything downstream
    (seeding, API, frontend metadata) is derived from the registries —
    no central hardcoded switch statement anywhere.
    """

    def __init__(self):
        self._defs: Dict[str, ToolConfigDefinition] = {}

    def register(self, definition: ToolConfigDefinition) -> None:
        self._defs[definition.tool_name] = definition

    def get(self, tool_name: str) -> Optional[ToolConfigDefinition]:
        return self._defs.get(tool_name)

    def all_definitions(self) -> List[ToolConfigDefinition]:
        return list(self._defs.values())


config_registry = _ConfigRegistry()


def register_config(definition: ToolConfigDefinition) -> None:
    """Register a tool configuration definition."""
    config_registry.register(definition)


# ── Universal fallback for tools without a definition ──────────────


def basic_definition(tool_name: str, display_name: str = "") -> ToolConfigDefinition:
    """The automatic basic config for tools without custom settings.

    Every discovered tool gets at least: enabled, always_load,
    tags and a model override — even without a declared
    ToolConfigDefinition.
    """
    return ToolConfigDefinition(
        tool_name=tool_name,
        display_name=display_name,
        custom_defaults={},
        custom_schema=[],
    )


# ── Helpers ────────────────────────────────────────────────────────


def _deep_copy(value: Any) -> Any:
    import copy

    return copy.deepcopy(value)


def definition_for_tool(tool_name: str, display_name: str = "") -> ToolConfigDefinition:
    """The config definition for a tool — declared one or basic fallback."""
    return config_registry.get(tool_name) or basic_definition(tool_name, display_name)
