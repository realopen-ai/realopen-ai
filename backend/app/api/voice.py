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
import asyncio
import io
import wave
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, File, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel, Field
import httpx

from app.config import settings
from app.voice import models_store
from app.services import model_prefs, voice_settings

logger = logging.getLogger(__name__)

router = APIRouter()


class ReadAloudRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


@router.post("/voice/speech")
async def read_aloud(request: ReadAloudRequest):
    """Render message audio with the voice-call provider and saved voice."""
    from app.voice.tts import create_tts_engine

    if not settings.VOICE_ENABLED:
        raise HTTPException(503, "Voice is disabled")
    if not request.text.strip():
        raise HTTPException(400, "Speech text is empty")
    selected = voice_settings.get()
    spec = settings.get_voice_config().tts
    engine = None
    try:
        engine = create_tts_engine(SimpleNamespace(
            provider=spec.provider, model=spec.model, voice=selected["voice"],
        ))
        pcm = bytearray()
        async with asyncio.timeout(120):
            async for chunk in engine.synthesize(request.text):
                if len(pcm) + len(chunk) > 32 * 1024 * 1024:
                    raise ValueError("Speech audio limit exceeded")
                pcm.extend(chunk)
        if not pcm or len(pcm) % 2:
            raise ValueError("Invalid speech audio")
        audio = io.BytesIO()
        with wave.open(audio, "wb") as wav:
            wav.setnchannels(1)
            wav.setsampwidth(2)
            wav.setframerate(settings.VOICE_TTS_SAMPLE_RATE)
            wav.writeframes(pcm)
        return Response(audio.getvalue(), media_type="audio/wav", headers={
            "X-Playback-Speed": str(selected["speed"]), "Cache-Control": "no-store",
        })
    except Exception as exc:
        logger.exception("Read aloud synthesis failed")
        raise HTTPException(502, "Speech unavailable") from exc
    finally:
        if engine is not None:
            await engine.cancel()


@router.post("/voice/transcribe")
async def transcribe_dictation(request: Request):
    """One-shot dictation using the same configured ASR as voice calls.

    Input is mono signed 16-bit little-endian PCM at 16 kHz, max 5 minutes.
    No agent loop or TTS is invoked.
    """
    from app.voice.asr import AsrError, create_asr_engine

    if not settings.VOICE_ENABLED:
        raise HTTPException(503, "Voice is disabled")
    pcm = bytearray()
    async for chunk in request.stream():
        if len(pcm) + len(chunk) > 16000 * 2 * 300:
            raise HTTPException(413, "Dictation exceeds 5 minutes")
        pcm.extend(chunk)
    if not pcm or len(pcm) % 2:
        raise HTTPException(400, "Expected nonempty 16 kHz signed 16-bit PCM")
    engine = create_asr_engine(settings.get_voice_config().asr)
    try:
        await engine.start_stream()
        await engine.feed_audio(bytes(pcm))
        return {"text": await engine.finish_stream()}
    except AsrError as exc:
        raise HTTPException(502, exc.message) from exc
    finally:
        await engine.cancel()


class VoiceSettingsRequest(BaseModel):
    voice: Optional[str] = None
    speed: Optional[float] = None
    persona: Optional[str] = None
    custom_personas: Optional[List[Dict[str, Any]]] = None


@router.get("/voice/settings")
async def get_voice_settings():
    return {
        **voice_settings.get(),
        "personas": voice_settings.PERSONAS,
        "model": await model_prefs.task_row("voice"),
    }


@router.put("/voice/settings")
async def put_voice_settings(request: VoiceSettingsRequest):
    try:
        payload = request.model_dump(exclude_none=True)
        if "voice" in payload and settings.VOICE_RUNTIME_URL:
            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(
                    settings.VOICE_RUNTIME_URL.rstrip("/") + "/v1/voices/prepare",
                    json={"voice": payload["voice"]},
                )
                if response.is_error:
                    detail = response.json().get("detail", response.text)
                    raise HTTPException(response.status_code, detail)
        return voice_settings.update(payload)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except HTTPException:
        raise
    except httpx.HTTPError as exc:
        logger.exception("Voice preparation failed")
        raise HTTPException(502, "Could not prepare voice. Check server logs.") from exc


@router.get("/voice/voices")
async def get_voices():
    builtins = [
        "alba",
        "anna",
        "azelma",
        "bill_boerst",
        "caro_davy",
        "charles",
        "cosette",
        "eponine",
        "eve",
        "fantine",
        "george",
        "jane",
        "jean",
        "javert",
        "marius",
        "mary",
        "michael",
        "paul",
        "peter_yearsley",
        "stuart_bell",
        "vera",
    ]
    custom = []
    if settings.VOICE_RUNTIME_URL:
        try:
            async with httpx.AsyncClient(timeout=3.0) as client:
                response = await client.get(
                    settings.VOICE_RUNTIME_URL.rstrip("/") + "/v1/voices"
                )
                response.raise_for_status()
                custom = response.json().get("custom", [])
        except Exception:
            pass
    return {"builtin": builtins, "custom": custom}


@router.post("/voice/voices")
async def upload_voice(file: UploadFile = File(...)):
    if not (file.filename or "").lower().endswith(".wav"):
        raise HTTPException(400, "Voice sample must be a .wav file")
    data = await file.read(20 * 1024 * 1024 + 1)
    if len(data) > 20 * 1024 * 1024:
        raise HTTPException(413, "Voice sample is larger than 20 MB")
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            response = await client.post(
                settings.VOICE_RUNTIME_URL.rstrip("/") + "/v1/voices",
                content=data,
                headers={
                    "Content-Type": "audio/wav",
                    "X-Voice-Name": file.filename or "voice.wav",
                },
            )
            if response.is_error:
                detail = response.json().get("detail", response.text)
                raise HTTPException(response.status_code, detail)
            return response.json()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Voice import failed")
        raise HTTPException(502, "Could not import voice. Check server logs.") from exc


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
    if settings.VOICE_RUNTIME_URL:
        url = settings.VOICE_RUNTIME_URL.rstrip("/")
        try:
            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{url}/health")
                response.raise_for_status()
                native = response.json()
            return {
                "ready": bool(native.get("ready")) and settings.VOICE_ENABLED,
                "enabled": settings.VOICE_ENABLED,
                "asr": native.get("asr") or {"valid": True},
                "tts": native.get("tts") or {"valid": True},
                "missing": [],
                "runtime": "host-native",
            }
        except Exception as exc:
            logger.warning("Host voice runtime unavailable: %s", exc)
            return {
                "ready": False,
                "enabled": settings.VOICE_ENABLED,
                "asr": {"valid": False, "reason": "host_runtime_unavailable"},
                "tts": {"valid": False, "reason": "host_runtime_unavailable"},
                "missing": ["Host voice runtime unavailable. Check server logs."],
                "runtime": "host-native",
            }
    status = models_store.voice_dependency_status()
    asr = status["asr"]
    tts = status["tts"]
    missing = [
        entry
        for entry in (_side_missing("ASR", asr), _side_missing("TTS", tts))
        if entry
    ]
    runtime_missing = [
        f"Runtime: {entry.get('description') or entry.get('id')}"
        for entry in status.get("runtime", [])
    ]
    missing.extend(runtime_missing)
    ready = bool(status["ready"]) and not runtime_missing and status["enabled"]
    return {
        "ready": ready,
        "enabled": bool(status["enabled"]),
        "asr": asr,
        "tts": tts,
        "missing": missing,
    }
