from fastapi import APIRouter

from app.core.logger import is_debug

router = APIRouter()


@router.get("/health")
async def health_check():
    """Basic health check endpoint."""
    if is_debug():
        print("[health] GET /api/health", flush=True)
    return {
        "status": "healthy",
        "service": "RealOpen-AI",
        "version": "0.1.0",
    }
