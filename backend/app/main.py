import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator

from app.config import settings
from app.api.health import router as health_router
from app.api.chat import router as chat_router
from app.api.models import router as models_router
from app.core.logger import is_debug
from app.core.middleware import DebugLoggingMiddleware

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown events."""
    if is_debug():
        logger.debug("🔧 DEBUG mode is ON — verbose logging enabled")

    # Import tools to register them in the global registry
    import app.agent.tools  # noqa: F401 — registers WebSearchTool, VisionTool, CodeExecTool

    if is_debug():
        from app.agent.base import get_tool_registry

        logger.debug(
            "🔧 Tools registered: %s",
            [t.name for t in get_tool_registry().all_tools()],
        )

    # Run database migrations on startup
    from alembic.config import Config as AlembicConfig
    from alembic import command

    try:
        alembic_cfg = AlembicConfig("alembic.ini")
        alembic_cfg.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
        command.upgrade(alembic_cfg, "head")
        if is_debug():
            logger.debug("🔧 Alembic migrations applied successfully")
    except Exception as e:
        logger.warning("Database migration on startup failed: %s", e)

    yield

    # Shutdown
    pass


app = FastAPI(
    title=settings.APP_NAME,
    version="0.1.0",
    lifespan=lifespan,
    docs_url="/api/docs",
    redoc_url="/api/redoc",
    openapi_url="/api/openapi.json",
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Debug request/response logging middleware (no-op when DEBUG=false)
app.add_middleware(DebugLoggingMiddleware)

# Prometheus metrics
Instrumentator().instrument(app).expose(app, endpoint="/metrics")

# Routes
app.include_router(health_router, prefix="/api", tags=["health"])
app.include_router(chat_router, prefix="/api", tags=["chat"])
app.include_router(models_router, prefix="/api", tags=["models"])
