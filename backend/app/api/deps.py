"""
Dependencies API — list, check, install, and uninstall optional system dependencies.

Endpoints:
  GET    /api/deps                  — list all dependencies with status
  GET    /api/deps/{name}/status    — check a single dependency
  POST   /api/deps/{name}/install   — install a dependency (SSE stream)
  DELETE /api/deps/{name}/uninstall — uninstall a dependency (SSE stream)
"""

import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from app.services.deps_manager import (
    get_catalog,
    get_dependency,
    is_installed,
    get_version,
    install_dependency,
    uninstall_dependency,
)

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/deps")
async def list_dependencies():
    """List all optional dependencies with their installation status."""
    return {"dependencies": get_catalog()}


@router.get("/deps/{name}/status")
async def check_dependency(name: str):
    """Check if a single dependency is installed."""
    dep = get_dependency(name)
    if not dep:
        raise HTTPException(404, f"Unknown dependency: {name}")
    return {
        "name": dep.name,
        "installed": is_installed(dep),
        "version": get_version(dep) if is_installed(dep) else None,
    }


@router.post("/deps/{name}/install")
async def install_dep(name: str):
    """Install a dependency. Returns an SSE stream with progress events.

    Events:
      data: {"stage": "updating", "output": "..."}
      data: {"stage": "downloading", "output": "..."}
      data: {"stage": "extracting", "output": "..."}
      data: {"stage": "linking", "output": "..."}
      data: {"stage": "done", "exit_code": 0, "version": "..."}
      data: {"stage": "error", "error": "..."}
    """
    dep = get_dependency(name)
    if not dep:
        raise HTTPException(404, f"Unknown dependency: {name}")

    async def generate():
        try:
            async for event in install_dependency(dep):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            logger.exception("Install failed: %s", e)
            yield f"data: {json.dumps({'stage': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.delete("/deps/{name}/uninstall")
async def uninstall_dep(name: str):
    """Uninstall a dependency. Returns an SSE stream with progress events.

    Fully removes the dependency from BOTH Docker volumes:
      - The overlay volume (/opt/optional) — extracted files
      - The apt cache volume (/var/cache/apt) — downloaded .deb files

    This ensures the dependency stays uninstalled even after a container
    rebuild (no cached .debs to re-extract from, no manifest entry).

    Events:
      data: {"stage": "uninstalling", "output": "..."}
      data: {"stage": "done", "exit_code": 0, "output": "..."}
      data: {"stage": "error", "error": "..."}
    """
    dep = get_dependency(name)
    if not dep:
        raise HTTPException(404, f"Unknown dependency: {name}")

    async def generate():
        try:
            async for event in uninstall_dependency(dep):
                yield f"data: {json.dumps(event)}\n\n"
        except Exception as e:
            logger.exception("Uninstall failed: %s", e)
            yield f"data: {json.dumps({'stage': 'error', 'error': str(e)})}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
