"""
Prometheus metrics for RealOpen-AI.

Exposes AI-specific metrics beyond the basic HTTP metrics from
prometheus_fastapi_instrumentator. Tracks tokens, tool calls,
RAG timing, model latency, memory extraction, and more.

Metrics are collected by Victoria Metrics and visualized in Grafana.
"""

import time
from contextlib import asynccontextmanager, contextmanager

from prometheus_client import Counter, Histogram, Gauge, Info, generate_latest, REGISTRY

# ── Chat / LLM metrics ───────────────────────────────────────────

chat_requests_total = Counter(
    "realopen_chat_requests_total",
    "Total number of chat requests",
    ["model", "status"],  # status: success, error
)

chat_tokens_total = Counter(
    "realopen_chat_tokens_total",
    "Total tokens processed (input + output estimate)",
    ["model"],
)

chat_duration_seconds = Histogram(
    "realopen_chat_duration_seconds",
    "Chat request duration in seconds",
    ["model"],
    buckets=[0.5, 1, 2, 5, 10, 20, 30, 60, 120, 300],
)

chat_streaming_tokens_per_second = Gauge(
    "realopen_chat_streaming_tokens_per_second",
    "Current tokens-per-second during streaming",
    ["model"],
)

# In-flight chat requests. A Counter cannot be decremented (prometheus_client
# removed Counter.dec), so running requests are tracked with a Gauge.
chat_requests_running = Gauge(
    "realopen_chat_requests_running",
    "Currently in-flight chat requests",
    ["model"],
)


# ── Tool call metrics ─────────────────────────────────────────────

tool_calls_total = Counter(
    "realopen_tool_calls_total",
    "Total tool calls executed",
    ["tool", "status"],  # tool: websearch, vision, code_exec, etc.
)

tool_call_duration_seconds = Histogram(
    "realopen_tool_call_duration_seconds",
    "Tool call execution duration",
    ["tool"],
    buckets=[0.1, 0.5, 1, 2, 5, 10, 15, 30, 60],
)


# ── RAG metrics ───────────────────────────────────────────────────

rag_search_duration_seconds = Histogram(
    "realopen_rag_search_duration_seconds",
    "RAG document search duration",
    buckets=[0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10],
)

rag_chunks_retrieved = Histogram(
    "realopen_rag_chunks_retrieved",
    "Number of chunks retrieved per RAG search",
    buckets=[0, 1, 2, 3, 5, 10, 20],
)

rag_documents_indexed_total = Counter(
    "realopen_rag_documents_indexed_total",
    "Total documents indexed into RAG",
)


# ── Memory extraction metrics ─────────────────────────────────────

memory_extractions_total = Counter(
    "realopen_memory_extractions_total",
    "Total memory extraction runs",
    ["status"],  # status: success, skipped, error
)

memory_facts_extracted_total = Counter(
    "realopen_memory_facts_extracted_total",
    "Total facts extracted from conversations",
)

memory_audit_runs_total = Counter(
    "realopen_memory_audit_runs_total",
    "Total memory audit/consolidation runs",
)

memory_total_count = Gauge(
    "realopen_memory_total_count",
    "Current total number of stored memories",
)


# ── Model / system metrics ────────────────────────────────────────

model_load_duration_seconds = Gauge(
    "realopen_model_load_duration_seconds",
    "Last model load time in seconds (from Ollama)",
    ["model"],
)

model_active = Gauge(
    "realopen_model_active",
    "Whether a model is currently loaded (1) or not (0)",
    ["model"],
)

context_usage_ratio = Gauge(
    "realopen_context_usage_ratio",
    "Current context window usage ratio (0.0 - 1.0)",
    ["model"],
)

agent_rounds = Histogram(
    "realopen_agent_rounds",
    "Number of agent rounds per request (tool call loops)",
    buckets=[1, 2, 3, 4, 5, 6, 8, 10],
)


# ── Conversation / session metrics ────────────────────────────────

conversations_active_total = Gauge(
    "realopen_conversations_active_total",
    "Number of conversations with recent activity (last 24h)",
)

conversations_created_total = Counter(
    "realopen_conversations_created_total",
    "Total conversations created",
)


# ── Context compaction metrics ────────────────────────────────────

context_compactions_total = Counter(
    "realopen_context_compactions_total",
    "Total context compaction runs",
    ["model"],
)


# ── Application info ──────────────────────────────────────────────

app_info = Info("realopen_app", "RealOpen-AI application information")


# ── Helper: expose metrics endpoint ───────────────────────────────


def get_metrics() -> bytes:
    """Generate Prometheus metrics text (includes fastapi instrumentator + custom)."""
    return generate_latest(REGISTRY)


# ── Context managers for easy instrumentation ─────────────────────


@contextmanager
def track_chat_request(model: str):
    """Track a chat request: duration, status, active model."""
    start = time.monotonic()
    status = "success"
    chat_requests_running.labels(model=model).inc()
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        elapsed = time.monotonic() - start
        chat_requests_running.labels(model=model).dec()
        chat_requests_total.labels(model=model, status=status).inc()
        chat_duration_seconds.labels(model=model).observe(elapsed)


@contextmanager
def track_tool_call(tool_name: str):
    """Track a tool call: duration and status."""
    start = time.monotonic()
    status = "success"
    try:
        yield
    except Exception:
        status = "error"
        raise
    finally:
        elapsed = time.monotonic() - start
        tool_calls_total.labels(tool=tool_name, status=status).inc()
        tool_call_duration_seconds.labels(tool=tool_name).observe(elapsed)


@asynccontextmanager
async def track_rag_search():
    """Track a RAG search: duration and chunks retrieved."""
    start = time.monotonic()
    try:
        yield  # caller sets chunks count via the yielded object
    finally:
        elapsed = time.monotonic() - start
        rag_search_duration_seconds.observe(elapsed)


def record_chat_tokens(model: str, count: int):
    """Record tokens processed for a chat request."""
    chat_tokens_total.labels(model=model).inc(count)


def record_rag_chunks(count: int):
    """Record number of chunks retrieved in a RAG search."""
    rag_chunks_retrieved.observe(count)


def record_rag_document_indexed():
    """Record a document being indexed into RAG."""
    rag_documents_indexed_total.inc()


def record_memory_extraction(success: bool, facts_added: int = 0):
    """Record a memory extraction run."""
    status = "success" if success else "error"
    memory_extractions_total.labels(status=status).inc()
    if facts_added > 0:
        memory_facts_extracted_total.inc(facts_added)


def record_memory_audit():
    """Record a memory audit run."""
    memory_audit_runs_total.inc()


def record_model_load(model: str, duration_seconds: float):
    """Record model load time."""
    model_load_duration_seconds.labels(model=model).set(duration_seconds)


def record_context_compaction(model: str):
    """Record a context compaction event."""
    context_compactions_total.labels(model=model).inc()


def record_agent_rounds(count: int):
    """Record number of agent rounds for a request."""
    agent_rounds.observe(count)


def set_context_usage(model: str, ratio: float):
    """Set current context window usage ratio."""
    context_usage_ratio.labels(model=model).set(min(max(ratio, 0.0), 1.0))


def set_streaming_tps(model: str, tps: float):
    """Set current streaming tokens per second."""
    chat_streaming_tokens_per_second.labels(model=model).set(tps)


def set_conversations_active(count: int):
    """Set count of active conversations."""
    conversations_active_total.set(count)


def record_conversation_created():
    """Record a new conversation being created."""
    conversations_created_total.inc()


def set_memory_total(count: int):
    """Set total memory count gauge."""
    memory_total_count.set(count)
