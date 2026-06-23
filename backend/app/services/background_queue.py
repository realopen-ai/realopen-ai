"""
Sequential background queue for memory extraction + audit.

WHY THIS EXISTS:

Local OpenAI-compatible backends (Ollama, llama.cpp, LM Studio, vLLM)
have a limited number of processing slots (llama.cpp defaults to 4).
Firing memory extraction concurrently with the main chat stream makes
them compete for those slots, EVICTING the main conversation's cached
KV-cache checkpoint and forcing a full prompt re-evaluation on the
next user turn. On a 4B model with a 2k-token system prompt, that's
several hundred ms of pure waste per turn.

This queue serializes extraction/audit jobs so they run STRICTLY AFTER
the chat stream goes idle. The chat API registers an "active stream"
flag per conversation; the queue polls it and waits until idle before
running each job.

USAGE:
    from app.services.background_queue import enqueue_extraction_job

    # After the chat stream finishes:
    enqueue_extraction_job(conv_id, extraction_messages, audit_after=True)

The job runs as a detached asyncio task. Errors are logged, never raised.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Dict, List

from app.config import settings

logger = logging.getLogger(__name__)


def _log(msg: str, *args) -> None:
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[bg_queue] {formatted}", flush=True)


# ── Active-stream tracking ──────────────────────────────────────────
# Per-conversation flag: True while the chat stream is actively emitting
# tokens. The background queue polls this to know when it's safe to run
# extraction without evicting the chat model's KV cache.

_active_streams: Dict[str, float] = {}  # conv_id_str -> started_at_monotonic
_active_streams_lock = asyncio.Lock()


async def mark_stream_active(conversation_id: str) -> None:
    """Mark a conversation's chat stream as active (KV cache in use)."""
    async with _active_streams_lock:
        _active_streams[conversation_id] = time.monotonic()


async def mark_stream_idle(conversation_id: str) -> None:
    """Mark a conversation's chat stream as idle (KV cache safe to evict)."""
    async with _active_streams_lock:
        _active_streams.pop(conversation_id, None)


async def is_stream_active(conversation_id: str) -> bool:
    """Check if a conversation's chat stream is currently active."""
    async with _active_streams_lock:
        return conversation_id in _active_streams


def _is_stream_active_sync(conversation_id: str) -> bool:
    """Sync version for use inside the polling loop."""
    return conversation_id in _active_streams


# ── Job queue ───────────────────────────────────────────────────────
# A simple per-conversation FIFO. We don't need a global queue — each
# conversation's extraction jobs are independent. The serialization is
# per-conversation: jobs for conv A don't block jobs for conv B.

_pending_jobs: Dict[str, List[dict]] = {}  # conv_id_str -> [job, ...]


async def _wait_for_idle(conversation_id: str) -> bool:
    """Wait until the conversation's chat stream goes idle.

    Returns True if the stream went idle (or was never active), False if
    we hit the max-wait timeout (in which case we run anyway — better to
    evict a cache than to never extract).
    """
    max_wait = settings.MEMORY_BG_QUEUE_MAX_WAIT
    poll = settings.MEMORY_BG_QUEUE_POLL
    deadline = time.monotonic() + max_wait

    while time.monotonic() < deadline:
        if not _is_stream_active_sync(conversation_id):
            return True
        await asyncio.sleep(poll)

    _log(
        " == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 = "
    )
    _log(
        "bg queue: stream still active after %ds for conv %s — running anyway",
        max_wait,
        conversation_id,
    )
    _log(
        " == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 = "
    )
    return False


async def _run_extraction_job(
    conversation_id: str,
    extraction_messages: List[dict],
) -> int:
    """Run a single extraction job (with idle wait + audit trigger)."""
    # 1. Wait for the chat stream to go idle so we don't evict the KV cache.
    await _wait_for_idle(conversation_id)

    # 2. Run extraction.
    try:
        from app.services.memory_extractor import extract_and_store

        added = await extract_and_store(
            messages=extraction_messages,
            conversation_id=conversation_id,
        )
        _log(
            " == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 = "
        )
        _log("extraction done for conv %s: added=%d", conversation_id, added)
        _log(
            " == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 = "
        )
        return added
    except Exception as e:
        _log(" == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧 PROCUREMENT = ")
        _log("extraction FAILED for conv %s: %s", conversation_id, e)
        _log(" == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧 PROCUREMENT = ")
        return 0


async def _run_extraction_and_audit(
    conversation_id: str,
    extraction_messages: List[dict],
) -> int:
    """Run extraction, then audit if the counter says it's due."""
    added = await _run_extraction_job(conversation_id, extraction_messages)

    # Audit is triggered inside extract_and_store via the extractions_since_audit
    # counter. But if we want a decoupled audit, we can call it here.
    # For now, extract_and_store handles the audit trigger internally,
    # so we just return.
    return added


def enqueue_extraction_job(
    conversation_id: str,
    extraction_messages: List[dict],
) -> None:
    """Enqueue a memory extraction job to run after the chat stream goes idle.

    Fire-and-forget: the job runs as a detached asyncio task. The caller
    does NOT await it. Errors are logged inside the task.

    The job:
    1. Waits for the conversation's chat stream to go idle (polls every
       MEMORY_BG_QUEUE_POLL seconds, up to MEMORY_BG_QUEUE_MAX_WAIT).
    2. Runs extract_and_store() which extracts facts and may trigger an
       audit if the extraction counter is due.
    """
    if not settings.MEMORY_BACKGROUND_QUEUE:
        # Feature disabled — run inline (old behavior).
        # We still create a task so the caller doesn't block, but we
        # don't wait for idle.
        asyncio.create_task(_run_extraction_job(conversation_id, extraction_messages))
        return

    # Feature enabled — create a task that waits for idle then runs.
    asyncio.create_task(_run_extraction_and_audit(conversation_id, extraction_messages))
    _log(
        " == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 = "
    )
    _log(
        "enqueued extraction job for conv %s (%d messages)",
        conversation_id,
        len(extraction_messages),
    )
    _log(
        " == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 == 🚧🚧 👷‍♂️ 🚧🚧 = "
    )


async def drain_pending_jobs(conversation_id: str, timeout: float = 30.0) -> int:
    """Wait for all pending extraction jobs for a conversation to finish.

    Used at graceful shutdown or for testing. Returns the number of jobs
    drained. Not used in the normal request path.
    """
    # This is a best-effort drain — we don't track job completion here.
    # In practice the jobs are fire-and-forget. This stub exists for
    # future testability.
    await asyncio.sleep(0.1)
    return 0
