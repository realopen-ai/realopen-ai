import asyncio
import logging
import uuid
from contextlib import asynccontextmanager, suppress
from pathlib import Path

# Optional voice runtimes live in a durable, platform/Python-keyed target.
# Activate it before importing routers or the voice manager so a container
# rebuild only needs to re-use the persisted packages, never reinstall them.
from app.services.pip_persistence import activate_persistent_site_packages

activate_persistent_site_packages()

# noqa: E402 — FastAPI imports must come after pip_persistence activation #
from fastapi import FastAPI, Query, WebSocket  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from prometheus_fastapi_instrumentator import Instrumentator  # noqa: E402
from prometheus_client import make_asgi_app  # noqa: E402

from app.config import settings  # noqa: E402
from app.core import metrics as app_metrics  # noqa: E402
from app.api.health import router as health_router  # noqa: E402
from app.api.chat import router as chat_router  # noqa: E402
from app.api.models import router as models_router  # noqa: E402
from app.api.modules import router as modules_router  # noqa: E402
from app.api.setup import router as setup_router  # noqa: E402
from app.api.memory import router as memory_router  # noqa: E402
from app.api.documents import router as documents_router  # noqa: E402
from app.api.reports import router as reports_router  # noqa: E402
from app.api.workspace import router as workspace_router  # noqa: E402
from app.api.flashcards import router as flashcards_router  # noqa: E402
from app.api.notes import router as notes_router  # noqa: E402
from app.api.quizzes import router as quizzes_router  # noqa: E402
from app.api.artifacts import router as artifacts_router  # noqa: E402
from app.api.notebooks import router as notebooks_router  # noqa: E402
from app.api.deps import router as deps_router  # noqa: E402
from app.api.providers import router as providers_router  # noqa: E402
from app.api.tools import router as tools_router  # noqa: E402
from app.api.skills import router as skills_router  # noqa: E402
from app.api.voice import router as voice_router  # noqa: E402
from app.api.sandboxes import router as sandboxes_router, terminal_proxy  # noqa: E402
from app.core.logger import is_debug  # noqa: E402
from app.core.middleware import DebugLoggingMiddleware  # noqa: E402

logger = logging.getLogger(__name__)

# Voice chat (real-time WebSocket pipeline). The import is guarded so the
# app still boots when the voice package (or one of its imports) is
# unavailable — the /ws/voice route is simply not registered and text chat
# is unaffected.
try:
    from app.voice.manager import voice_manager
except Exception as _voice_import_error:  # noqa: BLE001 — voice is optional
    logger.warning(
        "Voice package unavailable — /ws/voice disabled: %s", _voice_import_error
    )
    voice_manager = None


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
                # Auto-generate schematic thumbnail
                thumbnail_b64 = None
                try:
                    from app.services.thumbnail_gen import generate_schematic_thumbnail

                    thumbnail_b64 = generate_schematic_thumbnail(
                        pptx_path, meta["display_name"]
                    )
                except Exception as e:
                    logger.warning("Thumbnail generation failed for %s: %s", slug, e)

                t = Template(
                    id=uuid.uuid4(),
                    display_name=meta["display_name"],
                    slug=slug,
                    description=meta["description"],
                    tags=meta["tags"],
                    thumbnail=thumbnail_b64,
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
        # Resolve from the backend root, not the process working directory.
        # Development runs Uvicorn from ``/app/app`` so its reload watcher
        # cannot recursively watch the persistent voice-package target.
        backend_root = Path(__file__).resolve().parents[1]
        alembic_ini = backend_root / "alembic.ini"
        alembic_cfg = AlembicConfig(str(alembic_ini))
        alembic_cfg.set_main_option("script_location", str(backend_root / "alembic"))
        alembic_cfg.set_main_option("prepend_sys_path", str(backend_root))
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

    # Seed + load tool configurations (Brain ▸ Tools). Discovers every
    # registered tool, seeds a default configuration row for the ones
    # missing one (existing rows are never overwritten), and fills the
    # runtime config cache from the database.
    try:
        from app.agent.tools import config_store as tool_config_store

        result = await tool_config_store.seed_and_load()
        logger.info(
            "🧰 Tool configurations: %d seeded, %d loaded",
            result["seeded"],
            result["loaded"],
        )
    except Exception as e:
        logger.warning("Tool configuration seeding failed: %s", e)

    # Reinstall any optional deps that are in the manifest but missing from
    # the system (e.g. after a container rebuild). Uses apt-get install
    # --no-download to reinstall from cached .debs.
    # Debian handles dependency resolution, triggers, ldconfig, etc.
    try:
        from app.services.deps_manager import verify_deps_on_startup

        await verify_deps_on_startup()
    except Exception as e:
        logger.warning("Dependency verification on startup failed: %s", e)

    # Voice packages are activated from the durable target at module import.
    # Missing packages are deliberately installed only by the setup flow:
    # application startup must remain offline and must never be held hostage
    # by a package index or model host.

    async def idle_sandbox_monitor():
        from datetime import datetime
        from sqlalchemy import select
        from app.db.models import Sandbox, SandboxTask
        from app.db.session import async_session_factory
        from app.services import sandbox_host

        while True:
            await asyncio.sleep(30)
            try:
                async with async_session_factory() as db:
                    rows = (
                        (
                            await db.execute(
                                select(Sandbox).where(
                                    Sandbox.status == "running",
                                    Sandbox.desired_running.is_(True),
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    now = datetime.utcnow()
                    for item in rows:
                        active = await db.scalar(
                            select(SandboxTask.id).where(
                                SandboxTask.sandbox_id == item.id,
                                SandboxTask.status.in_(["queued", "running"]),
                            )
                        )
                        idle = (
                            item.last_active_at
                            and (now - item.last_active_at).total_seconds()
                            >= item.idle_timeout_seconds
                        )
                        if idle and not active:
                            await sandbox_host.call("stop", item)
                            item.status = "stopped"
                    await db.commit()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("Sandbox idle monitor: %s", exc)

    sandbox_monitor = asyncio.create_task(idle_sandbox_monitor())
    yield

    # Shutdown
    sandbox_monitor.cancel()
    with suppress(asyncio.CancelledError):
        await sandbox_monitor


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
app.include_router(flashcards_router, prefix="/api")
app.include_router(notes_router, prefix="/api")
app.include_router(quizzes_router, prefix="/api")
app.include_router(artifacts_router, prefix="/api")
app.include_router(notebooks_router, prefix="/api")
app.include_router(deps_router, prefix="/api", tags=["dependencies"])
app.include_router(providers_router, prefix="/api", tags=["providers"])
app.include_router(tools_router, prefix="/api", tags=["tools"])
app.include_router(skills_router, prefix="/api", tags=["skills"])
app.include_router(voice_router, prefix="/api", tags=["voice"])
app.include_router(sandboxes_router, prefix="/api")


@app.websocket("/api/sandboxes/{sandbox_id}/terminal")
async def sandbox_terminal_socket(websocket: WebSocket, sandbox_id: str):
    from app.db.session import async_session_factory

    async with async_session_factory() as db:
        await terminal_proxy(websocket, sandbox_id, db)


# ── Voice chat WebSocket (protocol v1) ─────────────────────────────
if voice_manager is not None:

    @app.websocket("/ws/voice")
    async def voice_ws(websocket: WebSocket, conversation_id: str = Query(...)) -> None:
        """Real-time voice session for one conversation.

        The URL carries the conversation id (the registry key for the
        one-session-per-conversation invariant); the ``start`` handshake
        re-validates it. See app/voice/session.py for the frame contract.
        """
        await websocket.accept()
        session = await voice_manager.get_or_create(conversation_id, websocket)
        if session is None:
            # Rejected (session_exists / invalid id) — the error frame and
            # close were sent by the manager.
            return
        try:
            await session.handle(websocket)
        finally:
            voice_manager.remove(conversation_id)
