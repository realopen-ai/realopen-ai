"""Independent catalog and runtime registries. Add an adapter here, not UI branches."""

from .huggingface_catalog import HuggingFaceCatalog
from .ollama_catalog import OllamaCatalog
from .local_catalog import LocalCatalog
from .ollama_runtime import OllamaRuntime
from .types import Catalog, ModelManagerError, Runtime

CATALOGS: dict[str, Catalog] = {
    item.id: item for item in (OllamaCatalog(), HuggingFaceCatalog(), LocalCatalog())
}


def runtime_adapters() -> dict[str, Runtime]:
    return {"ollama": OllamaRuntime()}


def catalog_adapter(ident: str) -> Catalog:
    if ident not in CATALOGS:
        raise ModelManagerError("Unknown model catalog.")
    return CATALOGS[ident]
