"""
Embedding service for the memory system.

Calls Ollama's /api/embeddings endpoint with default_embedding configured
in profiles.yml (default: nomic-embed-text:v1.5) to generate vector embeddings
for memory texts and queries.

Used by:
- app.services.memory.MemoryManager (to populate Memory.embedding on insert/update)
- app.services.memory.MemoryManager.get_relevant_memories (to embed the query
  for pgvector cosine similarity search)
- app.services.memory_extractor (to embed new facts for vector dedup)

Design:
- Module-level singleton with lazy init — embeddings are stateless, share
  one client across the process.
- Per-call try/except so a transient Ollama failure degrades gracefully
  (caller falls back to BM25-only retrieval or Jaccard-only dedup).
- Returns None on failure instead of raising — callers must handle.
"""

import logging
from typing import List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Singleton state — initialized lazily on first use
_resolved_model: Optional[str] = None
_ollama_url: Optional[str] = None


def _ensure_resolved() -> None:
    """Resolve the embedding model ID and Ollama URL once, cache for the process."""
    global _resolved_model, _ollama_url
    if _resolved_model is None:
        _resolved_model = settings.resolve_model(settings.MEMORY_EMBEDDING_MODEL_ROLE)
        _ollama_url = settings.OLLAMA_BASE_URL
        logger.info(
            "[embeddings] resolved embedding model=%s url=%s",
            _resolved_model,
            _ollama_url,
        )


def get_embedding_model() -> str:
    """Return the resolved Ollama embedding model ID (e.g. 'nomic-embed-text:v1.5')."""
    _ensure_resolved()
    return _resolved_model or "nomic-embed-text:v1.5"


async def get_embedding(text: str) -> Optional[List[float]]:
    """Generate an embedding for a single text.

    Returns None on any failure (network, HTTP, JSON parse, empty response).
    Callers MUST handle None — typically by storing NULL in the embedding
    column and letting retrieval fall back to BM25/Jaccard.
    """
    if not text or not text.strip():
        return None

    _ensure_resolved()
    if not _ollama_url or not _resolved_model:
        logger.debug("[embeddings] not configured, skipping")
        return None

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{_ollama_url}/api/embeddings",
                json={"model": _resolved_model, "prompt": text},
            )
            response.raise_for_status()
            data = response.json()
            embedding = data.get("embedding")
            if isinstance(embedding, list) and embedding:
                return embedding
            logger.warning(
                "[embeddings] empty embedding in response for text: %s",
                text[:60],
            )
            return None
    except Exception as e:
        logger.warning(
            "[embeddings] failed for text '%s...': %s",
            text[:60],
            e,
        )
        return None


async def get_embeddings(texts: List[str]) -> List[Optional[List[float]]]:
    """Generate embeddings for a batch of texts.

    Returns a list parallel to `texts` — each element is either a list of
    floats or None if that specific embedding failed. Maintains order.

    Implemented as parallel single-text calls (Ollama's /api/embeddings
    endpoint takes one prompt at a time). For very large batches this could
    be optimized with a semaphore, but memory extraction typically processes
    ≤5 facts at a time so the simple approach is fine.
    """
    import asyncio

    if not texts:
        return []

    # Run all calls concurrently — embedding calls are independent
    results = await asyncio.gather(
        *(get_embedding(t) for t in texts), return_exceptions=False
    )
    return list(results)
