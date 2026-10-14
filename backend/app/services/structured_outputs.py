"""Best-effort local JSON-schema negotiation, independent of native tools."""

import re

import httpx

from app.config import settings
from app.services import providers

_unsupported = set()


def _key(model):
    return (settings.OLLAMA_BASE_URL.rstrip("/"), model)


def should_try_schema(model):
    return not providers.is_groq_model(model) and _key(model) not in _unsupported


def schema_rejected(error):
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code not in {400, 422, 501}:
            return False
        try:
            message = error.response.text.lower()
        except httpx.ResponseNotRead:
            return False
    elif isinstance(error, providers.ProviderStreamError):
        message = str(error).lower()
    else:
        return False
    return bool(
        re.search(
            r"(?:json.?schema|structured outputs?|response.?format|format)[^\n]{0,80}"
            r"(?:not supported|unsupported|not available)|"
            r"(?:unsupported|does not support)[^\n]{0,80}(?:schema|format)|"
            r"cannot unmarshal object[^\n]{0,100}format[^\n]{0,40}string",
            message,
        )
    )


def remember_rejection(model):
    # No quality, network, or ordinary validation failure disables constraints.
    # Re-probe on process restart so runtime upgrades can restore support.
    _unsupported.add(_key(model))
