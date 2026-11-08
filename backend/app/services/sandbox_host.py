"""Backend client for the loopback-only host sandbox control plane."""

from __future__ import annotations

import asyncio
import logging
import uuid

import httpx
from urllib.parse import parse_qsl

from app.config import settings
from app.db.models import Sandbox

logger = logging.getLogger(__name__)


def base_url() -> str:
    return (settings.VOICE_RUNTIME_URL or "http://host.docker.internal:8766").rstrip(
        "/"
    )


def payload(sandbox: Sandbox) -> dict:
    return {
        "sandbox_id": str(sandbox.id),
        "volume_name": sandbox.volume_name,
        "container_name": sandbox.container_name,
        "image": sandbox.image,
        "cpu_limit": sandbox.cpu_limit,
        "memory_limit_mb": sandbox.memory_limit_mb,
        "preview_commands": getattr(sandbox, "preview_commands", None) or {},
    }


async def call(
    action: str, sandbox: Sandbox, extra: dict | None = None, timeout: float = 180.0
) -> dict:
    body = payload(sandbox)
    body.update(extra or {})
    if action == "exec":
        body.setdefault("command_id", str(uuid.uuid4()))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                f"{base_url()}/v1/sandboxes/{action}", json=body
            )
            response.raise_for_status()
            return response.json()
    except asyncio.CancelledError:
        if action == "exec":
            try:
                await asyncio.shield(
                    call(
                        "exec/cancel",
                        sandbox,
                        {
                            "command_id": body["command_id"],
                            "command": "cancel",
                            "timeout": 10,
                        },
                        timeout=15,
                    )
                )
            except Exception:
                logger.warning(
                    "Could not cancel interrupted sandbox command", exc_info=True
                )
        raise


async def preview_get(sandbox: Sandbox, port: int, path: str, query: str = ""):
    params = {
        "volume_name": sandbox.volume_name,
        "container_name": sandbox.container_name,
        "image": sandbox.image,
    }
    params.update(
        {
            key: value
            for key, value in parse_qsl(query, keep_blank_values=True)
            if key not in {"volume_name", "container_name", "image"}
        }
    )
    url = f"{base_url()}/v1/sandboxes/preview/{sandbox.id}/{port}/{path}"
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.get(url, params=params)
        return response


def websocket_url(sandbox_id: str) -> str:
    return (
        base_url().replace("http://", "ws://").replace("https://", "wss://")
        + f"/v1/sandboxes/{sandbox_id}/terminal"
    )
