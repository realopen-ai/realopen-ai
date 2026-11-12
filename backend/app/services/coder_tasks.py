"""Durable coder monitoring and admission checks, independent of browser connections."""

from datetime import datetime, timezone

from sqlalchemy import select, update

from app.db.models import SandboxTask
from app.db.session import async_session_factory

ACTIVE_TASKS = {"queued", "running"}
RESUMABLE_TASKS = {"paused", "cancelled", "failed", "interrupted"}


def task_view(task):
    progress = task.progress or {}
    end = task.completed_at or datetime.utcnow()
    return {
        "id": str(task.id),
        "sandbox_id": str(task.sandbox_id),
        "conversation_id": str(task.conversation_id) if task.conversation_id else None,
        "status": task.status,
        "request": task.request,
        "summary": task.summary,
        "files_changed": task.files_changed,
        "created_at": task.created_at.isoformat() + "Z",
        "completed_at": (
            task.completed_at.isoformat() + "Z" if task.completed_at else None
        ),
        "elapsed_seconds": max(0, (end - task.created_at).total_seconds()),
        "stop_after_round": bool(task.stop_after_round),
        "cancellation_requested": bool(task.cancellation_requested),
        "resumed_from": str(task.resumed_from) if task.resumed_from else None,
        "progress": progress,
        "can_resume": task.status in RESUMABLE_TASKS
        and (task.context_state or {}).get("schema_version") == 2,
        "plan": (task.context_state or {}).get("plan"),
        "verification": ((task.context_state or {}).get("execution") or {}).get(
            "checks", {}
        ),
    }


async def resumable_task(db, task_id, conversation_id):
    task = await db.get(SandboxTask, task_id)
    if not task or task.conversation_id != conversation_id:
        raise ValueError("Coding task not found in this conversation.")
    if task.status not in RESUMABLE_TASKS:
        raise ValueError(
            "Only stopped, failed or interrupted coding tasks can be resumed."
        )
    if (task.context_state or {}).get("schema_version") != 2:
        raise ValueError(
            "This task predates resumable checkpoints. Start a new coding request."
        )
    latest = await db.scalar(
        select(SandboxTask.id)
        .where(SandboxTask.sandbox_id == task.sandbox_id)
        .order_by(SandboxTask.created_at.desc())
        .limit(1)
    )
    if latest != task.id:
        raise ValueError("Resume the latest coding task in this workspace instead.")
    return task


async def recover_interrupted_tasks():
    """A process restart cannot leave stale tasks blocking the workspace forever."""
    async with async_session_factory() as db:
        await db.execute(
            update(SandboxTask)
            .where(SandboxTask.status.in_(ACTIVE_TASKS))
            .values(
                status="interrupted",
                completed_at=datetime.now(timezone.utc).replace(tzinfo=None),
                summary="Backend stopped before this coding run finished. Saved work is retained.",
            )
        )
        await db.commit()
