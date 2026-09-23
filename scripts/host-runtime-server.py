#!/usr/bin/env python3
"""Loopback-only host services for voice acceleration and coding sandboxes."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))

from app.services.pip_persistence import activate_persistent_site_packages  # noqa: E402

activate_persistent_site_packages()

# noqa: E402 — FastAPI imports must come after pip_persistence activation #
from fastapi import (  # noqa: E402
    FastAPI,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse  # noqa: E402
import httpx  # noqa: E402
from pydantic import BaseModel  # noqa: E402

from app.config import settings  # noqa: E402
from app.voice.asr import create_asr_engine  # noqa: E402
from app.voice.tts import create_tts_engine  # noqa: E402
from sandbox_runtime import SandboxRuntime, SandboxSpec  # noqa: E402

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


app = FastAPI(title="RealOpen Host Runtime")
sandbox_runtime = None
direct_preview_specs: dict[int, SandboxSpec] = {}


def _sandboxes() -> SandboxRuntime:
    global sandbox_runtime
    if sandbox_runtime is None:
        try:
            sandbox_runtime = SandboxRuntime()
        except Exception as exc:
            raise HTTPException(503, str(exc)) from exc
    return sandbox_runtime


class SpeechRequest(BaseModel):
    text: str
    voice: str | None = None


class VoiceRequest(BaseModel):
    voice: str


class SandboxPayload(BaseModel):
    sandbox_id: str
    volume_name: str
    container_name: str
    image: str = "realopenai-sandbox:latest"
    cpu_limit: float = 2.0
    memory_limit_mb: int = 2048

    def spec(self) -> SandboxSpec:
        return SandboxSpec(
            sandbox_id=self.sandbox_id,
            volume_name=self.volume_name,
            container_name=self.container_name,
            image=self.image,
            cpu_limit=self.cpu_limit,
            memory_limit_mb=self.memory_limit_mb,
        )


class SandboxExecPayload(SandboxPayload):
    command: str
    timeout: int = 120
    command_id: str = "command"


class SandboxFilePayload(SandboxPayload):
    path: str
    content: str | None = None
    content_base64: str | None = None


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
    docker_ready = False
    try:
        docker_ready = _sandboxes().ping()
    except HTTPException:
        pass
    return {
        "ready": True,
        "asr": asr.status(),
        "tts": tts.status(),
        "docker": docker_ready,
    }


@app.post("/v1/sandboxes/create")
async def sandbox_create(payload: SandboxPayload) -> dict:
    return await asyncio.to_thread(_sandboxes().create, payload.spec())


@app.post("/v1/sandboxes/start")
async def sandbox_start(payload: SandboxPayload) -> dict:
    return await asyncio.to_thread(_sandboxes().start, payload.spec())


@app.post("/v1/sandboxes/stop")
async def sandbox_stop(payload: SandboxPayload) -> dict:
    return await asyncio.to_thread(_sandboxes().stop, payload.spec())


@app.post("/v1/sandboxes/restart")
async def sandbox_restart(payload: SandboxPayload) -> dict:
    return await asyncio.to_thread(_sandboxes().restart, payload.spec())


@app.post("/v1/sandboxes/delete")
async def sandbox_delete(payload: SandboxPayload) -> dict:
    await asyncio.to_thread(_sandboxes().delete, payload.spec())
    return {"deleted": True}


@app.post("/v1/sandboxes/status")
async def sandbox_status(payload: SandboxPayload) -> dict:
    return await asyncio.to_thread(_sandboxes().status, payload.spec())


@app.post("/v1/sandboxes/exec")
async def sandbox_exec(payload: SandboxExecPayload) -> dict:
    return await asyncio.to_thread(
        _sandboxes().exec,
        payload.spec(),
        payload.command,
        payload.timeout,
        payload.command_id,
    )


@app.post("/v1/sandboxes/exec/detached")
async def sandbox_exec_detached(payload: SandboxExecPayload) -> dict:
    result = await asyncio.to_thread(
        _sandboxes().exec_detached,
        payload.spec(),
        payload.command,
        payload.command_id,
    )
    match = re.fullmatch(r"preview-(6767|6969)", payload.command_id)
    if match:
        direct_preview_specs[int(match.group(1))] = payload.spec()
    return result


@app.api_route(
    "/v1/sandboxes/preview/{sandbox_id}/{port}/{path:path}",
    methods=["GET", "HEAD"],
)
async def sandbox_preview(sandbox_id: str, port: int, path: str, request: Request):
    spec = SandboxPayload(
        sandbox_id=sandbox_id,
        volume_name=request.query_params["volume_name"],
        container_name=request.query_params["container_name"],
        image=request.query_params.get("image", "realopenai-sandbox:latest"),
    ).spec()
    if port in (6767, 6969):
        direct_preview_specs[port] = spec
    target = await asyncio.to_thread(_sandboxes().preview_target, spec, port)
    url = f"{target}/{path}"
    async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
        response = await client.request(
            request.method,
            url,
            params={
                key: value
                for key, value in request.query_params.multi_items()
                if key not in {"volume_name", "container_name", "image"}
            },
        )
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"content-type", "cache-control", "etag", "last-modified"}
    }
    return StreamingResponse(iter([response.content]), response.status_code, headers)


def _direct_preview_app(port: int) -> FastAPI:
    """Stable localhost gateway for the most recently started service."""
    direct = FastAPI(title=f"RealOpen Sandbox Preview {port}")

    @direct.api_route(
        "/{path:path}",
        methods=["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    )
    async def proxy(path: str, request: Request):
        spec = direct_preview_specs.get(port)
        if spec is None:
            raise HTTPException(503, f"No sandbox service is running on port {port}")
        target = await asyncio.to_thread(_sandboxes().preview_target, spec, port)
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in {"host", "content-length", "connection"}
        }
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.request(
                request.method,
                f"{target}/{path}",
                params=request.query_params,
                headers=headers,
                content=await request.body(),
            )
        forwarded_headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower()
            in {"content-type", "cache-control", "etag", "last-modified", "location"}
        }
        return StreamingResponse(
            iter([response.content]), response.status_code, forwarded_headers
        )

    return direct


@app.post("/v1/sandboxes/exec/cancel")
async def sandbox_exec_cancel(payload: SandboxExecPayload) -> dict:
    return {
        "cancelled": await asyncio.to_thread(
            _sandboxes().cancel, payload.spec(), payload.command_id
        )
    }


@app.post("/v1/sandboxes/usage")
async def sandbox_usage(payload: SandboxPayload) -> dict:
    return {"usage_bytes": await asyncio.to_thread(_sandboxes().usage, payload.spec())}


@app.post("/v1/sandboxes/files")
async def sandbox_files(payload: SandboxFilePayload) -> dict:
    tree = await asyncio.to_thread(
        _sandboxes().list_files, payload.spec(), payload.path
    )
    return {"tree": tree}


@app.post("/v1/sandboxes/files/read")
async def sandbox_file_read(payload: SandboxFilePayload) -> dict:
    data = await asyncio.to_thread(_sandboxes().read_file, payload.spec(), payload.path)
    return {
        "content": data.decode("utf-8", "replace"),
        "content_base64": base64.b64encode(data).decode("ascii"),
    }


@app.post("/v1/sandboxes/files/write")
async def sandbox_file_write(payload: SandboxFilePayload) -> dict:
    data = (
        base64.b64decode(payload.content_base64, validate=True)
        if payload.content_base64 is not None
        else (payload.content or "").encode()
    )
    await asyncio.to_thread(_sandboxes().write_file, payload.spec(), payload.path, data)
    return {"written": len(data)}


@app.websocket("/v1/sandboxes/{sandbox_id}/terminal")
async def sandbox_terminal(websocket: WebSocket, sandbox_id: str):
    await websocket.accept()
    try:
        initial = await websocket.receive_json()
        payload = SandboxPayload(**{**initial, "sandbox_id": sandbox_id})
        sock, exec_id = await asyncio.to_thread(
            _sandboxes().pty_socket,
            payload.spec(),
            int(initial.get("cols", 100)),
            int(initial.get("rows", 30)),
        )
        raw = getattr(sock, "_sock", sock)

        async def docker_to_web():
            while True:
                chunk = await asyncio.to_thread(raw.recv, 8192)
                if not chunk:
                    break
                await websocket.send_bytes(chunk)

        async def web_to_docker():
            while True:
                message = await websocket.receive()
                if message.get("bytes") is not None:
                    await asyncio.to_thread(raw.sendall, message["bytes"])
                elif message.get("text"):
                    event = json.loads(message["text"])
                    if event.get("type") == "resize":
                        await asyncio.to_thread(
                            _sandboxes().client.api.exec_resize,
                            exec_id,
                            height=int(event.get("rows", 30)),
                            width=int(event.get("cols", 100)),
                        )

        tasks = [
            asyncio.create_task(docker_to_web()),
            asyncio.create_task(web_to_docker()),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        raw.close()
    except (WebSocketDisconnect, Exception):
        with contextlib.suppress(Exception):
            await websocket.close()


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

    for direct_port in (6767, 6969):
        threading.Thread(
            target=uvicorn.run,
            args=(_direct_preview_app(direct_port),),
            kwargs={
                "host": "127.0.0.1",
                "port": direct_port,
                "log_level": "warning",
            },
            daemon=True,
            name=f"sandbox-preview-{direct_port}",
        ).start()
    uvicorn.run(app, host="127.0.0.1", port=8766, log_level="info")
