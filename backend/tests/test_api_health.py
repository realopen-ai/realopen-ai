"""
Tests for the health-check router (app/api/health.py).

Scope:
- GET /api/health returns 200 with the exact status dict
  {"status": "healthy", "service": "RealOpen-AI", "version": "0.1.0"}.
- Response content type is JSON.
- The health_check coroutine can be called directly (pure unit).
- Router shape: exactly one route, path "/health", method GET only.
- Unmatched paths 404; non-GET methods on /health are rejected with 405.

Mocking: none required — the endpoint is a static dict with no I/O.
The FastAPI app is built per-test with `include_router(..., prefix="/api")`
and driven through httpx's ASGITransport (in-process, no network).
"""

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.api import health


def _build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(health.router, prefix="/api")
    return app


@pytest.mark.asyncio
async def test_health_endpoint_returns_200_and_status_dict():
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/health")

    assert resp.status_code == 200
    assert resp.json() == {
        "status": "healthy",
        "service": "RealOpen-AI",
        "version": "0.1.0",
    }


@pytest.mark.asyncio
async def test_health_endpoint_content_type_is_json():
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/health")

    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")


@pytest.mark.asyncio
async def test_health_endpoint_404_for_unknown_path():
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.get("/api/health/extra")

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Not Found"


@pytest.mark.asyncio
async def test_health_endpoint_rejects_post_with_405():
    app = _build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post("/api/health", json={"anything": True})

    assert resp.status_code == 405
    assert resp.json()["detail"] == "Method Not Allowed"


@pytest.mark.asyncio
async def test_health_check_callable_directly():
    """The handler itself is a plain coroutine — call it without any HTTP layer."""
    result = await health.health_check()
    assert result == {
        "status": "healthy",
        "service": "RealOpen-AI",
        "version": "0.1.0",
    }


def test_router_has_exactly_one_get_route():
    routes = list(health.router.routes)
    assert len(routes) == 1, f"expected 1 route, got {len(routes)}"

    route = routes[0]
    assert isinstance(route, APIRoute)
    assert route.path == "/health"
    assert route.methods == {"GET"}
    assert route.name == "health_check"
