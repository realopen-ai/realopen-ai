"""Shared extension points. No authentication or WebSocket server is enabled by default."""

from dataclasses import dataclass, field
import logging
from typing import Any, Awaitable, Callable

from starlette.concurrency import run_in_threadpool


@dataclass
class Services:
    # Installers run before routers are built. An Auth feature can append Depends
    # objects here, while a WS feature listens to the same committed mutations.
    route_dependencies: list = field(default_factory=list)
    initializers: list[Callable[[], None]] = field(default_factory=list)
    shutdown: list[Callable[[], Awaitable[None]]] = field(default_factory=list)
    mutation_listeners: list[Callable[[dict[str, Any]], Awaitable[None]]] = field(
        default_factory=list
    )

    async def startup(self):
        for initialize in self.initializers:
            await run_in_threadpool(initialize)

    async def close(self):
        for close in reversed(self.shutdown):
            try:
                await close()
            except Exception:
                logging.getLogger(__name__).exception("Extension shutdown failed")

    async def changed(self, entity: str, action: str, record_id: int):
        # Notifications are post-commit. A notification failure must not turn a
        # committed mutation into an apparent failed write (and duplicate retry).
        for listener in self.mutation_listeners:
            try:
                await listener({"entity": entity, "action": action, "id": record_id})
            except Exception:
                logging.getLogger(__name__).exception("Mutation notification failed")
