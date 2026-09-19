"""Voice chat REST endpoints — runtime readiness for the frontend mic button.

The frontend voiceStore (frontend/src/voice/voiceStore.ts) calls
``GET /api/voice/status`` as its CANONICAL readiness probe and parses the
response defensively:

    { "ready": bool, "enabled": bool, "asr": bool|obj, "tts": bool|obj,
      "missing": string[] }

where the per-side objects carry ``installed`` / ``valid`` booleans. The
full per-side dicts from ``app.voice.models_store`` are returned (the
tooltip can be precise); ``missing`` holds human-readable entries
("ASR: <model> (<reason>)", "TTS: …", "Runtime: <display>") mirroring the
shape the setup-wizard fallback path produces.

``ready`` is the FULL gate: both models installed AND valid for the
current profiles.yml selection, the runtime pip packages importable, and
VOICE_ENABLED on — exactly the conditions the /ws/voice ``start``
handshake enforces (app/voice/session.py), so the mic button never arms a
session that would immediately be rejected.

The heavier install-time view (download plan, sizes, wizard rows) stays in
GET /api/setup/status → ``voice`` summary (app/services/voice_model_installer).
"""

import logging
from typing import List, Optional

from fastapi import APIRouter

from app.voice import models_store

logger = logging.getLogger(__name__)

router = APIRouter()


def _side_missing(label: str, side: dict) -> Optional[str]:
    """One human-readable missing entry for a side (None when ready)."""
    if side.get("valid"):
        return None
    model = side.get("model")
    reason = side.get("reason") or "not_ready"
    if model:
        return f"{label}: {model} ({reason})"
    return f"{label}: {reason}"


def _runtime_missing() -> List[str]:
    """Missing voice runtime pip packages (setup-side probe, guarded).

    Uses the installer's find_spec-based probe — cheap, no model loading.
    """
    try:
        from app.services import voice_model_installer

        return [
            f"Runtime: {entry.get('description') or entry.get('id')}"
            for entry in voice_model_installer.voice_runtime_entries()
        ]
    except Exception as e:  # noqa: BLE001 — optional integration
        logger.debug("voice runtime probe unavailable: %s", e)
        return []


@router.get("/voice/status")
async def get_voice_status():
    """Voice pipeline readiness (the mic button's gate)."""
    status = models_store.voice_dependency_status()
    asr = status["asr"]
    tts = status["tts"]
    missing = [
        entry
        for entry in (_side_missing("ASR", asr), _side_missing("TTS", tts))
        if entry
    ]
    runtime_missing = _runtime_missing()
    missing.extend(runtime_missing)
    ready = bool(status["ready"]) and not runtime_missing and status["enabled"]
    return {
        "ready": ready,
        "enabled": bool(status["enabled"]),
        "asr": asr,
        "tts": tts,
        "missing": missing,
    }
