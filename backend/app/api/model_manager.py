"""Brain → Models: catalog browsing and host-owned installation control."""

import json
import logging
from uuid import UUID

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.services import providers
from app.services.model_manager.host_api import RemoveRequest
from app.services.model_manager.local_catalog import (
    CHUNK_BYTES,
    LocalPathRequest,
    LocalUploadRequest,
)
from app.services.model_manager.registry import CATALOGS, catalog_adapter
from app.services.model_manager.types import InstallRequest, ModelManagerError
from app.services.model_manager.validation import resolve_reference

router = APIRouter(prefix="/model-manager")
logger = logging.getLogger(__name__)


def host_url():
    return (
        settings.MODEL_RUNTIME_URL
        or settings.VOICE_RUNTIME_URL
        or "http://host.docker.internal:8766"
    ).rstrip("/")


async def host_call(method, path, **kwargs):
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.request(
                method, host_url() + "/v1/models" + path, **kwargs
            )
            if response.status_code == 400:
                detail = response.json().get("detail", "Invalid model operation")
                raise HTTPException(400, detail)
            response.raise_for_status()
            return response.json()
    except (httpx.HTTPError, ValueError):
        raise HTTPException(
            503, "Model manager unavailable. Start or restart the host runtime."
        ) from None


@router.get("")
async def inventory():
    result = await host_call("GET", "")
    await providers._fetch_ollama_tags(force=True)
    return result


@router.get("/catalogs")
async def catalogs():
    return {
        "catalogs": [
            {"id": c.id, "label": c.label}
            for c in CATALOGS.values()
            if getattr(c, "searchable", True)
        ]
    }


@router.get("/search")
async def search(catalog: str, q: str = Query(min_length=1, max_length=200)):
    try:
        rows = await catalog_adapter(catalog).search(q.strip())
        return {"models": [r.model_dump(exclude={"variants"}) for r in rows]}
    except ModelManagerError as exc:
        raise HTTPException(400, exc.message) from None


class InspectRequest(BaseModel):
    catalog: str = Field(max_length=60)
    reference: str = Field(min_length=1, max_length=600)


@router.post("/inspect")
async def inspect(body: InspectRequest):
    try:
        catalog, model, variant, revision = resolve_reference(
            body.reference, body.catalog
        )
        catalog_adapter(catalog)
    except (ModelManagerError, ValueError) as exc:
        message = (
            exc.message if isinstance(exc, ModelManagerError) else "Invalid model link."
        )
        raise HTTPException(400, message) from None
    result = await host_call(
        "GET",
        "/inspect",
        params={
            "catalog": catalog,
            "model": model,
            **({"revision": revision} if revision else {}),
        },
    )
    result["requested_variant"] = variant or (
        model.partition(":")[2] if catalog == "ollama" else None
    )
    return result


@router.get("/jobs")
async def jobs():
    return await host_call("GET", "/jobs")


@router.post("/jobs", status_code=202)
async def create(body: InstallRequest):
    return await host_call("POST", "/jobs", json=body.model_dump())


@router.post("/jobs/{ident}/cancel")
async def cancel(ident: str):
    return await host_call("POST", f"/jobs/{_job_id(ident)}/cancel")


@router.post("/jobs/{ident}/retry", status_code=202)
async def retry(ident: str):
    return await host_call("POST", f"/jobs/{_job_id(ident)}/retry")


def _job_id(value):
    # No user-controlled host paths.
    try:
        return str(UUID(value))
    except ValueError:
        raise HTTPException(422, "Invalid model job ID") from None


@router.post("/remove")
async def remove(body: RemoveRequest):
    result = await host_call("POST", "/remove", json=body.model_dump())
    await providers._fetch_ollama_tags(force=True)
    return result


@router.get("/events")
async def events():
    async def stream():
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(None, connect=10)
            ) as client:
                async with client.stream(
                    "GET", host_url() + "/v1/models/events"
                ) as response:
                    response.raise_for_status()
                    async for chunk in response.aiter_bytes():
                        yield chunk
        except httpx.HTTPError:
            yield "event: unavailable\ndata: " + json.dumps(
                {"message": "Model manager disconnected. Reconnecting…"}
            ) + "\n\n"

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/local/path")
async def inspect_local_path(body: LocalPathRequest):
    return await host_call("POST", "/local/path", json=body.model_dump())


@router.post("/local/uploads", status_code=201)
async def start_local_upload(body: LocalUploadRequest):
    return await host_call("POST", "/local/uploads", json=body.model_dump())


@router.put("/local/uploads/{ident}/file")
async def upload_local_chunk(
    ident: str,
    request: Request,
    path: str = Query(max_length=500),
    offset: int = Query(ge=0),
):
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > CHUNK_BYTES):
        raise HTTPException(413, "Model upload chunk exceeds the allowed size")
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(120, connect=10)) as client:
            response = await client.put(
                host_url() + f"/v1/models/local/uploads/{_job_id(ident)}/file",
                params={"path": path, "offset": offset},
                content=request.stream(),
                headers={
                    "Content-Type": "application/octet-stream",
                    **({"Content-Length": length} if length else {}),
                },
            )
            if response.status_code == 400:
                raise HTTPException(
                    400, response.json().get("detail", "Invalid model upload")
                )
            response.raise_for_status()
            return response.json()
    except httpx.HTTPError:
        raise HTTPException(
            503, "Model upload disconnected. Check the host runtime and retry."
        ) from None


@router.post("/local/uploads/{ident}/finish")
async def finish_local_upload(ident: str):
    return await host_call("POST", f"/local/uploads/{_job_id(ident)}/finish")


@router.delete("/local/uploads/{ident}")
async def discard_local_upload(ident: str):
    return await host_call("DELETE", f"/local/uploads/{_job_id(ident)}")
