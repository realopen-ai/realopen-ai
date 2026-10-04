"""Tests for the sequential background job queue (app/services/background_queue.py).

Scope:
  - active-stream tracking: mark_stream_active / mark_stream_idle /
    is_stream_active / _is_stream_active_sync
  - _wait_for_idle: immediate return when idle, poll-until-idle, and the
    max-wait timeout (runs anyway → returns False)
  - _run_extraction_job: success returns the extractor's count, failure
    returns 0 (errors never propagate)
  - _run_extraction_and_audit: delegates to the extraction job
  - enqueue_extraction_job: feature-flag on/off paths both spawn a detached
    task that runs the (mocked) extractor
  - drain_pending_jobs: best-effort stub returns 0
  - _log: bad format args fall back to concatenation instead of raising

Mocks:
  - app.services.memory_extractor.extract_and_store (imported lazily inside
    the job) — no LLM/Ollama call is ever made.
  - settings.MEMORY_BG_QUEUE_* timing knobs — polls use tiny real sleeps
    (≤50 ms) so no test hangs; no fake clocks needed.
"""

import asyncio
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.config import settings  # noqa: E402
from app.services import background_queue as bgq  # noqa: E402
from app.services import memory_extractor  # noqa: E402


@pytest.fixture(autouse=True)
def clean_state():
    """Isolate module-level stream/queue state between tests."""
    bgq._active_streams.clear()
    bgq._pending_jobs.clear()
    yield
    bgq._active_streams.clear()
    bgq._pending_jobs.clear()


@pytest.fixture(autouse=True)
def fast_timing(monkeypatch):
    """Keep queue timing knobs small (monkeypatch restores them afterwards)."""
    monkeypatch.setattr(settings, "MEMORY_BG_QUEUE_MAX_WAIT", 1.0)
    monkeypatch.setattr(settings, "MEMORY_BG_QUEUE_POLL", 0.01)
    yield


# ── active-stream tracking ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_stream_lifecycle_round_trip():
    conv = "conv-1"
    assert await bgq.is_stream_active(conv) is False
    assert bgq._is_stream_active_sync(conv) is False

    await bgq.mark_stream_active(conv)
    assert await bgq.is_stream_active(conv) is True
    assert bgq._is_stream_active_sync(conv) is True

    await bgq.mark_stream_idle(conv)
    assert await bgq.is_stream_active(conv) is False
    # Idling an unknown conversation is a no-op
    await bgq.mark_stream_idle(conv)
    assert await bgq.is_stream_active(conv) is False


@pytest.mark.asyncio
async def test_streams_are_tracked_per_conversation():
    await bgq.mark_stream_active("conv-a")
    assert await bgq.is_stream_active("conv-b") is False
    assert await bgq.is_stream_active("conv-a") is True


# ── _wait_for_idle ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_for_idle_returns_immediately_when_idle():
    assert await bgq._wait_for_idle("never-active") is True


@pytest.mark.asyncio
async def test_wait_for_idle_polls_until_stream_goes_idle(monkeypatch):
    conv = "conv-busy"
    await bgq.mark_stream_active(conv)

    async def release_soon():
        await asyncio.sleep(0.03)
        await bgq.mark_stream_idle(conv)

    releaser = asyncio.create_task(release_soon())
    result = await bgq._wait_for_idle(conv)
    await releaser
    assert result is True
    assert await bgq.is_stream_active(conv) is False


@pytest.mark.asyncio
async def test_wait_for_idle_times_out_and_runs_anyway(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_BG_QUEUE_MAX_WAIT", 0.05)
    monkeypatch.setattr(settings, "MEMORY_BG_QUEUE_POLL", 0.01)
    conv = "conv-stuck"
    await bgq.mark_stream_active(conv)
    result = await bgq._wait_for_idle(conv)
    assert result is False
    # The stream flag is left untouched — the caller just proceeds.
    assert await bgq.is_stream_active(conv) is True


# ── _run_extraction_job ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_extraction_job_success(monkeypatch):
    captured = {}

    async def fake_extract(messages, conversation_id):
        captured["messages"] = messages
        captured["conversation_id"] = conversation_id
        return 7

    monkeypatch.setattr(memory_extractor, "extract_and_store", fake_extract)
    messages = [{"role": "user", "content": "hi"}]
    added = await bgq._run_extraction_job("conv-x", messages)
    assert added == 7
    assert captured == {"messages": messages, "conversation_id": "conv-x"}


@pytest.mark.asyncio
async def test_run_extraction_job_swallows_extractor_failure(monkeypatch):
    async def boom(messages, conversation_id):
        raise RuntimeError("ollama exploded")

    monkeypatch.setattr(memory_extractor, "extract_and_store", boom)
    added = await bgq._run_extraction_job("conv-x", [])
    assert added == 0


@pytest.mark.asyncio
async def test_run_extraction_job_waits_for_idle_first(monkeypatch):
    conv = "conv-sequenced"
    order = []

    async def fake_extract(messages, conversation_id):
        order.append("extract")
        return 1

    monkeypatch.setattr(memory_extractor, "extract_and_store", fake_extract)
    await bgq.mark_stream_active(conv)

    async def release_soon():
        await asyncio.sleep(0.02)
        order.append("idle")
        await bgq.mark_stream_idle(conv)

    releaser = asyncio.create_task(release_soon())
    added = await bgq._run_extraction_job(conv, [])
    await releaser
    assert added == 1
    # Extraction strictly happened after the stream went idle
    assert order == ["idle", "extract"]


@pytest.mark.asyncio
async def test_run_extraction_and_audit_delegates(monkeypatch):
    calls = []

    async def fake_extract(messages, conversation_id):
        calls.append(conversation_id)
        return 3

    monkeypatch.setattr(memory_extractor, "extract_and_store", fake_extract)
    assert await bgq._run_extraction_and_audit("conv-y", []) == 3
    assert calls == ["conv-y"]


# ── enqueue_extraction_job ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_enqueue_runs_job_when_feature_enabled(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_BACKGROUND_QUEUE", True)
    started = asyncio.Event()

    async def fake_job(conversation_id, messages):
        started.set()
        return 5

    monkeypatch.setattr(bgq, "_run_extraction_job", fake_job)

    bgq.enqueue_extraction_job("conv-z", [{"role": "user", "content": "x"}])
    # Fire-and-forget task — give the loop a chance to run it
    await asyncio.wait_for(started.wait(), timeout=2.0)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_enqueue_runs_job_when_feature_disabled(monkeypatch):
    monkeypatch.setattr(settings, "MEMORY_BACKGROUND_QUEUE", False)
    started = asyncio.Event()
    seen = {}

    async def fake_job(conversation_id, messages):
        seen["conv"] = conversation_id
        seen["messages"] = messages
        started.set()
        return 0

    monkeypatch.setattr(bgq, "_run_extraction_job", fake_job)
    bgq.enqueue_extraction_job("conv-off", [{"m": 1}])
    await asyncio.wait_for(started.wait(), timeout=2.0)
    assert seen == {"conv": "conv-off", "messages": [{"m": 1}]}
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_enqueued_job_survives_extractor_error(monkeypatch):
    """Errors inside the detached task are logged, never raised."""
    monkeypatch.setattr(settings, "MEMORY_BACKGROUND_QUEUE", True)
    finished = asyncio.Event()

    async def failing_audit(conversation_id, messages):
        try:
            raise RuntimeError("extraction backend down")
        finally:
            finished.set()

    monkeypatch.setattr(bgq, "_run_extraction_and_audit", failing_audit)
    bgq.enqueue_extraction_job("conv-err", [])
    await asyncio.wait_for(finished.wait(), timeout=2.0)
    # Let the task's exception surface if it leaked (it must not).
    await asyncio.sleep(0.05)


# ── drain + logging ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drain_pending_jobs_is_best_effort_stub():
    drained = await bgq.drain_pending_jobs("conv-1", timeout=0.2)
    assert drained == 0


def test_log_formats_args():
    # Proper format string
    assert bgq._log("value=%d", 3) is None
    # Mismatched args fall back to concatenation without raising
    assert bgq._log("value=%d", "not-a-number") is None
    # No args
    assert bgq._log("plain message") is None


def test_pending_jobs_dict_exists_per_conversation():
    """The per-conversation FIFO registry is part of the module contract."""
    assert isinstance(bgq._pending_jobs, dict)
    bgq._pending_jobs["conv-q"] = [{"job": 1}]
    assert bgq._pending_jobs["conv-q"] == [{"job": 1}]
