"""Read Hugging Face metadata without executing repository code."""

import re
from urllib.parse import quote

from .downloads import download_files
from .http import metadata
from .types import CatalogModel, ModelFile, ModelManagerError
from .bundles import discover_variants
from .validation import file_name, repository, revision_id


class HuggingFaceCatalog:
    id = "huggingface"
    label = "Hugging Face"

    async def search(self, query):
        rows = await metadata(
            "https://huggingface.co/api/models",
            params={"search": query, "limit": 30, "sort": "downloads", "direction": -1},
        )
        return [
            CatalogModel(
                catalog=self.id,
                id=repository(row["id"]),
                title=row["id"],
                url="https://huggingface.co/" + row["id"],
                description=row.get("pipeline_tag", ""),
            )
            for row in rows
        ]

    async def inspect(self, model, revision=None):
        model = repository(model)
        revision = revision_id(revision)
        path = quote(model, safe="/") + ("/revision/" + revision if revision else "")
        data = await metadata(
            "https://huggingface.co/api/models/" + path, params={"blobs": "true"}
        )
        pinned = revision_id(data.get("sha"))
        if not pinned:
            raise ModelManagerError(
                "The catalog did not return a pinned model revision."
            )
        files = []
        for row in data.get("siblings", []):
            name = file_name(row["rfilename"])
            lfs = row.get("lfs") or {}
            size = row.get("size", lfs.get("size"))
            if not isinstance(size, int) or size < 0:
                continue
            digest = lfs.get("sha256")
            files.append(
                ModelFile(
                    path=name,
                    size=size,
                    sha256=(
                        digest
                        if digest and re.fullmatch(r"[a-f0-9]{64}", digest)
                        else None
                    ),
                )
            )
        variants = discover_variants(
            files,
            data.get("config") or {},
            mlx=data.get("library_name") == "mlx" or "mlx" in data.get("tags", []),
        )
        return CatalogModel(
            catalog=self.id,
            id=model,
            title=model,
            url="https://huggingface.co/" + model,
            revision=pinned,
            license=(
                (data.get("cardData") or {}).get("license")
                if isinstance((data.get("cardData") or {}).get("license"), str)
                else None
            ),
            gated=bool(data.get("gated")),
            description=data.get("pipeline_tag", ""),
            variants=variants,
        )

    async def download(self, model, variant):
        async for event in download_files(model, variant):
            yield event
