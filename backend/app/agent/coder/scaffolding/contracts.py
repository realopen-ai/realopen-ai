"""Build-time feature contracts, deliberately independent of stack renderers."""

from dataclasses import dataclass, field
from typing import Callable

from app.agent.coder.scaffolding.spec import AppSpec


@dataclass
class Contribution:
    files: dict[str, str] = field(default_factory=dict)
    backend_dependencies: tuple[str, ...] = ()
    backend_dev_dependencies: tuple[str, ...] = ()
    backend_installers: tuple[str, ...] = ()
    backend_routers: tuple[str, ...] = ()
    frontend_plugins: tuple[str, ...] = ()
    frontend_boundaries: tuple[str, ...] = ()
    frontend_mounts: tuple[str, ...] = ()
    frontend_dependencies: dict[str, str] = field(default_factory=dict)
    frontend_dev_dependencies: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Feature:
    name: str
    render: Callable[[AppSpec], Contribution]
    requires: tuple[str, ...] = ()
    version: int = 1
