"""
FastAPI middleware that logs every incoming request and its response
when the ``DEBUG`` environment variable is set to ``true``.

Logged information includes:
- HTTP method & path
- Query parameters
- Request headers (except Authorization / Cookie)
- Request body (truncated to 2 000 chars)
- Response status code
- Elapsed wall-clock time

The middleware is **zero-overhead** when DEBUG=false because the
``@app.middleware("request")`` handler returns immediately without
reading the body.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from app.core.logger import is_debug

logger = logging.getLogger("app.debug.middleware")

# Headers we intentionally skip (sensitive)
_SKIP_HEADERS = {"authorization", "cookie", "set-cookie"}

MAX_BODY_LOG = 2000  # characters


class DebugLoggingMiddleware(BaseHTTPMiddleware):
    """Log every request/response pair when DEBUG=true."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # Fast-path when debug is off
        if not is_debug():
            return await call_next(request)

        start = time.perf_counter()

        # ── Log the request ──────────────────────────────────────
        method = request.method
        path = request.url.path
        query = str(request.query_params) or "(none)"

        safe_headers: Dict[str, str] = {
            k: v for k, v in request.headers.items() if k.lower() not in _SKIP_HEADERS
        }

        body_str = ""
        if method in ("POST", "PUT", "PATCH"):
            try:
                raw = await request.body()
                body_str = raw.decode("utf-8", errors="replace")[:MAX_BODY_LOG]
            except Exception:
                body_str = "<could not read body>"

        logger.debug(
            "➡️  REQUEST  %s %s  query=%s  headers=%s  body=%s",
            method,
            path,
            query,
            _truncate(json.dumps(safe_headers), 500),
            _truncate(body_str, MAX_BODY_LOG),
        )

        # ── Call the next handler ────────────────────────────────
        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            logger.debug(
                "❌ EXCEPTION  %s %s  (%.1f ms)  error=%s",
                method,
                path,
                elapsed_ms,
                exc,
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000

        # ── Log the response ─────────────────────────────────────
        logger.debug(
            "⬅️  RESPONSE %s %s  status=%d  (%.1f ms)",
            method,
            path,
            response.status_code,
            elapsed_ms,
        )

        return response


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"… ({len(text)} chars total)"
