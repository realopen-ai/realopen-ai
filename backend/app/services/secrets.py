"""Secret store for tool/provider credentials.

Values live in files under the persistent state directory with 0600
permissions, never inside database JSON, and are never returned to the
frontend in plaintext (only a masked preview / set flag).

Layout::

    {state_dir}/tool_secrets/{tool_name}/{field}

Example::

    state/tool_secrets/use_websearch/google_api_key

Scope/field names are restricted to a safe charset so they can never
escape the secrets directory.
"""

import logging
import re
from pathlib import Path
from typing import Dict, Optional

from app.config import settings

logger = logging.getLogger(__name__)

_SAFE_NAME = re.compile(r"^[a-z0-9_\-]{1,64}$")


def _state_dir() -> Path:
    return settings._get_state_dir()


def _secret_path(scope: str, field: str) -> Optional[Path]:
    """Path of a secret file, or None when the names are unsafe."""
    if not _SAFE_NAME.match(scope) or not _SAFE_NAME.match(field):
        logger.warning("Unsafe secret scope/field rejected")
        return None
    return _state_dir() / "tool_secrets" / scope / field


def get_secret(scope: str, field: str) -> Optional[str]:
    """Read a stored secret (or None)."""
    path = _secret_path(scope, field)
    if path is None:
        return None
    try:
        if path.exists():
            value = path.read_text(encoding="utf-8").strip()
            return value or None
    except OSError:
        pass
    return None


def set_secret(scope: str, field: str, value: str) -> bool:
    """Persist a secret (called after validation where applicable)."""
    path = _secret_path(scope, field)
    if path is None:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value.strip() + "\n", encoding="utf-8")
        try:  # best-effort permission tightening (same as the Groq key)
            path.chmod(0o600)
        except OSError:
            pass
        return True
    except OSError as e:
        logger.error("Failed to store secret: %s", e)
        return False


def clear_secret(scope: str, field: str) -> bool:
    """Remove a stored secret. Returns True when one existed."""
    path = _secret_path(scope, field)
    if path is None:
        return False
    try:
        if path.exists():
            path.unlink()
            return True
    except OSError:
        pass
    return False


def mask_key(key: str) -> str:
    """Mask a secret for display (same convention as providers._mask_key)."""
    if len(key) <= 10:
        return "•" * max(8, len(key))
    return f"{key[:4]}{'•' * 8}{key[-4:]}"


def secret_status(scope: str, field: str) -> Dict[str, object]:
    """Status of one secret for API responses (never the plaintext)."""
    value = get_secret(scope, field)
    if value is None:
        return {"set": False}
    return {"set": True, "masked": mask_key(value)}
