"""
Embedding service for the memory + RAG systems.

Two-tier embedding strategy:

  Lane 1 (primary): Ollama HTTP /api/embeddings with nomic-embed-text:v1.5
                    (768-dim). Configured via profiles.yml role
                    ``default_embedding``. Always tried first because it
                    shares the same model the stored vectors were built
                    with — dimensionally consistent by construction.

  Lane 2 (fallback): Local FastEmbed ONNX model (default:
                      ``sentence-transformers/all-MiniLM-L6-v2``, 384-dim).
                      Loaded lazily on first Ollama failure and kept warm
                      for the process lifetime. Used ONLY for query-time
                      embedding when Ollama is unreachable — NEVER for
                      document ingestion (mixing dimensions would corrupt
                      the index).

CRITICAL DESIGN NOTE on the fallback lane:
  FastEmbed produces 384-dim vectors while nomic-embed-text produces 768.
  pgvector columns are typed (``Vector(768)``) so a 384-dim insert will
  fail. Therefore the fallback lane is query-time-only: when Ollama is
  down, we can still embed the user's QUERY and compare it against stored
  768-dim vectors using a DIMENSION-AGNOSTIC keyword fallback (the BM25
  tsvector path in memory.py / rag.py). We do NOT attempt cross-dimension
  cosine — that's mathematically invalid (i believe).

  In practice this means: Ollama down → vector search unavailable →
  retrieval degrades to BM25-only (memory) or empty (RAG). The fallback
  lane's value is that it lets query-embedding succeed so the higher-
  level "did embedding work?" code path doesn't short-circuit, and it
  keeps the FastEmbed model warm so recovery is instant when Ollama
  comes back.

  Concretely: get_embedding() tries Ollama. If Ollama fails, returns
  None (callers degrade to BM25). The FastEmbed lane is wired but not
  active by default. Set EMBEDDING_FALLBACK_FASTEMBED=true to enable it
  for query-time embedding when Ollama is down (useful only if you also
  reconfigure your vector columns to 384-dim).

Caching:
  An LRU cache keyed on sha256(text) avoids re-embedding identical text
  (common: the same query rephrased across turns, the same memory re-
  retrieved). Capped at EMBEDDING_CACHE_SIZE entries (default 2048).
  Cache hits return in ~0µs and never touch Ollama.

Concurrency:
  asyncio.Semaphore(EMBEDDING_CONCURRENCY) caps concurrent Ollama calls
  so a 50-chunk digestion doesn't fire 50 simultaneous HTTP requests at
  a single-threaded Ollama instance (which would queue them and could
  cause timeouts).
"""

import asyncio
import hashlib
import logging
import os
from collections import OrderedDict
from typing import List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ── Singleton state (lazy-init) ──────────────────────────────────────
_resolved_model: Optional[str] = None
_ollama_url: Optional[str] = None

# LRU cache: {sha256_hex: embedding_list}. Bounded by EMBEDDING_CACHE_SIZE.
# Ordered by insertion (oldest evicted first). Thread-safe via the GIL for
# the dict ops; the semaphore below serializes the actual Ollama calls.
_embedding_cache: "OrderedDict[str, List[float]]" = OrderedDict()
_EMBEDDING_CACHE_MAX = 2048

# Concurrency cap for Ollama embedding calls. Ollama is single-threaded
# for embedding generation, so firing 50 parallel requests just makes them
# queue up internally and risks HTTP timeouts. 5 keeps the pipeline
# flowing without overwhelming the server.
_EMBED_SEMAPHORE: Optional[asyncio.Semaphore] = None

# Process-level latch: once Ollama embedding is found down, skip re-probing
# for EMBEDDING_DOWN_COOLDOWN_SECONDS to avoid paying the connect timeout
# on every call. Reset by clear_ollama_down_latch() (e.g. when the user
# saves a new Ollama URL in settings).
_ollama_embed_down: bool = False
_ollama_down_until: float = 0.0
_OLLAMA_DOWN_COOLDOWN = 30.0  # seconds

# FastEmbed lane (lazy-loaded). Kept None when disabled or unavailable.
_fastembed_client = None
_fastembed_dim: Optional[int] = None
_fastembed_load_attempted = False


def _get_semaphore() -> asyncio.Semaphore:
    """Lazy-init the semaphore so it binds to the running event loop."""
    global _EMBED_SEMAPHORE
    if _EMBED_SEMAPHORE is None:
        # Concurrent Ollama embedding calls. Configurable via env var
        # EMBEDDING_CONCURRENCY (default 5).
        concurrency = int(os.environ.get("EMBEDDING_CONCURRENCY", "5"))
        _EMBED_SEMAPHORE = asyncio.Semaphore(concurrency)
    return _EMBED_SEMAPHORE


def _log(msg: str, *args) -> None:
    """Always-visible print() logger for the embeddings service."""
    try:
        formatted = msg % args if args else msg
    except (TypeError, ValueError):
        formatted = f"{msg} {args}"
    print(f"[embeddings] {formatted}", flush=True)


def _ensure_resolved() -> None:
    """Resolve the embedding model ID and Ollama URL once, cache for the process."""
    global _resolved_model, _ollama_url
    if _resolved_model is None:
        _resolved_model = settings.resolve_model(settings.MEMORY_EMBEDDING_MODEL_ROLE)
        _ollama_url = settings.OLLAMA_BASE_URL
        _log("resolved embedding model=%s url=%s", _resolved_model, _ollama_url)


def _cache_key(text: str) -> str:
    """sha256 of the text — stable, collision-resistant, fast enough."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _cache_get(text: str) -> Optional[List[float]]:
    """Return cached embedding for text, or None. Moves hit to MRU position."""
    key = _cache_key(text)
    val = _embedding_cache.get(key)
    if val is not None:
        _embedding_cache.move_to_end(key)
    return val


def _cache_put(text: str, embedding: List[float]) -> None:
    """Cache an embedding, evicting LRU entries if over capacity."""
    key = _cache_key(text)
    _embedding_cache[key] = embedding
    _embedding_cache.move_to_end(key)
    while len(_embedding_cache) > _EMBEDDING_CACHE_MAX:
        _embedding_cache.popitem(last=False)


def clear_embedding_cache() -> int:
    """Clear the embedding cache. Returns the number of entries dropped."""
    n = len(_embedding_cache)
    _embedding_cache.clear()
    return n


def cache_stats() -> dict:
    """Return cache statistics for observability."""
    return {
        "entries": len(_embedding_cache),
        "capacity": _EMBEDDING_CACHE_MAX,
        "ollama_down": _ollama_embed_down,
        "ollama_down_cooldown_remaining": (
            max(0.0, _ollama_down_until - asyncio.get_event_loop().time())
            if _ollama_embed_down
            else 0.0
        ),
        "fastembed_available": _fastembed_client is not None,
        "fastembed_dim": _fastembed_dim,
    }


def clear_ollama_down_latch() -> None:
    """Reset the 'Ollama embedding down' latch.

    Call this when the user saves a new Ollama URL or restarts Ollama,
    so we re-probe on the next embedding call instead of waiting for
    the cooldown.
    """
    global _ollama_embed_down, _ollama_down_until
    _ollama_embed_down = False
    _ollama_down_until = 0.0


def get_embedding_model() -> str:
    """Return the resolved Ollama embedding model ID (e.g. 'nomic-embed-text:v1.5')."""
    _ensure_resolved()
    return _resolved_model or "nomic-embed-text:v1.5"


def get_embedding_dimension() -> int:
    """Return the expected embedding dimension (768 for nomic-embed-text).

    Used at startup to validate that the DB vector columns match the
    configured model — a model swap to a different-dim model would
    silently break vector search at insert time.
    """
    # nomic-embed-text:v1.5 = 768. If a different model is configured,
    # we probe its dimension on first use via _probe_dimension().
    return 768


async def _try_fastembed(text: str) -> Optional[List[float]]:
    """Try the FastEmbed fallback lane. Returns None if unavailable/disabled.

    Loads the ONNX model lazily on first call. The model choice and
    dimension are controlled by env vars:
      EMBEDDING_FALLBACK_FASTEMBED=true  (default: false — disabled)
      FASTEMBED_MODEL=sentence-transformers/all-MiniLM-L6-v2
    """
    global _fastembed_client, _fastembed_dim, _fastembed_load_attempted

    if os.environ.get("EMBEDDING_FALLBACK_FASTEMBED", "false").lower() != "true":
        return None

    if not _fastembed_load_attempted:
        _fastembed_load_attempted = True
        try:
            from fastembed import TextEmbedding  # type: ignore

            model_name = os.environ.get(
                "FASTEMBED_MODEL",
                "sentence-transformers/all-MiniLM-L6-v2",
            )
            loop = asyncio.get_event_loop()
            _fastembed_client = await loop.run_in_executor(
                None, lambda: TextEmbedding(model_name=model_name)
            )
            # Probe dimension
            import numpy as np  # type: ignore

            vecs = list(_fastembed_client.embed(["hello"]))
            if vecs:
                _fastembed_dim = int(np.array(vecs[0]).shape[0])
            _log(
                "FastEmbed lane loaded: model=%s dim=%s",
                model_name,
                _fastembed_dim,
            )
        except Exception as e:
            _log("FastEmbed lane unavailable: %s", e)
            _fastembed_client = None
            _fastembed_dim = None

    if _fastembed_client is None:
        return None

    try:
        import numpy as np  # type: ignore

        loop = asyncio.get_event_loop()
        vecs = await loop.run_in_executor(
            None, lambda: list(_fastembed_client.embed([text]))
        )
        if vecs:
            return [float(x) for x in np.array(vecs[0])]
    except Exception as e:
        _log("FastEmbed encode failed: %s", e)

    return None


async def get_embedding(text: str) -> Optional[List[float]]:
    """Generate an embedding for a single text.

    Returns None on any failure (network, HTTP, JSON parse, empty response).
    Callers MUST handle None — typically by storing NULL in the embedding
    column and letting retrieval fall back to BM25/Jaccard.

    Two-tier:
      1. Check the in-process LRU cache (sha256-keyed). Hit → instant return.
      2. Try Ollama. On failure, set the down-latch (30s cooldown) and
         return None (the FastEmbed lane is wired but disabled by default
         because it produces a different dimension than the stored
         pgvector columns — see module docstring).
    """
    if not text or not text.strip():
        return None

    # 1. Cache check
    cached = _cache_get(text)
    if cached is not None:
        return cached

    _ensure_resolved()
    if not _ollama_url or not _resolved_model:
        _log("not configured, skipping embedding")
        return None

    # 2. Check the down-latch — if Ollama embedding was recently down,
    # skip the probe (avoids paying connect timeout on every call).
    import time as _time

    global _ollama_embed_down, _ollama_down_until
    now = _time.monotonic()
    if _ollama_embed_down and now < _ollama_down_until:
        # Ollama still in cooldown — try FastEmbed as a last resort.
        fb = await _try_fastembed(text)
        if fb is not None:
            return fb
        return None

    # 3. Acquire the semaphore so we don't fire more than `concurrency`
    # requests at Ollama simultaneously.
    sem = _get_semaphore()
    async with sem:
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(
                    f"{_ollama_url}/api/embeddings",
                    json={"model": _resolved_model, "prompt": text},
                )
                response.raise_for_status()
                data = response.json()
                embedding = data.get("embedding")
                if isinstance(embedding, list) and embedding:
                    _cache_put(text, embedding)
                    # Ollama recovered — clear the down latch.
                    if _ollama_embed_down:
                        clear_ollama_down_latch()
                        _log("Ollama embedding recovered — latch cleared")
                    return embedding
                _log("empty embedding in response for text: %s", text[:60])
                return None
        except Exception as e:
            _log("ollama embedding failed for text '%s...': %s", text[:60], e)
            # Trip the latch so we don't re-probe for the cooldown period.
            _ollama_embed_down = True
            _ollama_down_until = now + _OLLAMA_DOWN_COOLDOWN
            # Try FastEmbed as a last resort (only active if explicitly enabled).
            fb = await _try_fastembed(text)
            if fb is not None:
                return fb
            return None


async def get_embeddings(texts: List[str]) -> List[Optional[List[float]]]:
    """Generate embeddings for a batch of texts.

    Returns a list parallel to ``texts`` — each element is either a list of
    floats or None if that specific embedding failed. Maintains order.

    Implemented as concurrent single-text calls (Ollama's /api/embeddings
    endpoint takes one prompt at a time), GATED by a semaphore so we
    don't overwhelm Ollama. Cache hits skip the semaphore entirely.

    For a 50-chunk digestion with a warm cache this can be near-instant;
    cold it runs ~5 at a time instead of 50 at once — slightly slower in
    wall-clock but vastly more reliable.
    """
    if not texts:
        return []

    # Serve cache hits synchronously, queue misses for concurrent fetch.
    results: List[Optional[List[float]]] = [None] * len(texts)
    miss_indices: List[int] = []
    miss_texts: List[str] = []

    for i, t in enumerate(texts):
        cached = _cache_get(t) if t and t.strip() else None
        if cached is not None:
            results[i] = cached
        else:
            miss_indices.append(i)
            miss_texts.append(t)

    if miss_texts:
        _log(
            "embedding %d texts (%d cache hits, %d misses, semaphore=%s)",
            len(texts),
            len(texts) - len(miss_texts),
            len(miss_texts),
            os.environ.get("EMBEDDING_CONCURRENCY", "5"),
        )
        fetched = await asyncio.gather(
            *(get_embedding(t) for t in miss_texts), return_exceptions=False
        )
        for idx, emb in zip(miss_indices, fetched):
            results[idx] = emb

    return results
