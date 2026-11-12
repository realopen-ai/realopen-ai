"""Catalog metadata and runtime contracts; neither assumes Ollama's install protocol."""

from collections.abc import AsyncIterator
from typing import Any, Protocol

from pydantic import BaseModel, Field


class ModelManagerError(Exception):
    """An actionable error whose message is safe to show to the user."""

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ModelFile(BaseModel):
    path: str
    size: int = Field(ge=0)
    sha256: str | None = None


class Variant(BaseModel):
    id: str
    label: str
    format: str
    size: int | None = None
    quantization: str | None = None
    files: list[ModelFile] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class CatalogModel(BaseModel):
    catalog: str
    id: str
    title: str
    url: str
    description: str = ""
    revision: str | None = None
    license: str | None = None
    gated: bool = False
    variants: list[Variant] = Field(default_factory=list)


class InstallRequest(BaseModel):
    catalog: str = Field(max_length=60)
    model: str = Field(min_length=1, max_length=240)
    variant: str = Field(min_length=1, max_length=300)
    runtime: str = Field(default="ollama", max_length=60)
    name: str = Field(min_length=1, max_length=200)
    revision: str | None = Field(default=None, max_length=64)


class Catalog(Protocol):
    id: str
    label: str

    async def search(self, query: str) -> list[CatalogModel]: ...
    async def inspect(
        self, model: str, revision: str | None = None
    ) -> CatalogModel: ...
    def download(
        self, model: CatalogModel, variant: Variant
    ) -> AsyncIterator[dict]: ...


class Runtime(Protocol):
    id: str
    label: str

    def normalize_name(self, name: str) -> str: ...
    async def status(self) -> dict: ...
    async def installed(self) -> list[dict]: ...
    def compatibility(self, model: CatalogModel, variant: Variant) -> dict: ...
    def install(
        self, model: CatalogModel, variant: Variant, name: str, files: dict[str, str]
    ) -> AsyncIterator[dict]: ...
    async def remove(self, name: str) -> None: ...
