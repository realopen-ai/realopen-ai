import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_fastapi_instrumentator import Instrumentator
from prometheus_client import make_asgi_app

from app.config import settings
from app.core import metrics as app_metrics
from app.api.health import router as health_router
from app.api.chat import router as chat_router
from app.api.models import router as models_router
from app.api.modules import router as modules_router
from app.api.setup import router as setup_router
from app.api.memory import router as memory_router
from app.api.documents import router as documents_router
from app.api.reports import router as reports_router
from app.api.workspace import router as workspace_router
from app.core.logger import is_debug
from app.core.middleware import DebugLoggingMiddleware

logger = logging.getLogger(__name__)


def _get_data_dir() -> Path:
    """Get the data directory for persistent files.

    In Docker: /app/data (mounted volume)
    In dev: backend/../data
    """
    candidates = [
        Path("/app/data"),
        Path(__file__).parent.parent.parent / "data",
    ]
    for d in candidates:
        try:
            d.mkdir(parents=True, exist_ok=True)
            return d
        except OSError:
            continue
    # Fallback
    fallback = Path("/app/data")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _detect_setup_mode() -> bool:
    """Detect whether the app should enter setup mode.

    Setup mode is triggered when:
    - The data/.setup-complete marker file does NOT exist
    - The SETUP_MODE env var is explicitly set to true

    The marker file is created by the setup wizard when setup completes.
    """
    # Explicit env var override
    if settings.SETUP_MODE:
        return True

    # Check for data/.setup-complete marker
    data_dir = _get_data_dir()
    marker = data_dir / ".setup-complete"

    if marker.exists():
        return False  # Setup already completed

    # No marker found — need setup
    logger.info("No data/.setup-complete marker found — entering setup mode")
    return True


def _apply_module_config() -> None:
    """Apply module configuration by registering/unregistering tools.

    All tools are imported and registered at startup. This function
    then unregisters tools belonging to disabled modules, keeping
    them in the registry's backup so they can be restored on toggle.
    """
    from app.agent.base import get_tool_registry

    registry = get_tool_registry()
    modules = settings.get_modules()

    # Get all tools that belong to disabled modules
    disabled_tools = []
    for name, module in modules.items():
        if not settings.is_module_enabled(name):
            disabled_tools.extend(module.tools)

    if disabled_tools:
        removed = registry.unregister_many(disabled_tools)
        if is_debug():
            logger.debug(
                "🔧 Module config: unregistered %d tools from disabled modules: %s",
                removed,
                disabled_tools,
            )

    # Log enabled module tools
    enabled_tools = []
    for name, module in modules.items():
        if settings.is_module_enabled(name):
            for tool_name in module.tools:
                if registry.has_tool(tool_name):
                    enabled_tools.append(tool_name)

    if is_debug():
        logger.debug(
            "🔧 Module config: active tools: %s",
            enabled_tools,
        )


async def _seed_default_templates():
    """Seed default PPTX templates into the DB if the table is empty.

    Checks if the templates table has any rows. If empty, inserts rows
    for each .pptx file found in backend/app/templates/pptx/ that matches
    the known default templates (corporate, modern, elegant).
    """
    from app.db.session import async_session_factory
    from app.db.models import Template
    from sqlalchemy import select
    from datetime import datetime
    from pathlib import Path

    templates_dir = Path(__file__).resolve().parent / "templates" / "pptx"
    defaults = {
        "corporate": {
            "display_name": "Corporate",
            "description": "Navy blue professional theme",
            "tags": ["corporate", "professional", "navy"],
        },
        "modern": {
            "display_name": "Modern",
            "description": "Teal and orange vibrant theme",
            "tags": ["modern", "vibrant", "teal"],
        },
        "elegant": {
            "display_name": "Elegant",
            "description": "Dark purple and gold sophisticated theme",
            "tags": ["elegant", "sophisticated", "dark"],
        },
    }

    async with async_session_factory() as db:
        # Check if any templates exist
        result = await db.execute(select(Template).limit(1))
        if result.scalar_one_or_none():
            return  # Table already has templates

        # Seed defaults
        for slug, meta in defaults.items():
            pptx_path = templates_dir / f"{slug}.pptx"
            if pptx_path.exists():
                t = Template(
                    id=uuid.uuid4(),
                    display_name=meta["display_name"],
                    slug=slug,
                    description=meta["description"],
                    tags=meta["tags"],
                    thumbnail=None,
                    path=f"{slug}.pptx",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
                db.add(t)
                logger.info("📦 Seeded template: %s (%s)", meta["display_name"], slug)

        await db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Application lifespan: startup and shutdown events."""
    if is_debug():
        logger.debug("🔧 DEBUG mode is ON — verbose logging enabled")

    # Detect setup mode
    setup_mode = _detect_setup_mode()
    if setup_mode:
        logger.info("🚀 Running in SETUP MODE — web setup wizard will be shown")
        # In setup mode, we still need the DB and basic tools,
        # but we skip the full module configuration
    else:
        logger.info("✅ Setup complete — running normally")

    # Import tools to register them in the global registry
    import app.agent.tools  # noqa: F401 — registers all tools

    if is_debug():
        from app.agent.base import get_tool_registry

        logger.debug(
            "🔧 Tools registered (before module filter): %s",
            [t.name for t in get_tool_registry().all_tools()],
        )

    # Apply module configuration — unregister tools from disabled modules
    # Skip in setup mode since modules haven't been configured yet
    if not setup_mode:
        _apply_module_config()

    if is_debug():
        from app.agent.base import get_tool_registry

        logger.debug(
            "🔧 Tools active (after module filter): %s",
            [t.name for t in get_tool_registry().all_tools()],
        )

    # Log module status
    for name, module in settings.get_modules().items():
        enabled = settings.is_module_enabled(name)
        available = module.is_available_for_profile(settings.HARDWARE_PROFILE)
        logger.info(
            "📦 Module '%s': enabled=%s, available=%s, required=%s",
            name,
            enabled,
            available,
            module.required,
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

    # Seed default templates into the DB if the templates table is empty
    try:
        await _seed_default_templates()
    except Exception as e:
        logger.warning("Template seeding failed: %s", e)

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
Instrumentator(
    excluded_handlers=[
        "/metrics",
        "/api/health",
        "/api/docs",
        "/api/redoc",
        "/api/openapi.json",
    ],
).instrument(app)

# Mount a combined metrics endpoint: fastapi instrumentator + custom AI metrics
metrics_app = make_asgi_app(registry=app_metrics.REGISTRY)
app.mount("/metrics", metrics_app)

# Routes
app.include_router(health_router, prefix="/api", tags=["health"])
app.include_router(chat_router, prefix="/api", tags=["chat"])
app.include_router(models_router, prefix="/api", tags=["models"])
app.include_router(modules_router, prefix="/api", tags=["modules"])
app.include_router(setup_router, prefix="/api", tags=["setup"])
app.include_router(memory_router, prefix="/api", tags=["memory"])
app.include_router(documents_router, prefix="/api", tags=["documents"])
app.include_router(reports_router, prefix="/api", tags=["reports"])
app.include_router(workspace_router, prefix="/api", tags=["workspace"])
