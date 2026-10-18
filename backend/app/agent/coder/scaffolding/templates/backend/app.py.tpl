from contextlib import asynccontextmanager

from fastapi import FastAPI

from backend.services import Services

# FEATURE_IMPORTS


def create_app():
    services = Services()

    @asynccontextmanager
    async def lifespan(app):
        try:
            await services.startup()
            yield
        finally:
            await services.close()

    app = FastAPI(title=APP_TITLE, lifespan=lifespan)
    app.state.services = services
    # FEATURE_INSTALLERS
    # FEATURE_ROUTERS

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    return app


app = create_app()
