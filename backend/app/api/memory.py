"""
Memory API routes for RealOpen-AI.

FastAPI router with prefix /api/memory. Provides CRUD, search, pin/unpin,
and audit endpoints for the memory management system.
"""

import logging
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db.session import async_session_factory
from app.services.memory import MemoryManager
from app.services.memory_extractor import audit_memories

logger = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------


class MemoryAddRequest(BaseModel):
    text: str
    category: str = "fact"
    source: str = "user"


class MemoryUpdateRequest(BaseModel):
    text: Optional[str] = None
    category: Optional[str] = None


class MemoryPinRequest(BaseModel):
    pinned: bool


class MemorySearchRequest(BaseModel):
    query: str
    category: Optional[str] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _memory_to_dict(m) -> dict:
    """Convert a Memory ORM object to a dict for API responses."""
    return {
        "id": str(m.id),
        "text": m.text,
        "category": m.category,
        "source": m.source,
        "pinned": m.pinned,
        "uses": m.uses,
        "conversation_id": str(m.conversation_id) if m.conversation_id else None,
        "created_at": int(m.created_at.timestamp() * 1000) if m.created_at else 0,
        "updated_at": int(m.updated_at.timestamp() * 1000) if m.updated_at else 0,
    }


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/memory")
async def list_memories(
    category: Optional[str] = None, limit: int = 100, offset: int = 0
):
    """List all memories with optional category filter."""
    async with async_session_factory() as db:
        manager = MemoryManager()
        memories = await manager.get_all_memories(
            db, category=category, limit=limit, offset=offset
        )
        return {
            "memories": [_memory_to_dict(m) for m in memories],
            "total": len(memories),
        }


@router.get("/memory/categories")
async def get_categories():
    """Get categories with counts."""
    async with async_session_factory() as db:
        manager = MemoryManager()
        categories = await manager.get_categories_with_counts(db)
        return {"categories": categories}


@router.get("/memory/{memory_id}")
async def get_memory(memory_id: str):
    """Get a single memory by ID."""
    async with async_session_factory() as db:
        manager = MemoryManager()
        memory = await manager.get_memory_by_id(db, memory_id)
        if not memory:
            raise HTTPException(status_code=404, detail="Memory not found")
        return {"memory": _memory_to_dict(memory)}


@router.post("/memory")
async def add_memory(request: MemoryAddRequest):
    """Add a new memory."""
    if not request.text or not request.text.strip():
        raise HTTPException(status_code=400, detail="Memory text cannot be empty")

    async with async_session_factory() as db:
        manager = MemoryManager()

        # Check for duplicates first
        duplicates = await manager.find_duplicates(db, request.text)
        if duplicates:
            return {
                "ok": True,
                "message": "Memory already exists",
                "duplicate_of": str(duplicates[0].id),
            }

        try:
            memory = await manager.add_memory(
                db,
                request.text,
                category=request.category,
                source=request.source,
            )
            await db.commit()
            return {"ok": True, "memory": _memory_to_dict(memory)}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@router.put("/memory/{memory_id}")
async def update_memory(memory_id: str, request: MemoryUpdateRequest):
    """Update an existing memory."""
    async with async_session_factory() as db:
        manager = MemoryManager()
        memory = await manager.update_memory(
            db, memory_id, text=request.text, category=request.category
        )
        if not memory:
            raise HTTPException(status_code=404, detail="Memory not found")
        await db.commit()
        return {"ok": True, "memory": _memory_to_dict(memory)}


@router.delete("/memory/{memory_id}")
async def delete_memory(memory_id: str):
    """Delete a memory by ID."""
    async with async_session_factory() as db:
        manager = MemoryManager()
        deleted = await manager.delete_memory(db, memory_id)
        if not deleted:
            raise HTTPException(status_code=404, detail="Memory not found")
        await db.commit()
        return {"ok": True, "message": "Memory deleted successfully"}


@router.post("/memory/{memory_id}/pin")
async def pin_memory(memory_id: str, request: MemoryPinRequest):
    """Pin or unpin a memory. Pinned memories are always included in context."""
    async with async_session_factory() as db:
        manager = MemoryManager()
        memory = await manager.pin_memory(db, memory_id, request.pinned)
        if not memory:
            raise HTTPException(status_code=404, detail="Memory not found")
        await db.commit()
        return {"ok": True, "pinned": memory.pinned}


@router.post("/memory/search")
async def search_memories(request: MemorySearchRequest):
    """Search memories using Jaccard similarity."""
    async with async_session_factory() as db:
        manager = MemoryManager()
        memories = await manager.search_memories(
            db, request.query, category=request.category, limit=20
        )
        return {
            "memories": [_memory_to_dict(m) for m in memories],
            "total": len(memories),
            "query": request.query,
        }


@router.post("/memory/audit")
async def run_audit():
    """Run memory audit/consolidation via LLM.

    Deduplicates and consolidates memories. Returns before and after counts.
    """
    result = await audit_memories()

    if "error" in result and "before" not in result:
        raise HTTPException(status_code=502, detail=f"Audit failed: {result['error']}")

    return {
        "ok": "error" not in result,
        "before": result.get("before", 0),
        "after": result.get("after", 0),
        "removed": result.get("before", 0) - result.get("after", 0),
        "already_tidy": bool(result.get("already_tidy")),
    }
