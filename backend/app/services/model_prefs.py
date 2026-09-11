"""Per-task model preferences (Settings ▸ AI ▸ Models).

Six user-facing "task slots" control which model runs which job:

    chat               → the main chat / agent loop
    vision             → image understanding (vision tool, RAG OCR)
    document_reasoning → RAG reranking & document reasoning
    report             → report + presentation generation
    excel              → Excel spec generation
    image              → image generation (diffusion models)

Each slot either holds an explicit model id (user selection, persisted at
``state/model_prefs.json``) or falls back to the profile role resolved
from ``profiles.yml`` — i.e. the models chosen at setup.

Slot constraints:
- ``vision`` and ``image`` are LOCAL-ONLY (they rely on Ollama-native
  image/generate formats), so cloud (Groq) models are rejected there.

Preferences are validated on write AND on read: a pref whose model has
since been uninstalled (or whose provider was disconnected) is ignored
and the profile default is used instead.
"""

import json
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.config import settings
from app.services import providers

# ─── Task slot definitions ──────────────────────────────────────────

TASK_SLOTS: Dict[str, Dict[str, Any]] = {
    "chat": {
        "label": "Chat / Agent",
        "fallback_role": "default",
        "local_only": False,
    },
    "vision": {
        "label": "Vision",
        "fallback_role": "default_vision",
        "local_only": True,
    },
    "document_reasoning": {
        "label": "Document reasoning",
        "fallback_role": "default_utility",
        "local_only": False,
    },
    "report": {
        "label": "Report generation",
        "fallback_role": "default_utility",
        "local_only": False,
    },
    "excel": {
        "label": "Excel generation",
        "fallback_role": "default_utility",
        "local_only": False,
    },
    "image": {
        "label": "Image generation",
        "fallback_role": "default_image_gen",
        "local_only": True,
    },
}

# Role names that mean "the chat model" in incoming requests
_CHAT_ROLE_ALIASES = {"default", "chat", "assistant"}

_prefs_lock = threading.Lock()


def _prefs_path() -> Path:
    return providers._state_dir() / "model_prefs.json"


def get_prefs() -> Dict[str, str]:
    """Raw persisted prefs (task → model id). Invalid/missing file → {}."""
    path = _prefs_path()
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {
                    k: str(v)
                    for k, v in data.items()
                    if k in TASK_SLOTS and isinstance(v, str) and v
                }
    except (json.JSONDecodeError, OSError):
        pass
    return {}


def _save_prefs(prefs: Dict[str, str]) -> None:
    path = _prefs_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(prefs, indent=2), encoding="utf-8")


# ─── Validity ───────────────────────────────────────────────────────


async def _is_valid_model(model_id: str) -> bool:
    """A pref target is valid when its provider can actually run it."""
    if providers.is_groq_model(model_id):
        # Cloud model — requires a saved (validated-on-connect) key
        return providers.get_groq_key() is not None

    tags = await providers._fetch_ollama_tags()
    installed_ids = {m["id"] for m in tags}
    if model_id in installed_ids:
        return True
    if not providers._tags_cache.get("reachable", False):
        # Ollama unreachable — cannot disprove; accept the pref rather
        # than silently reverting the user's choice.
        return True
    # Ollama reachable: the model must be installed or be a known
    # profile/module model (defaults that aren't pulled yet).
    return model_id in {m.get("id") for m in settings.get_available_models()}


# ─── Resolution ─────────────────────────────────────────────────────


def default_task_model(task: str) -> str:
    """The setup/profile default for a task (no pref applied)."""
    slot = TASK_SLOTS.get(task)
    if slot is None:
        raise KeyError(f"Unknown task slot: {task!r}")
    return settings.resolve_model(slot["fallback_role"])


async def resolve_task_model(task: str) -> str:
    """Effective model for a task: pref when valid, profile default else."""
    slot = TASK_SLOTS.get(task)
    if slot is None:
        raise KeyError(f"Unknown task slot: {task!r}")
    pref = get_prefs().get(task)
    if pref and await _is_valid_model(pref):
        return pref
    return settings.resolve_model(slot["fallback_role"])


async def resolve_chat_request_model(raw_model: str) -> str:
    """Resolve the model of an incoming chat request.

    When the request carries a role ("default"/"chat") the chat slot's
    preference wins over the profile default. Explicit model ids (from
    the composer's model picker) pass through untouched — they are
    already concrete.
    """
    raw = (raw_model or "default").strip()
    if raw in _CHAT_ROLE_ALIASES:
        return await resolve_task_model("chat")
    return settings.resolve_model(raw)


# ─── Mutation (API layer) ───────────────────────────────────────────


async def set_task_model(task: str, model: Optional[str]) -> Dict[str, Any]:
    """Set (or clear with model=None) a task's preferred model.

    Returns the updated task row for the API response. Raises ValueError
    with a user-meaningful message on invalid input.
    """
    if task not in TASK_SLOTS:
        raise ValueError(f"Unknown task: {task!r}")

    if model is None or model == "":
        with _prefs_lock:
            prefs = get_prefs()
            prefs.pop(task, None)
            _save_prefs(prefs)
        return await task_row(task)

    model = model.strip()
    if providers.is_groq_model(model):
        if TASK_SLOTS[task]["local_only"]:
            raise ValueError(
                f"The {TASK_SLOTS[task]['label']} task runs locally — "
                "cloud models are not supported there."
            )
        if providers.get_groq_key() is None:
            raise ValueError("Connect the Groq provider before selecting its models.")
    else:
        # Local model: must be installed in Ollama or a known profile model
        tags = await providers._fetch_ollama_tags()
        installed_ids = {m["id"] for m in tags}
        known_ids = {m.get("id") for m in settings.get_available_models()}
        reachable = providers._tags_cache.get("reachable", False)
        if reachable and model not in (installed_ids | known_ids):
            raise ValueError(
                f"Model {model!r} is not installed in Ollama "
                "(pull it first or pick another model)."
            )

    with _prefs_lock:
        prefs = get_prefs()
        prefs[task] = model
        _save_prefs(prefs)
    return await task_row(task)


async def task_row(task: str) -> Dict[str, Any]:
    """One task's status for the API: current model + default + flags."""
    pref = get_prefs().get(task)
    effective = await resolve_task_model(task)
    default = default_task_model(task)
    return {
        "task": task,
        "label": TASK_SLOTS[task]["label"],
        "model": effective,
        "provider": providers.provider_of(effective),
        "default_model": default,
        "is_default": effective == default,
        "local_only": TASK_SLOTS[task]["local_only"],
    }


async def task_overview() -> List[Dict[str, Any]]:
    """All task slots (current selection + defaults) for the AI tab."""
    return [await task_row(task) for task in TASK_SLOTS]
