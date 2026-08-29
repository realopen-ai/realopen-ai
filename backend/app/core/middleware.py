"""
FastAPI middleware that logs every incoming request and its response
when the ``DEBUG`` environment variable is set to ``true``.

Logged information includes:
- HTTP method & path
- Query parameters
- Request body (truncated to 2 000 chars)
- Response status code
- Elapsed wall-clock time

IMPORTANT: This is implemented as a **pure ASGI middleware** instead of
Starlette's BaseHTTPMiddleware. The latter wraps the response body in a
way that consumes the entire StreamingResponse before sending it to the
client, which breaks SSE streaming - events arrive all at once instead
of progressively. A pure ASGI middleware simply intercepts the send
calls and passes them through without buffering.

IMPORTANT 2: When reading the request body for logging, we must NOT
consume the ``receive`` callable permanently — the downstream app still
needs to read the body.  After reading the body, we replace ``receive``
with a replay callable that yields the cached body bytes and then
signals end-of-body.
"""

from __future__ import annotations

import time

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.core.logger import is_debug

MAX_BODY_LOG = 2000  # characters


class DebugLoggingMiddleware:
    """Pure ASGI middleware that logs every request/response when DEBUG=true.

    Unlike BaseHTTPMiddleware, this does NOT consume the response body,
    so StreamingResponse works correctly and SSE events are sent
    progressively.

    Unlike a naive ASGI middleware that reads the body with
    ``request.body()``, this one replays the body so the downstream
    app can still read it.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if not is_debug():
            await self.app(scope, receive, send)
            return

        # Build a Request object to read method/path/body
        request = Request(scope, receive)
        method = request.method
        path = request.url.path
        query = str(request.query_params) or "(none)"

        # Read request body for POST/PUT/PATCH (for logging only)
        body_str = ""
        downstream_receive: Receive = receive  # default: pass through
        if method in ("POST", "PUT", "PATCH"):
            try:
                raw = await request.body()
                body_str = raw.decode("utf-8", errors="replace")[:MAX_BODY_LOG]
                # Replace receive with a replay callable so the downstream
                # app can still read the body.  We yield the cached body
                # bytes on the first call, then signal end-of-body.
                downstream_receive = _make_body_replay(raw, receive)
            except Exception:
                body_str = "<could not read body>"

        if path != "/api/health":
            print(
                f"[middleware] 🔵 REQUEST  {method} {path}  query={query}  "
                f"body={_truncate(body_str, MAX_BODY_LOG)}",
                flush=True,
            )

        start = time.perf_counter()
        status_code = None

        # Wrap the `send` callable to capture the response status code
        async def send_with_logging(message: dict) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message.get("status", 0)
            await send(message)

        try:
            await self.app(scope, downstream_receive, send_with_logging)
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start) * 1000
            print(
                f"[middleware] ❌ EXCEPTION  {method} {path}  ({elapsed_ms:.1f} ms)  error={exc}",
                flush=True,
            )
            raise

        elapsed_ms = (time.perf_counter() - start) * 1000

        if path != "/api/health":
            print(
                f"[middleware] ⬅️  RESPONSE {method} {path}  status={status_code}  ({elapsed_ms:.1f} ms)",
                flush=True,
            )


def _make_body_replay(body: bytes, original_receive: Receive) -> Receive:
    """Return a ``receive`` callable that replays the already-read body.

    The ASGI spec requires the server to call ``receive()`` to get
    ``http.request`` messages containing the body, followed by a final
    ``http.disconnect``.  Since we already consumed the real ``receive``,
    we create a replay callable that yields the cached body in one chunk
    on the first call, then delegates to the ORIGINAL ``receive`` for
    subsequent calls (so that ``http.disconnect`` is only sent when the
    actual client disconnects, not immediately).

    IMPORTANT: If we returned ``http.disconnect`` on the second call,
    Starlette's StreamingResponse would abort the stream immediately,
    because it checks for disconnect signals between body chunks.
    By delegating to the original receive, the stream continues until
    the real client disconnects.
    """
    body_sent = False

    async def replay() -> dict:
        nonlocal body_sent
        if not body_sent:
            body_sent = True
            return {
                "type": "http.request",
                "body": body,
                "more_body": False,
            }
        # Delegate to the original receive so that http.disconnect is
        # only sent when the real client disconnects (not immediately).
        # This prevents StreamingResponse from aborting prematurely.
        return await original_receive()

    return replay


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[:max_len] + f"... ({len(text)} chars total)"
