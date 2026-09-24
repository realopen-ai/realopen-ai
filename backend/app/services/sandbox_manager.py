"""Database-backed sandbox provisioning shared by API and agents."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Sandbox
from app.services import sandbox_host

DEFAULT_CPU_LIMIT = 2.0
DEFAULT_MEMORY_LIMIT_MB = 2048
DEFAULT_WORKSPACE_QUOTA_BYTES = 2 * 1024**3
DEFAULT_IDLE_TIMEOUT_SECONDS = 1800


async def provision_sandbox(
    db: AsyncSession,
    *,
    name: str,
    conversation: Conversation | None = None,
    cpu_limit: float = DEFAULT_CPU_LIMIT,
    memory_limit_mb: int = DEFAULT_MEMORY_LIMIT_MB,
    workspace_quota_bytes: int = DEFAULT_WORKSPACE_QUOTA_BYTES,
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
) -> Sandbox:
    """Create, start, and optionally link a persistent sandbox."""
    ident = uuid.uuid4()
    item = Sandbox(
        id=ident,
        name=name.strip()[:120] or "Coder workspace",
        volume_name=f"realopenai-sandbox-{ident}",
        container_name=f"realopenai-sandbox-{ident}",
        cpu_limit=cpu_limit,
        memory_limit_mb=memory_limit_mb,
        workspace_quota_bytes=workspace_quota_bytes,
        idle_timeout_seconds=idle_timeout_seconds,
        status="creating",
        desired_running=True,
        last_active_at=datetime.utcnow(),
    )
    db.add(item)
    await db.flush()
    if conversation is not None:
        conversation.sandbox_id = item.id
    try:
        await sandbox_host.call("create", item)
    except Exception as exc:
        item.status, item.error = "error", str(exc)
        raise
    item.status, item.error = "running", None
    return item
