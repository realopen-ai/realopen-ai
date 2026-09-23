from __future__ import annotations

import asyncio
import base64
import json
import uuid
from datetime import datetime

import websockets
from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Sandbox, SandboxCommand, SandboxTask
from app.db.session import get_db
from app.services import sandbox_host
from app.services.sandbox_manager import provision_sandbox

router = APIRouter(prefix="/sandboxes", tags=["sandboxes"])


class CreateSandbox(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    cpu_limit: float = Field(default=2.0, ge=0.25, le=8)
    memory_limit_mb: int = Field(default=2048, ge=256, le=16384)
    workspace_quota_bytes: int = Field(
        default=2 * 1024**3, ge=64 * 1024**2, le=50 * 1024**3
    )
    idle_timeout_seconds: int = Field(default=1800, ge=60, le=86400)
    conversation_id: uuid.UUID | None = None


class ExecRequest(BaseModel):
    command: str = Field(min_length=1, max_length=32_000)
    timeout: int = Field(default=120, ge=1, le=1800)
    command_id: str | None = Field(default=None, max_length=80)


class FileWrite(BaseModel):
    path: str
    content: str


def as_dict(item: Sandbox) -> dict:
    return {
        "id": str(item.id),
        "name": item.name,
        "status": item.status,
        "desired_running": item.desired_running,
        "image": item.image,
        "cpu_limit": item.cpu_limit,
        "memory_limit_mb": item.memory_limit_mb,
        "workspace_quota_bytes": item.workspace_quota_bytes,
        "usage_bytes": item.usage_bytes,
        "idle_timeout_seconds": item.idle_timeout_seconds,
        "last_active_at": (
            item.last_active_at.isoformat() if item.last_active_at else None
        ),
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "error": item.error,
    }


async def get_one(db: AsyncSession, sandbox_id: uuid.UUID) -> Sandbox:
    item = await db.get(Sandbox, sandbox_id)
    if not item:
        raise HTTPException(404, "Sandbox not found")
    return item


async def touch(db: AsyncSession, item: Sandbox) -> None:
    item.last_active_at = datetime.utcnow()
    await db.flush()


@router.get("")
async def list_sandboxes(db: AsyncSession = Depends(get_db)):
    rows = (
        (await db.execute(select(Sandbox).order_by(Sandbox.updated_at.desc())))
        .scalars()
        .all()
    )
    return {"sandboxes": [as_dict(row) for row in rows]}


@router.post("")
async def create_sandbox(body: CreateSandbox, db: AsyncSession = Depends(get_db)):
    conversation = None
    if body.conversation_id:
        conversation = await db.get(Conversation, body.conversation_id)
        if not conversation:
            raise HTTPException(404, "Conversation not found")
    try:
        item = await provision_sandbox(
            db,
            name=body.name,
            conversation=conversation,
            cpu_limit=body.cpu_limit,
            memory_limit_mb=body.memory_limit_mb,
            workspace_quota_bytes=body.workspace_quota_bytes,
            idle_timeout_seconds=body.idle_timeout_seconds,
        )
    except Exception as exc:
        raise HTTPException(503, f"Host sandbox runtime failed: {exc}") from exc
    return as_dict(item)


@router.get("/{sandbox_id}")
async def get_sandbox(sandbox_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    item = await get_one(db, sandbox_id)
    try:
        runtime = await sandbox_host.call("status", item, timeout=15)
        item.status = runtime["status"]
        item.error = None
    except Exception as exc:
        item.status, item.error = "error", str(exc)
    return as_dict(item)


async def _lifecycle(sandbox_id: uuid.UUID, action: str, db: AsyncSession):
    item = await get_one(db, sandbox_id)
    try:
        result = await sandbox_host.call(action, item)
    except Exception as exc:
        item.status, item.error = "error", str(exc)
        raise HTTPException(503, str(exc)) from exc
    item.status = result["status"]
    item.desired_running = action != "stop"
    item.error = None
    await touch(db, item)
    return as_dict(item)


@router.post("/{sandbox_id}/start")
async def start_sandbox(sandbox_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _lifecycle(sandbox_id, "start", db)


@router.post("/{sandbox_id}/stop")
async def stop_sandbox(sandbox_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _lifecycle(sandbox_id, "stop", db)


@router.post("/{sandbox_id}/restart")
async def restart_sandbox(sandbox_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    return await _lifecycle(sandbox_id, "restart", db)


@router.delete("/{sandbox_id}")
async def delete_sandbox(sandbox_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    item = await get_one(db, sandbox_id)
    try:
        await sandbox_host.call("delete", item)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    await db.delete(item)
    return {"deleted": True}


@router.put("/{sandbox_id}/link/{conversation_id}")
async def link_conversation(
    sandbox_id: uuid.UUID,
    conversation_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
):
    await get_one(db, sandbox_id)
    conversation = await db.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    conversation.sandbox_id = sandbox_id
    return {
        "linked": True,
        "sandbox_id": str(sandbox_id),
        "conversation_id": str(conversation_id),
    }


@router.delete("/link/{conversation_id}")
async def unlink_conversation(
    conversation_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    conversation = await db.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    conversation.sandbox_id = None
    return {"linked": False}


@router.get("/conversation/{conversation_id}")
async def conversation_sandbox(
    conversation_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    conversation = await db.get(Conversation, conversation_id)
    if not conversation:
        raise HTTPException(404, "Conversation not found")
    return {
        "sandbox": (
            as_dict(await get_one(db, conversation.sandbox_id))
            if conversation.sandbox_id
            else None
        )
    }


@router.post("/{sandbox_id}/exec")
async def execute(
    sandbox_id: uuid.UUID, body: ExecRequest, db: AsyncSession = Depends(get_db)
):
    item = await get_one(db, sandbox_id)
    command_id = body.command_id or str(uuid.uuid4())
    result = await sandbox_host.call(
        "exec",
        item,
        {**body.model_dump(exclude_none=True), "command_id": command_id},
        timeout=body.timeout + 20,
    )
    usage = await sandbox_host.call("usage", item)
    item.usage_bytes = usage["usage_bytes"]
    await touch(db, item)
    result["usage_bytes"] = item.usage_bytes
    result["quota_exceeded"] = item.usage_bytes > item.workspace_quota_bytes
    return result


@router.get("/{sandbox_id}/commands")
async def command_history(
    sandbox_id: uuid.UUID,
    limit: int = Query(default=500, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
):
    await get_one(db, sandbox_id)
    rows = (
        (
            await db.execute(
                select(SandboxCommand)
                .where(SandboxCommand.sandbox_id == sandbox_id)
                .order_by(SandboxCommand.started_at.desc(), SandboxCommand.id.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    rows.reverse()
    return {
        "commands": [
            {
                "id": str(row.id),
                "task_id": str(row.task_id) if row.task_id else None,
                "conversation_id": (
                    str(row.conversation_id) if row.conversation_id else None
                ),
                "source": row.source,
                "tool_name": row.tool_name,
                "sequence": row.sequence,
                "command": row.command,
                "cwd": row.cwd,
                "stdout": row.stdout,
                "stderr": row.stderr,
                "exit_code": row.exit_code,
                "output_truncated": row.output_truncated,
                "started_at": row.started_at.isoformat() if row.started_at else None,
                "completed_at": (
                    row.completed_at.isoformat() if row.completed_at else None
                ),
                "duration_ms": row.duration_ms,
            }
            for row in rows
        ]
    }


@router.post("/{sandbox_id}/exec/{command_id}/cancel")
async def cancel_command(
    sandbox_id: uuid.UUID, command_id: str, db: AsyncSession = Depends(get_db)
):
    item = await get_one(db, sandbox_id)
    return await sandbox_host.call(
        "exec/cancel",
        item,
        {"command": "cancel", "command_id": command_id, "timeout": 10},
    )


@router.get("/{sandbox_id}/files")
async def files(
    sandbox_id: uuid.UUID,
    path: str = Query("/workspace"),
    db: AsyncSession = Depends(get_db),
):
    item = await get_one(db, sandbox_id)
    return await sandbox_host.call("files", item, {"path": path})


@router.get("/{sandbox_id}/file")
async def read_file(
    sandbox_id: uuid.UUID, path: str, db: AsyncSession = Depends(get_db)
):
    item = await get_one(db, sandbox_id)
    return await sandbox_host.call("files/read", item, {"path": path})


@router.put("/{sandbox_id}/file")
async def write_file(
    sandbox_id: uuid.UUID, body: FileWrite, db: AsyncSession = Depends(get_db)
):
    item = await get_one(db, sandbox_id)
    usage = await sandbox_host.call("usage", item)
    item.usage_bytes = usage["usage_bytes"]
    current = await sandbox_host.call("files/stat", item, {"path": body.path})
    replacement_size = len(body.content.encode())
    projected_usage = max(0, item.usage_bytes - current["size"]) + replacement_size
    if projected_usage > item.workspace_quota_bytes:
        raise HTTPException(413, "Workspace quota exceeded")
    result = await sandbox_host.call("files/write", item, body.model_dump())
    await touch(db, item)
    return result


@router.post("/{sandbox_id}/upload")
async def upload_file(
    sandbox_id: uuid.UUID,
    path: str = Query("/workspace"),
    upload: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
):
    item = await get_one(db, sandbox_id)
    data = await upload.read(25 * 1024**2 + 1)
    if len(data) > 25 * 1024**2:
        raise HTTPException(413, "Upload exceeds the 25 MB limit")
    filename = (upload.filename or "upload.bin").replace("/", "_")
    target = path.rstrip("/") + "/" + filename
    usage = await sandbox_host.call("usage", item)
    current = await sandbox_host.call("files/stat", item, {"path": target})
    projected_usage = max(0, usage["usage_bytes"] - current["size"]) + len(data)
    if projected_usage > item.workspace_quota_bytes:
        raise HTTPException(413, "Workspace quota exceeded")
    result = await sandbox_host.call(
        "files/write",
        item,
        {"path": target, "content_base64": base64.b64encode(data).decode("ascii")},
    )
    await touch(db, item)
    return {**result, "path": target}


@router.get("/{sandbox_id}/download")
async def download_file(
    sandbox_id: uuid.UUID, path: str, db: AsyncSession = Depends(get_db)
):
    item = await get_one(db, sandbox_id)
    result = await sandbox_host.call("files/read", item, {"path": path})
    data = base64.b64decode(result["content_base64"])
    filename = path.rsplit("/", 1)[-1] or "download.bin"
    return Response(
        data,
        media_type="application/octet-stream",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/{sandbox_id}/preview/{port}/{path:path}")
async def preview(
    sandbox_id: uuid.UUID,
    port: int,
    path: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    item = await get_one(db, sandbox_id)
    response = await sandbox_host.preview_get(item, port, path, request.url.query)
    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() in {"content-type", "cache-control", "etag", "last-modified"}
    }
    return Response(response.content, response.status_code, headers=headers)


@router.get("/{sandbox_id}/previews")
async def discover_previews(
    sandbox_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    item = await get_one(db, sandbox_id)
    try:
        runtime = await sandbox_host.call("status", item, timeout=10)
    except Exception:
        return {"available": [], "preferred": None}
    if not runtime.get("running"):
        return {"available": [], "preferred": None}

    async def probe(port: int) -> dict | None:
        try:
            response = await asyncio.wait_for(
                sandbox_host.preview_get(item, port, ""), timeout=3
            )
            # API roots commonly return 404 while the service is healthy.
            if response.status_code < 500:
                return {
                    "port": port,
                    "url": f"/api/sandboxes/{item.id}/preview/{port}/",
                    "host_url": f"http://localhost:{port}",
                }
        except Exception:
            pass
        return None

    results = await asyncio.gather(probe(6767), probe(6969))
    available = [result for result in results if result is not None]
    preferred = next(
        (result for result in available if result["port"] == 6767),
        available[0] if available else None,
    )
    return {"available": available, "preferred": preferred}


@router.get("/{sandbox_id}/tasks")
async def tasks(sandbox_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    rows = (
        (
            await db.execute(
                select(SandboxTask)
                .where(SandboxTask.sandbox_id == sandbox_id)
                .order_by(SandboxTask.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    return {
        "tasks": [
            {
                "id": str(x.id),
                "status": x.status,
                "request": x.request,
                "summary": x.summary,
                "worklog": x.worklog,
                "files_changed": x.files_changed,
                "tests_run": x.tests_run,
                "created_at": x.created_at.isoformat(),
            }
            for x in rows
        ]
    }


@router.post("/{sandbox_id}/tasks/{task_id}/cancel")
async def cancel_task(
    sandbox_id: uuid.UUID, task_id: uuid.UUID, db: AsyncSession = Depends(get_db)
):
    task = await db.get(SandboxTask, task_id)
    if not task or task.sandbox_id != sandbox_id:
        raise HTTPException(404, "Task not found")
    task.cancellation_requested = True
    return {"cancellation_requested": True}


async def terminal_proxy(websocket: WebSocket, sandbox_id: str, db: AsyncSession):
    try:
        ident = uuid.UUID(sandbox_id)
        item = await get_one(db, ident)
    except Exception:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    try:
        async with websockets.connect(
            sandbox_host.websocket_url(sandbox_id), max_size=2**20
        ) as host:
            initial = await websocket.receive_text()
            config = json.loads(initial)
            await host.send(json.dumps({**sandbox_host.payload(item), **config}))

            async def client_to_host():
                while True:
                    event = await websocket.receive()
                    if event.get("bytes") is not None:
                        await host.send(event["bytes"])
                    elif event.get("text") is not None:
                        await host.send(event["text"])

            async def host_to_client():
                async for message in host:
                    if isinstance(message, bytes):
                        await websocket.send_bytes(message)
                    else:
                        await websocket.send_text(message)

            running = [
                asyncio.create_task(client_to_host()),
                asyncio.create_task(host_to_client()),
            ]
            _, pending = await asyncio.wait(
                running, return_when=asyncio.FIRST_COMPLETED
            )
            for job in pending:
                job.cancel()
    except (WebSocketDisconnect, Exception):
        pass
