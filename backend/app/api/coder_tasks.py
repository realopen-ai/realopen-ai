"""Conversation-scoped coder task monitoring and explicit user controls."""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import SandboxTask
from app.db.session import get_db
from app.services.chat_streams import stop_stream
from app.services.coder_tasks import ACTIVE_TASKS, task_view

router = APIRouter(prefix="/coder/tasks", tags=["coder"])


@router.get("")
async def list_tasks(
    conversation_id: uuid.UUID = Query(...), db: AsyncSession = Depends(get_db)
):
    rows = (
        (
            await db.execute(
                select(SandboxTask)
                .where(SandboxTask.conversation_id == conversation_id)
                .order_by(SandboxTask.created_at.desc())
                .limit(1)
            )
        )
        .scalars()
        .all()
    )
    return {"tasks": [task_view(task) for task in rows]}


@router.post("/{task_id}/{action}")
async def control_task(
    task_id: uuid.UUID,
    action: str,
    conversation_id: uuid.UUID = Query(...),
    db: AsyncSession = Depends(get_db),
):
    if action not in {"stop-after-round", "cancel"}:
        raise HTTPException(404, "Unknown task action")
    task = await db.scalar(
        select(SandboxTask)
        .where(
            SandboxTask.id == task_id, SandboxTask.conversation_id == conversation_id
        )
        .with_for_update()
    )
    if not task:
        raise HTTPException(404, "Coding task not found")
    if task.status not in ACTIVE_TASKS:
        raise HTTPException(409, "Coding task is no longer running")
    if action == "stop-after-round":
        task.stop_after_round = True
    else:
        task.cancellation_requested = True
    # Commit before notifying the execution; the next round reads these flags.
    await db.commit()
    if action == "cancel":
        await stop_stream(str(conversation_id))
    return task_view(task)
