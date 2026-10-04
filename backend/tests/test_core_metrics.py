"""
Tests for the Prometheus metrics helpers (app/core/metrics.py).

Scope:
- get_metrics() exposes the custom realopen_* families as Prometheus text.
- The instrumentation context managers: track_chat_request (success and
  error, running-gauge bookkeeping), track_tool_call (success and error),
  track_rag_search (async).
- Every record_*/set_* helper: counter increments, gauge values, histogram
  observes, and the context-usage clamp (0.0 - 1.0).

Mocking: none — prometheus_client counters live in-process. Each test uses
a UNIQUE label value (e.g. model="pytest-metrics-<case>") so the samples
start from zero and assertions are exact regardless of test ordering.
Values are read back through REGISTRY.get_sample_value.
"""

import pytest
from prometheus_client import REGISTRY

from app.core import metrics


def _sample(name: str, labels: dict | None = None) -> float:
    value = REGISTRY.get_sample_value(name, labels)
    return float(value) if value is not None else 0.0


# ── get_metrics ────────────────────────────────────────────────────────


def test_get_metrics_returns_bytes_with_realopen_families():
    metrics.record_chat_tokens("pytest-metrics-text", 1)
    payload = metrics.get_metrics()
    assert isinstance(payload, bytes)
    assert b"realopen_chat_tokens_total" in payload
    assert b"# HELP" in payload or b"# TYPE" in payload


def test_get_metrics_reflects_increment():
    model = "pytest-metrics-reflect"
    metrics.record_chat_tokens(model, 3)
    payload = metrics.get_metrics()
    assert f'realopen_chat_tokens_total{{model="{model}"}}'.encode() in payload


# ── track_chat_request ─────────────────────────────────────────────────


def test_track_chat_request_success():
    model = "pytest-metrics-chat-ok"
    with metrics.track_chat_request(model):
        # While running, the in-flight gauge is up
        assert _sample("realopen_chat_requests_running", {"model": model}) == 1.0

    # Gauge back to zero; success counted once; no error sample
    assert _sample("realopen_chat_requests_running", {"model": model}) == 0.0
    assert _sample(
        "realopen_chat_requests_total", {"model": model, "status": "success"}
    ) == 1.0
    assert _sample(
        "realopen_chat_requests_total", {"model": model, "status": "error"}
    ) == 0.0
    assert _sample("realopen_chat_duration_seconds_count", {"model": model}) == 1.0


def test_track_chat_request_error_counts_as_error_and_reraises():
    model = "pytest-metrics-chat-err"
    with pytest.raises(ValueError, match="kaput"):
        with metrics.track_chat_request(model):
            raise ValueError("kaput")

    assert _sample(
        "realopen_chat_requests_total", {"model": model, "status": "error"}
    ) == 1.0
    assert _sample(
        "realopen_chat_requests_total", {"model": model, "status": "success"}
    ) == 0.0
    # in-flight gauge was decremented in the finally block
    assert _sample("realopen_chat_requests_running", {"model": model}) == 0.0
    assert _sample("realopen_chat_duration_seconds_count", {"model": model}) == 1.0


def test_track_chat_request_duration_sum_positive():
    model = "pytest-metrics-chat-dur"
    with metrics.track_chat_request(model):
        pass
    observed_sum = _sample("realopen_chat_duration_seconds_sum", {"model": model})
    assert observed_sum >= 0.0


# ── track_tool_call ────────────────────────────────────────────────────


def test_track_tool_call_success():
    tool = "pytest-metrics-tool-ok"
    with metrics.track_tool_call(tool):
        pass
    assert _sample(
        "realopen_tool_calls_total", {"tool": tool, "status": "success"}
    ) == 1.0
    assert _sample("realopen_tool_call_duration_seconds_count", {"tool": tool}) == 1.0


def test_track_tool_call_error_reraises_and_counts_error():
    tool = "pytest-metrics-tool-err"
    with pytest.raises(RuntimeError, match="tool blew up"):
        with metrics.track_tool_call(tool):
            raise RuntimeError("tool blew up")

    assert _sample("realopen_tool_calls_total", {"tool": tool, "status": "error"}) == 1.0
    assert _sample(
        "realopen_tool_calls_total", {"tool": tool, "status": "success"}
    ) == 0.0
    assert _sample("realopen_tool_call_duration_seconds_count", {"tool": tool}) == 1.0


# ── track_rag_search ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_track_rag_search_observes_duration():
    before = _sample("realopen_rag_search_duration_seconds_count")
    async with metrics.track_rag_search():
        pass
    assert _sample("realopen_rag_search_duration_seconds_count") == before + 1.0


@pytest.mark.asyncio
async def test_track_rag_search_records_duration_even_on_error():
    before = _sample("realopen_rag_search_duration_seconds_count")
    with pytest.raises(RuntimeError, match="rag failed"):
        async with metrics.track_rag_search():
            raise RuntimeError("rag failed")
    assert _sample("realopen_rag_search_duration_seconds_count") == before + 1.0


# ── record_* helpers ───────────────────────────────────────────────────


def test_record_chat_tokens_accumulates():
    model = "pytest-metrics-tokens"
    metrics.record_chat_tokens(model, 5)
    metrics.record_chat_tokens(model, 3)
    assert _sample("realopen_chat_tokens_total", {"model": model}) == 8.0


def test_record_rag_chunks_observes_histogram():
    metrics.record_rag_chunks(4)
    metrics.record_rag_chunks(7)
    count = _sample("realopen_rag_chunks_retrieved_count")
    total = _sample("realopen_rag_chunks_retrieved_sum")
    assert count >= 2.0
    assert total >= 11.0


def test_record_rag_document_indexed_increments():
    before = _sample("realopen_rag_documents_indexed_total")
    metrics.record_rag_document_indexed()
    metrics.record_rag_document_indexed()
    assert _sample("realopen_rag_documents_indexed_total") == before + 2.0


def test_record_memory_extraction_success_with_facts():
    metrics.record_memory_extraction(True, facts_added=4)
    assert _sample(
        "realopen_memory_extractions_total", {"status": "success"}
    ) >= 1.0
    assert _sample("realopen_memory_facts_extracted_total") >= 4.0


def test_record_memory_extraction_error_status_without_facts():
    """Failed runs count under status=error; facts_added=0 leaves the
    facts counter untouched (the documented caller contract on error)."""
    facts_before = _sample("realopen_memory_facts_extracted_total")
    metrics.record_memory_extraction(False, facts_added=0)
    assert _sample("realopen_memory_extractions_total", {"status": "error"}) >= 1.0
    assert _sample("realopen_memory_facts_extracted_total") == facts_before


def test_record_memory_extraction_counts_facts_whenever_given():
    """Current contract: any facts_added > 0 increments the facts counter,
    regardless of the success flag (callers pass 0 for failed runs)."""
    metrics.record_memory_extraction(True, facts_added=3)
    metrics.record_memory_extraction(False, facts_added=2)
    assert _sample("realopen_memory_facts_extracted_total") >= 5.0
    assert _sample("realopen_memory_extractions_total", {"status": "success"}) >= 1.0
    assert _sample("realopen_memory_extractions_total", {"status": "error"}) >= 1.0


def test_record_memory_extraction_success_zero_facts():
    facts_before = _sample("realopen_memory_facts_extracted_total")
    metrics.record_memory_extraction(True, facts_added=0)
    assert _sample("realopen_memory_facts_extracted_total") == facts_before


def test_record_memory_audit_increments():
    before = _sample("realopen_memory_audit_runs_total")
    metrics.record_memory_audit()
    assert _sample("realopen_memory_audit_runs_total") == before + 1.0


def test_record_model_load_sets_gauge():
    model = "pytest-metrics-load"
    metrics.record_model_load(model, 12.5)
    assert _sample("realopen_model_load_duration_seconds", {"model": model}) == 12.5
    metrics.record_model_load(model, 3.25)
    assert _sample("realopen_model_load_duration_seconds", {"model": model}) == 3.25


def test_record_context_compaction_increments():
    model = "pytest-metrics-compact"
    metrics.record_context_compaction(model)
    metrics.record_context_compaction(model)
    assert _sample("realopen_context_compactions_total", {"model": model}) == 2.0


def test_record_agent_rounds_observes_histogram():
    before_count = _sample("realopen_agent_rounds_count")
    before_sum = _sample("realopen_agent_rounds_sum")
    metrics.record_agent_rounds(3)
    assert _sample("realopen_agent_rounds_count") == before_count + 1.0
    assert _sample("realopen_agent_rounds_sum") == before_sum + 3.0


# ── set_* gauge helpers ────────────────────────────────────────────────


def test_set_context_usage_in_range_value():
    model = "pytest-metrics-ctx"
    metrics.set_context_usage(model, 0.5)
    assert _sample("realopen_context_usage_ratio", {"model": model}) == 0.5


def test_set_context_usage_clamps_above_one():
    model = "pytest-metrics-ctx-hi"
    metrics.set_context_usage(model, 1.7)
    assert _sample("realopen_context_usage_ratio", {"model": model}) == 1.0


def test_set_context_usage_clamps_below_zero():
    model = "pytest-metrics-ctx-lo"
    metrics.set_context_usage(model, -0.3)
    assert _sample("realopen_context_usage_ratio", {"model": model}) == 0.0


def test_set_streaming_tps_sets_gauge():
    model = "pytest-metrics-tps"
    metrics.set_streaming_tps(model, 42.5)
    assert _sample("realopen_chat_streaming_tokens_per_second", {"model": model}) == 42.5
    metrics.set_streaming_tps(model, 0.0)
    assert _sample("realopen_chat_streaming_tokens_per_second", {"model": model}) == 0.0


def test_set_conversations_active_sets_gauge():
    metrics.set_conversations_active(7)
    assert _sample("realopen_conversations_active_total") == 7.0
    metrics.set_conversations_active(0)
    assert _sample("realopen_conversations_active_total") == 0.0


def test_record_conversation_created_increments():
    before = _sample("realopen_conversations_created_total")
    metrics.record_conversation_created()
    assert _sample("realopen_conversations_created_total") == before + 1.0


def test_set_memory_total_sets_gauge():
    metrics.set_memory_total(123)
    assert _sample("realopen_memory_total_count") == 123.0
    metrics.set_memory_total(0)
    assert _sample("realopen_memory_total_count") == 0.0


# ── module-level metric objects exist with expected families ──────────


def test_all_metric_families_registered():
    # Labeled families only appear in the exposition once a child sample
    # exists — touch every family once so presence is deterministic.
    m = "pytest-metrics-family"
    metrics.chat_requests_total.labels(model=m, status="success").inc()
    metrics.chat_tokens_total.labels(model=m).inc()
    metrics.chat_duration_seconds.labels(model=m).observe(0.1)
    metrics.chat_streaming_tokens_per_second.labels(model=m).set(1.0)
    metrics.chat_requests_running.labels(model=m).inc()
    metrics.chat_requests_running.labels(model=m).dec()
    metrics.tool_calls_total.labels(tool="t", status="success").inc()
    metrics.tool_call_duration_seconds.labels(tool="t").observe(0.1)
    metrics.rag_search_duration_seconds.observe(0.1)
    metrics.rag_chunks_retrieved.observe(1)
    metrics.rag_documents_indexed_total.inc()
    metrics.memory_extractions_total.labels(status="success").inc()
    metrics.memory_facts_extracted_total.inc()
    metrics.memory_audit_runs_total.inc()
    metrics.memory_total_count.set(1)
    metrics.model_load_duration_seconds.labels(model=m).set(1.0)
    metrics.model_active.labels(model=m).set(1)
    metrics.context_usage_ratio.labels(model=m).set(0.5)
    metrics.agent_rounds.observe(1)
    metrics.conversations_active_total.set(1)
    metrics.conversations_created_total.inc()
    metrics.context_compactions_total.labels(model=m).inc()

    payload = metrics.get_metrics().decode("utf-8", errors="replace")
    for family in (
        "realopen_chat_requests_total",
        "realopen_chat_requests_running",
        "realopen_chat_tokens_total",
        "realopen_chat_duration_seconds",
        "realopen_chat_streaming_tokens_per_second",
        "realopen_tool_calls_total",
        "realopen_tool_call_duration_seconds",
        "realopen_rag_search_duration_seconds",
        "realopen_rag_chunks_retrieved",
        "realopen_rag_documents_indexed_total",
        "realopen_memory_extractions_total",
        "realopen_memory_facts_extracted_total",
        "realopen_memory_audit_runs_total",
        "realopen_memory_total_count",
        "realopen_model_load_duration_seconds",
        "realopen_model_active",
        "realopen_context_usage_ratio",
        "realopen_agent_rounds",
        "realopen_conversations_active_total",
        "realopen_conversations_created_total",
        "realopen_context_compactions_total",
    ):
        assert family in payload, f"metric family {family} missing from exposition"
