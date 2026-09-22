#!/usr/bin/env python3
"""Loopback-only native ASR/TTS service used by the Docker backend."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.services.pip_persistence import activate_persistent_site_packages  # noqa: E402

activate_persistent_site_packages()

# noqa: E402 — FastAPI imports must come after pip_persistence activation #
from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import StreamingResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.config import settings  # noqa: E402
from app.voice.asr import create_asr_engine  # noqa: E402
from app.voice.tts import create_tts_engine  # noqa: E402

cfg = settings.get_voice_config()
# This process is the native endpoint; never select the remote adapters even
# if VOICE_RUNTIME_URL is exported in the caller's shell or root .env.
settings.VOICE_RUNTIME_URL = ""
asr = create_asr_engine(cfg.asr)
tts = create_tts_engine(cfg.tts)
tts_engines = {str(cfg.tts.voice or "mary"): tts}
asr_lock = asyncio.Lock()
voice_lock = asyncio.Lock()
CUSTOM_DIR = ROOT / "data" / "voice-voices"
CUSTOM_DIR.mkdir(parents=True, exist_ok=True)


@contextlib.contextmanager
def online_hub():
    """Temporarily allow an explicit settings-time voice download."""
    saved = os.environ.get("HF_HUB_OFFLINE")
    os.environ["HF_HUB_OFFLINE"] = "0"
    import huggingface_hub.constants as constants
    from huggingface_hub.utils._http import reset_sessions

    saved_constant = constants.HF_HUB_OFFLINE
    constants.HF_HUB_OFFLINE = False
    reset_sessions()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HF_HUB_OFFLINE", None)
        else:
            os.environ["HF_HUB_OFFLINE"] = saved
        constants.HF_HUB_OFFLINE = saved_constant
        reset_sessions()


app = FastAPI(title="RealOpen Native Voice Runtime")


class SpeechRequest(BaseModel):
    text: str
    voice: str | None = None


class VoiceRequest(BaseModel):
    voice: str


def _custom_voices() -> list[dict]:
    result = []
    for meta_path in sorted(CUSTOM_DIR.glob("*.json")):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if (CUSTOM_DIR / f"{meta['id']}.safetensors").exists():
                result.append(meta)
        except (OSError, KeyError, json.JSONDecodeError):
            continue
    return result


def _voice_source(voice: str) -> str:
    if voice.startswith("custom:"):
        ident = voice.removeprefix("custom:")
        path = (CUSTOM_DIR / f"{ident}.safetensors").resolve()
        if path.parent != CUSTOM_DIR.resolve() or not path.exists():
            raise HTTPException(404, "Custom voice not found")
        return str(path)
    return voice


def _tts_for_voice(voice: str):
    source = _voice_source(voice)
    engine = tts_engines.get(source)
    if engine is None:
        values = (
            cfg.tts.model_dump() if hasattr(cfg.tts, "model_dump") else vars(cfg.tts)
        )
        spec = SimpleNamespace(**{**values, "voice": source})
        engine = create_tts_engine(spec)
        tts_engines[source] = engine
    return engine


@app.get("/health")
async def health() -> dict:
    return {"ready": True, "asr": asr.status(), "tts": tts.status()}


@app.post("/v1/warmup/asr")
async def warm_asr() -> dict:
    await asr.warm_up()
    return {"ready": True}


@app.post("/v1/warmup/tts")
async def warm_tts() -> dict:
    await tts.warm_up()
    return {"ready": True}


@app.get("/v1/voices")
async def voices() -> dict:
    return {"custom": _custom_voices()}


@app.post("/v1/voices/prepare")
async def prepare_voice(payload: VoiceRequest) -> dict:
    """Download/cache a catalog voice before it is selected."""
    if payload.voice.startswith("custom:"):
        _voice_source(payload.voice)
        return {"ready": True, "voice": payload.voice}
    engine = _tts_for_voice(payload.voice)
    model = await engine.warm_up()
    try:
        with online_hub():
            await asyncio.to_thread(model.get_state_for_audio_prompt, payload.voice)
    except Exception as exc:
        raise HTTPException(500, f"Could not prepare voice: {exc}") from exc
    return {"ready": True, "voice": payload.voice}


@app.post("/v1/voices")
async def import_voice(request: Request) -> dict:
    data = await request.body()
    if not data.startswith(b"RIFF") or b"WAVE" not in data[:16]:
        raise HTTPException(400, "Invalid WAV file")
    original = request.headers.get("X-Voice-Name", "custom-voice.wav")
    stem = Path(original).stem
    slug = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:48] or "voice"
    ident = slug
    counter = 2
    while (CUSTOM_DIR / f"{ident}.json").exists():
        ident = f"{slug}-{counter}"
        counter += 1
    wav_path = CUSTOM_DIR / f"{ident}.wav"
    state_path = CUSTOM_DIR / f"{ident}.safetensors"
    wav_path.write_bytes(data)
    try:
        with online_hub():
            enable_cloning = getattr(tts, "enable_voice_cloning", None)
            model = (
                await enable_cloning()
                if callable(enable_cloning)
                else await tts.warm_up()
            )
        state = await asyncio.to_thread(
            model.get_state_for_audio_prompt, str(wav_path), truncate=True
        )
        from pocket_tts import export_model_state

        await asyncio.to_thread(export_model_state, state, str(state_path))
    except Exception as exc:
        wav_path.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)
        raise HTTPException(500, f"Voice cloning failed: {exc}") from exc
    meta = {"id": ident, "name": stem[:80], "voice": f"custom:{ident}"}
    (CUSTOM_DIR / f"{ident}.json").write_text(
        json.dumps(meta, indent=2), encoding="utf-8"
    )
    return meta


@app.post("/v1/asr")
async def transcribe(request: Request, language: str = "English") -> dict:
    pcm = await request.body()
    if not pcm:
        raise HTTPException(400, "empty PCM body")
    # The native engines read language from the resolved spec. The query is
    # retained in the protocol so engines with per-call language controls can
    # use it without changing Docker's adapter.
    async with asr_lock:
        await asr.start_stream()
        await asr.feed_audio(pcm)
        text = await asr.finish_stream()
    return {"text": text, "language": language}


@app.post("/v1/tts")
async def synthesize(payload: SpeechRequest) -> StreamingResponse:
    selected = _tts_for_voice(payload.voice or str(cfg.tts.voice or "mary"))

    async def chunks():
        async for pcm in selected.synthesize(payload.text):
            yield pcm

    return StreamingResponse(chunks(), media_type="application/octet-stream")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="info")
