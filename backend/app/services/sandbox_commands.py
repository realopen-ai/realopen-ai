"""Persistence helpers for bounded sandbox command history."""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from app.db.models import Conversation, SandboxCommand
from app.db.session import async_session_factory

MAX_STREAM_CHARS = 250_000


def split_command_output(output: str, exit_code: int | None) -> tuple[str, str]:
    """Extract stdout/stderr from a sandbox exec envelope."""
    try:
        payload = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return (output, "") if exit_code in (None, 0) else ("", output)
    if not isinstance(payload, dict) or not ({"stdout", "stderr"} & payload.keys()):
        return (output, "") if exit_code in (None, 0) else ("", output)
    return str(payload.get("stdout") or ""), str(payload.get("stderr") or "")


async def persist_command(
    *,
    sandbox_id: uuid.UUID,
    conversation_id: uuid.UUID | None,
    source: str,
    tool_name: str,
    command: str,
    stdout: str = "",
    stderr: str = "",
    exit_code: int | None = None,
    task_id: uuid.UUID | None = None,
    sequence: int | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
) -> None:
    started = started_at or datetime.utcnow()
    completed = completed_at or datetime.utcnow()
    truncated = len(stdout) > MAX_STREAM_CHARS or len(stderr) > MAX_STREAM_CHARS
    record = SandboxCommand(
        sandbox_id=sandbox_id,
        conversation_id=conversation_id,
        task_id=task_id,
        source=source,
        tool_name=tool_name,
        sequence=sequence,
        command=command,
        stdout=stdout[:MAX_STREAM_CHARS],
        stderr=stderr[:MAX_STREAM_CHARS],
        exit_code=exit_code,
        output_truncated=truncated,
        started_at=started,
        completed_at=completed,
        duration_ms=max(0, int((completed - started).total_seconds() * 1000)),
    )
    async with async_session_factory() as db:
        db.add(record)
        await db.commit()


async def persist_general_code_command(
    conversation_id: str | None,
    *,
    command: str,
    stdout: str,
    stderr: str,
    exit_code: int | None,
    started_at: datetime,
    completed_at: datetime,
) -> None:
    if not conversation_id:
        return
    async with async_session_factory() as db:
        conversation = await db.get(Conversation, uuid.UUID(conversation_id))
        sandbox_id = conversation.sandbox_id if conversation else None
    if sandbox_id:
        await persist_command(
            sandbox_id=sandbox_id,
            conversation_id=uuid.UUID(conversation_id),
            source="general_agent",
            tool_name="use_code_exec",
            command=command,
            stdout=stdout,
            stderr=stderr,
            exit_code=exit_code,
            started_at=started_at,
            completed_at=completed_at,
        )
