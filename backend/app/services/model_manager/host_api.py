"""Host control plane, isolated from catalog browsing and frontend navigation."""

import asyncio
import json
import logging

import httpx
from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from .jobs import ModelJobs
from .local_catalog import LocalPathRequest, LocalUploadRequest
from .types import InstallRequest, ModelManagerError

logger = logging.getLogger(__name__)


class RemoveRequest(BaseModel):
    runtime: str = Field(max_length=60)
    name: str = Field(min_length=1, max_length=200)


def model_host_router(manager: ModelJobs):
    router = APIRouter(prefix="/v1/models")

    async def call(operation, *args):
        try:
            return await operation(*args)
        except ModelManagerError as exc:
            raise HTTPException(400, exc.message) from None
        except (httpx.HTTPError, ValueError):
            logger.exception("Model runtime request failed")
            raise HTTPException(
                503, "Model runtime unavailable. Check host runtime logs."
            ) from None

    @router.get("")
    async def inventory():
        return await call(manager.inventory)

    @router.get("/inspect")
    async def inspect(catalog: str, model: str, revision: str | None = None):
        return await call(manager.inspect, catalog, model, revision)

    @router.get("/jobs")
    async def jobs():
        return {"jobs": manager.snapshot()}

    @router.post("/jobs", status_code=202)
    async def create(body: InstallRequest):
        return await call(manager.create, body)

    @router.post("/jobs/{ident}/cancel")
    async def cancel(ident: str):
        return await call(manager.cancel, ident)

    @router.post("/jobs/{ident}/retry", status_code=202)
    async def retry(ident: str):
        return await call(manager.retry, ident)

    @router.post("/remove")
    async def remove(body: RemoveRequest):
        await call(manager.remove, body.runtime, body.name)
        return {"removed": True}

    @router.post("/local/path")
    async def local_path(body: LocalPathRequest):
        source = manager.catalog("local")
        ident = await call(source.prepare_path, body.path, body.format_hint)
        return await call(manager.inspect, "local", ident)

    @router.post("/local/uploads", status_code=201)
    async def local_upload(body: LocalUploadRequest):
        return await call(manager.catalog("local").begin_upload, body)

    @router.put("/local/uploads/{ident}/file")
    async def local_chunk(
        ident: str,
        request: Request,
        path: str = Query(max_length=500),
        offset: int = Query(ge=0),
    ):
        if (
            request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            != "application/octet-stream"
        ):
            raise HTTPException(
                415, "Send model file bytes as application/octet-stream"
            )
        return await call(
            manager.catalog("local").upload_chunk, ident, path, offset, request.stream()
        )

    @router.post("/local/uploads/{ident}/finish")
    async def local_finish(ident: str):
        source = manager.catalog("local")
        await call(source.finish_upload, ident)
        return await call(manager.inspect, "local", ident)

    @router.delete("/local/uploads/{ident}")
    async def local_discard(ident: str):
        if any(
            r["request"]["catalog"] == "local" and r["request"]["model"] == ident
            for r in manager.records.values()
        ):
            raise HTTPException(
                400,
                "This upload is referenced by an import job and is retained for retry. Cancel the job to stop importing.",
            )
        await call(manager.catalog("local").discard, ident)
        return {"discarded": True}

    @router.get("/events")
    async def events():
        async def stream():
            last = None
            heartbeat = 0
            while True:
                snapshot = json.dumps({"jobs": manager.snapshot()}, ensure_ascii=False)
                if snapshot != last:
                    yield f"data: {snapshot}\n\n"
                    last = snapshot
                elif heartbeat % 15 == 0:
                    yield ": heartbeat\n\n"
                heartbeat += 1
                await asyncio.sleep(1)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
