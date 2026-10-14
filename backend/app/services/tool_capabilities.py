"""Remember native-tool protocol rejections, scoped to model and endpoint.

Unknown models are tried natively. Only explicit unsupported-tool rejections
are remembered, never parser, execution, or connectivity failures.
The existing persistent state volume holds atomic, per-model JSON records.
"""

import asyncio
import hashlib
import json
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import settings
from app.services import providers, agent_settings

logger = logging.getLogger(__name__)
_fallbacks: set[Path] = set()
_REASONS = {"unsupported_tools"}


def _directory() -> Path:
    return providers._state_dir() / "tool_capabilities"


def _path(model: str) -> Path:
    provider = providers.provider_of(model)
    endpoint = (
        providers.GROQ_BASE_URL if provider == "groq" else settings.OLLAMA_BASE_URL
    )
    # Hash endpoints too: URLs may contain credentials. Never persist them.
    key = hashlib.sha256(
        json.dumps([provider, endpoint.rstrip("/"), model]).encode()
    ).hexdigest()
    return _directory() / f"{key}.json"


def should_try_native(model: str) -> bool:
    if agent_settings.effective().tool_calling == "text":
        return False
    try:
        path = _path(model)
        if path in _fallbacks:
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        return not (
            isinstance(data, dict)
            and data.get("schema_version") == 1
            and data.get("protocol") == "text"
            and data.get("reason") in _REASONS
        )
    except FileNotFoundError:
        return True
    except (OSError, ValueError):
        logger.warning("Could not read tool capability state; trying native tools")
        return True


def _provider_error_message(error: Exception) -> str | None:
    """Read provider diagnostics, never failed_generation/model output."""
    if isinstance(error, httpx.HTTPStatusError):
        if error.response.status_code not in {400, 422, 500, 501}:
            return None
        try:
            message = error.response.text
            try:
                payload = error.response.json()
                detail = (
                    payload.get("error", payload) if isinstance(payload, dict) else None
                )
                if isinstance(detail, dict):
                    message = str(detail.get("message", ""))
                elif isinstance(detail, str):
                    message = detail
            except ValueError:
                pass
        except httpx.ResponseNotRead:
            return None
    elif isinstance(error, providers.ProviderStreamError):
        message = str(error)
    else:
        return None
    return message.lower()


def rejection_reason(error: Exception) -> str | None:
    """Only explicit lack of native-tool support warrants a durable fallback."""
    message = _provider_error_message(error)
    if message is None:
        return None
    if re.search(
        r"(?:does not|doesn't|cannot|can't)\s+support\s+(?:native\s+)?(?:tools|tool calling|function calling)"
        r"|unsupported\s+(?:native\s+)?(?:tools|tool calling|function calling)"
        r"|(?:tools|tool calling|function calling)\s+(?:(?:is|are)\s+)?(?:currently\s+)?(?:not supported|unsupported)",
        message,
    ):
        # Reject schema/format limitations, not the model's tool capability.
        if re.search(
            r"\b(?:schema|format|arguments?|parameters?|configuration)\b", message
        ):
            return None
        return "unsupported_tools"
    return None


def native_parser_error(error: Exception) -> bool:
    """Transient malformed tool output, distinct from unsupported tools.

    Callers use this only for an inference attempt with native tools enabled.
    Generic XML/JSON/configuration errors must not trigger protocol recovery.
    """
    message = _provider_error_message(error)
    if message is None:
        return False
    if re.search(r"\b(?:schema|configuration|manifest)\b", message):
        return False
    if re.search(
        r"(?:failed|unable|error)[^\n]{0,40}pars(?:e|ing)[^\n]{0,40}(?:tool|function)[ _-]?call"
        r"|(?:tool|function)[ _-]?call[^\n]{0,40}pars(?:e|ing)[^\n]{0,20}(?:error|failed)",
        message,
    ):
        return True
    xml_error = re.search(
        r"xml\s+(?:syntax|parse|parsing)\s+error|(?:invalid|malformed)\s+xml", message
    )
    tool_tag = re.search(r"</?(?:function|parameter|tool_call)(?:[\s=>])", message)
    json_error = re.search(
        r"(?:invalid|malformed)\s+json|json\s+(?:syntax|parse|parsing)\s+error", message
    )
    tool_context = re.search(
        r"(?:tool|function)[ _-]?(?:call|arguments)|\barguments\b", message
    )
    return bool((xml_error and tool_tag) or (json_error and tool_context))


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as file:
            temporary = Path(file.name)
            json.dump(data, file, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


async def remember_fallback(model: str, reason: str) -> None:
    if reason not in _REASONS:
        raise ValueError("Only explicit unsupported-tools rejections may be remembered")
    try:
        path = _path(model)
        _fallbacks.add(path)
        await asyncio.to_thread(
            _write,
            path,
            {
                "schema_version": 1,
                "model": model,
                "provider": providers.provider_of(model),
                "protocol": "text",
                "reason": reason,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
    except OSError:
        # Keep the in-process decision even if durable storage is unavailable.
        logger.warning(
            "Could not persist tool capability state; fallback remains active for this process"
        )


def reset(model: str) -> None:
    """Re-probe this model after a model/runtime upgrade."""
    path = _path(model)
    _fallbacks.discard(path)
    path.unlink(missing_ok=True)
