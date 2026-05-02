"""
Centralised debug-logging utilities for RealOpen-AI backend.

When the environment variable ``DEBUG=true`` (case-insensitive), every
module that calls ``get_debug_logger()`` will emit verbose log messages
at DEBUG level.  When DEBUG is false/missing, those loggers are silent.

Usage in any module::

    from app.core.logger import get_debug_logger
    dbg = get_debug_logger(__name__)

    async def my_function():
        dbg("Entering my_function")
        ...
        dbg("Leaving my_function result=%s", result)
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable

# ── Public helpers ──────────────────────────────────────────────────

_is_debug: bool = os.getenv("DEBUG", "false").strip().lower() in ("true", "1", "yes")


def is_debug() -> bool:
    """Return whether DEBUG mode is active."""
    return _is_debug


def get_debug_logger(name: str) -> Callable[..., None]:
    """Return a callable that logs at DEBUG level when DEBUG=true.

    The returned callable accepts ``(msg, *args)`` — same signature as
    ``logging.Logger.debug``.  When DEBUG is false the callable is a
    no-op so there is zero runtime overhead.
    """
    logger = logging.getLogger(name)

    if _is_debug:

        def _log(msg: str, *args: Any) -> None:
            logger.debug(msg, *args)

        return _log
    else:

        def _noop(msg: str, *args: Any) -> None:
            pass

        return _noop


class RequestTimer:
    """Simple context-manager that logs the elapsed time of a block."""

    def __init__(self, label: str, logger: logging.Logger | None = None):
        self.label = label
        self.logger = logger or logging.getLogger("app.debug.timer")
        self.start: float = 0.0

    def __enter__(self) -> "RequestTimer":
        self.start = time.perf_counter()
        if _is_debug:
            self.logger.debug("⏱  START  %s", self.label)
        return self

    def __exit__(self, *exc: Any) -> None:
        elapsed_ms = (time.perf_counter() - self.start) * 1000
        if _is_debug:
            self.logger.debug("⏱  END    %s  (%.1f ms)", self.label, elapsed_ms)
