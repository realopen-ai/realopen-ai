"""Curated ONNX downloads using the revisions verified by RealOpen Lens."""

import asyncio
import json
from contextlib import aclosing
from pathlib import Path

from filelock import FileLock, Timeout

from app.services.lens.config import get_lens_config
from app.services.safe_paths import confined_path
from .downloads import cache_root, download_files
from .types import CatalogModel, ModelFile, ModelManagerError, Variant

CATALOG = "lens"
TITLE = {"siglip2": "SigLIP2 Base Patch16 224", "e5": "Multilingual E5 Small"}
MANIFEST = json.loads(
    (Path(__file__).parent.parent / "lens" / "models.lock.json").read_text()
)["models"]


def model_spec(ident: str) -> CatalogModel:
    spec = MANIFEST[ident]
    variants = []
    for precision in ("int8", "fp32") if ident == "siglip2" else ("int8",):
        keys = ["tokenizer", f"text_{precision}"]
        if ident == "siglip2":
            keys.append(f"vision_{precision}")
        files = [ModelFile.model_validate(spec["files"][key]) for key in keys]
        variants.append(
            Variant(
                id=precision,
                label=precision.upper(),
                format="onnx",
                quantization=precision.upper(),
                size=sum(file.size for file in files),
                files=files,
            )
        )
    return CatalogModel(
        catalog=CATALOG,
        id=spec["repo"],
        title=TITLE[ident],
        url=f"https://huggingface.co/{spec['repo']}",
        revision=spec["revision"],
        variants=variants,
    )


def model_paths(ident: str, precision: str) -> dict[str, Path]:
    model = model_spec(ident)
    variant = next(v for v in model.variants if v.id == precision)
    root = cache_root(model)
    try:
        marker = confined_path(root, f"{precision}.installed")
        if not marker.is_file():
            return {}
        files = {item.path: confined_path(root, item.path) for item in variant.files}
        if any(files[item.path].stat().st_size != item.size for item in variant.files):
            return {}
    except (ValueError, OSError):
        return {}
    return files


class LensCatalog:
    id = CATALOG
    label = "Lens"
    searchable = False  # Profile-selected variants have their own install cards.

    async def search(self, query):
        terms = query.casefold().split()
        return [
            model
            for ident in MANIFEST
            if (model := model_spec(ident))
            and all(
                term in f"{model.title} {model.id} Lens".casefold() for term in terms
            )
        ]

    async def inspect(self, model, revision=None):
        for ident, spec in MANIFEST.items():
            if model == spec["repo"] and revision in {None, spec["revision"]}:
                return model_spec(ident)
        raise ModelManagerError("Unknown Lens model or revision.")

    async def download(self, model, variant):
        # Setup and the host model manager share the same partial files.
        # Both precisions also share a tokenizer, so lock per repository.
        lock = FileLock(confined_path(cache_root(model), ".download.lock"), timeout=0)
        while True:
            try:
                lock.acquire()
                break
            except Timeout:
                await asyncio.sleep(0.5)
        try:
            async with aclosing(download_files(model, variant)) as stream:
                async for event in stream:
                    yield event
        finally:
            lock.release()


class LensRuntime:
    id = "lens-onnx"
    label = "Lens ONNX"
    completion_message = "Installed and ready for Lens indexing."

    def normalize_name(self, name):
        if name not in {
            f"{ident}:{variant.id}"
            for ident in MANIFEST
            for variant in model_spec(ident).variants
        }:
            raise ModelManagerError("Choose a supported Lens model and precision.")
        return name

    async def status(self):
        return {
            "id": self.id,
            "label": self.label,
            "available": True,
            "version": None,
            "formats": ["onnx"],
        }

    async def installed(self):
        entries = []
        for ident in MANIFEST:
            for variant in model_spec(ident).variants:
                if await asyncio.to_thread(model_paths, ident, variant.id):
                    entries.append(
                        {
                            "name": f"{ident}:{variant.id}",
                            "runtime": self.id,
                            "family": TITLE[ident],
                            "size": variant.size,
                            "quantization": variant.quantization,
                        }
                    )
        return entries

    def compatibility(self, model, variant):
        supported = model.catalog == CATALOG and variant.format == "onnx"
        return {
            "supported": supported,
            "reason": (
                "Local CPU image retrieval model"
                if supported
                else "Lens accepts its curated retrieval models."
            ),
        }

    async def install(self, model, variant, name, files):
        ident = next(key for key, spec in MANIFEST.items() if spec["repo"] == model.id)
        if name != f"{ident}:{variant.id}" or set(files) != {
            f.path for f in variant.files
        }:
            raise ModelManagerError(
                "Lens model files or installation name do not match."
            )
        marker = confined_path(cache_root(model), f"{variant.id}.installed")
        await asyncio.to_thread(marker.touch)
        yield {"stage": "registering", "message": "Verified Lens model files"}

    async def remove(self, name):
        ident, precision = self.normalize_name(name).split(":")
        marker = confined_path(cache_root(model_spec(ident)), f"{precision}.installed")
        await asyncio.to_thread(marker.unlink, missing_ok=True)


def recommended_lens_models(profile: str | None = None) -> list[dict]:
    config = get_lens_config(profile)
    return [
        {
            "title": model.title,
            "catalog": CATALOG,
            "model": model.id,
            "revision": model.revision,
            "variant": precision,
            "runtime": LensRuntime.id,
            "name": f"{ident}:{precision}",
            "size": next(v.size for v in model.variants if v.id == precision),
        }
        for ident, precision in (("siglip2", config.siglip2), ("e5", config.e5))
        for model in [model_spec(ident)]
    ]
