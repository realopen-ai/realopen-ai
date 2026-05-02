"""
FastAPI middleware that logs every incoming request and its response
when the ``DEBUG`` environment variable is set to ``true``.

Logged information includes:
- HTTP method & path
- Query parameters
- Request body (truncated to 2 000 chars)
- Response status code
- Elapsed wall-clock time

Uses ``print()`` to ``stdout`` for guaranteed visibility — Python's
logging module can be silently swallowed by uvicorn's log config.
"""

from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.logger import is_debug

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

        body_str = ""
        if method in ("POST", "PUT", "PATCH"):
            try:
                raw = await request.body()
                body_str = raw.decode("utf-8", errors="replace")[:MAX_BODY_LOG]
            except Exception:
                body_str = "<could not read body>"

        print(
            f"🔵 REQUEST  {method} {path}  query={query}  "
            f"body={_truncate(body_str, MAX_BODY_LOG)}",
            flush=True,
        )

        # ── Call the next handler ────────────────────────────────
        try:
            response = await call_next(request)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            print(
                f"❌ EXCEPTION  {method} {path}  ({elapsed_ms:.1f} ms)  error={exc}",
                flush=True,
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000

        # ── Log the response ─────────────────────────────────────
        print(
            f"⬅️  RESPONSE {method} {path}  status={response.status_code}  ({elapsed_ms:.1f} ms)",
            flush=True,
        )

        return response


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"... ({len(text)} chars total)"
