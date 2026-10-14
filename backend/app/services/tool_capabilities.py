"""Remember native-tool protocol rejections, scoped to model and endpoint.

Unknown models are tried natively. Only explicit protocol rejections are
remembered, never inference quality, tool execution, or connectivity failures.
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
_REASONS = {"unsupported_tools", "native_parser"}


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


def rejection_reason(error: Exception) -> str | None:
    """Recognize tool protocol errors, not arbitrary 4xx/5xx failures."""
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
    message = message.lower()
    if re.search(
        r"(?:does not|doesn't|cannot|can't)\s+support\s+(?:native\s+)?(?:tools|tool calling|function calling)"
        r"|unsupported\s+(?:native\s+)?(?:tools|tool calling|function calling)"
        r"|(?:tools|tool calling|function calling)\s+(?:(?:is|are)\s+)?(?:currently\s+)?(?:not supported|unsupported|not available|not enabled)",
        message,
    ):
        return "unsupported_tools"
    if re.search(
        r"(?:failed|unable|error)[^\n]{0,40}pars(?:e|ing)[^\n]{0,40}(?:tool|function)[ _-]?call"
        r"|(?:tool|function)[ _-]?call[^\n]{0,40}pars(?:e|ing)[^\n]{0,20}(?:error|failed)",
        message,
    ):
        return "native_parser"
    return None


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
        raise ValueError(
            "Only confirmed native-tool protocol failures may be remembered"
        )
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
