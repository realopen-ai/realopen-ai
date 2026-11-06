"""Ollama adapter: API-only pulls/imports, with no shell or model-supplied code."""

import asyncio
import json
from pathlib import Path

import httpx

from app.config import settings
from . import ollama_local
from .downloads import cache_root, checksum
from .bundles import SPLIT_GGUF
from .types import ModelManagerError
from .validation import model_name


class OllamaRuntime:
    id = "ollama"
    label = "Ollama"

    def __init__(self, base_url=None):
        self.base_url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
        # This adapter executes on the host, outside Docker's DNS namespace.
        self.local_mlx = False
        if self.base_url == "http://host.docker.internal:11434":
            self.base_url = "http://127.0.0.1:11434"

    def normalize_name(self, name):
        name = model_name(name)
        return name if ":" in name else name + ":latest"

    async def status(self):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(self.base_url + "/api/version")
                response.raise_for_status()
                self.local_mlx = await ollama_local.supported(self.base_url)
                return {
                    "id": self.id,
                    "label": self.label,
                    "available": True,
                    "local_mlx": self.local_mlx,
                    "version": response.json().get("version"),
                    "formats": ["ollama", "gguf", "safetensors"],
                }
        except (httpx.HTTPError, ValueError):
            return {
                "id": self.id,
                "label": self.label,
                "available": False,
                "version": None,
                "formats": ["ollama", "gguf", "safetensors"],
            }

    async def installed(self):
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(self.base_url + "/api/tags")
            response.raise_for_status()
            return [
                {
                    "name": m["name"],
                    "runtime": self.id,
                    "size": m.get("size"),
                    "modified_at": m.get("modified_at"),
                    "quantization": (m.get("details") or {}).get("quantization_level"),
                    "family": (m.get("details") or {}).get("family"),
                }
                for m in response.json().get("models", [])
            ]

    def compatibility(self, model, variant):
        if model.gated:
            return {
                "supported": False,
                "reason": "This repository requires access approval. Use a public, ungated repository.",
            }
        if model.catalog == "ollama" and variant.format == "ollama":
            return {
                "supported": True,
                "reason": "Pulled directly by Ollama; the runtime checks model/version compatibility.",
            }
        if variant.format == "mlx" and self.local_mlx:
            return {
                "supported": True,
                "reason": "Native Apple Silicon import through the installed Ollama CLI. Experimental; architecture support is checked at import. Original quantization is preserved.",
            }
        if variant.format == "gguf":
            return {
                "supported": True,
                "reason": "GGUF import; architecture support is checked by your Ollama runtime during import.",
            }
        if variant.format == "safetensors" and not variant.metadata.get("quantized"):
            names = {item.path for item in variant.files}
            if not {"config.json", "tokenizer_config.json"}.issubset(names):
                return {
                    "supported": False,
                    "reason": "Safetensors import requires config.json and tokenizer_config.json.",
                }
            return {
                "supported": True,
                "reason": "Unquantized Safetensors import; architecture support depends on your Ollama version. No automatic quantization is applied.",
            }
        return {
            "supported": False,
            "reason": "This Ollama adapter cannot import these prequantized/MLX weights. Choose a GGUF variant or use a compatible runtime. The original quantization will not be changed.",
        }

    async def _stream(self, path, payload):
        success = False
        async with httpx.AsyncClient(timeout=httpx.Timeout(None, connect=15)) as client:
            async with client.stream(
                "POST", self.base_url + path, json={**payload, "stream": True}
            ) as response:
                if response.status_code >= 400:
                    raise ModelManagerError(
                        "Ollama rejected the operation. Check your runtime version and model compatibility."
                    )
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    event = json.loads(line)
                    if event.get("error"):
                        raise ModelManagerError(
                            "Ollama could not install this model. Check runtime logs for architecture, storage, or version requirements."
                        )
                    success = event.get("status") == "success"
                    yield {
                        "stage": "installing",
                        "message": str(event.get("status", "Installing model"))[:180],
                        "completed": event.get("completed", 0),
                        "total": event.get("total", 0),
                    }
        if not success:
            raise ModelManagerError(
                "Ollama ended the installation before confirming success. Retry the operation."
            )

    def import_configuration(self, variant, files):
        if not files:
            raise ModelManagerError("No model files were selected for import.")
        if variant.format == "gguf":
            first_name = next(iter(files))
            source = Path(files[first_name])
            split = SPLIT_GGUF.fullmatch(source.name)
            if split:
                source = source.parent / (split[1] + "-*-of-" + split[3] + ".gguf")
        else:
            if "config.json" not in files:
                raise ModelManagerError("This model bundle is missing config.json.")
            source = Path(files["config.json"]).parent
        return {
            "kind": "modelfile",
            "filename": "Modelfile",
            "content": "FROM " + json.dumps(str(source), ensure_ascii=False) + "\n",
        }

    async def install(self, model, variant, name, files):
        model_name(name)
        modelfile = None
        if model.catalog == "local":
            configuration = self.import_configuration(variant, files)
            modelfile = cache_root(model) / "Modelfile"
            await asyncio.to_thread(
                modelfile.write_text, configuration["content"], encoding="utf-8"
            )
            yield {
                "stage": "preparing",
                "message": "Generated import configuration",
                "configuration": configuration,
            }
        if variant.format == "ollama":
            source_name = model_name(model.id + ":" + variant.id)
            async for event in self._stream("/api/pull", {"model": source_name}):
                yield event
            if source_name != name:
                async with httpx.AsyncClient(timeout=30) as client:
                    response = await client.post(
                        self.base_url + "/api/copy",
                        json={"source": source_name, "destination": name},
                    )
                    response.raise_for_status()
        elif variant.format == "mlx":
            if not self.local_mlx:
                raise ModelManagerError(
                    "Local MLX import is unavailable in this runtime."
                )
            async for event in ollama_local.install(
                self.base_url, model, name, modelfile=modelfile
            ):
                yield event
        else:
            blobs = {}
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(None, connect=15)
            ) as client:
                for filename, value in files.items():
                    path = Path(value)
                    digest = "sha256:" + await asyncio.to_thread(checksum, path)
                    exists = await client.head(self.base_url + "/api/blobs/" + digest)
                    if exists.status_code != 200:

                        async def content(source=path):
                            with source.open("rb") as stream:
                                while chunk := await asyncio.to_thread(
                                    stream.read, 1024 * 1024
                                ):
                                    yield chunk

                        yield {
                            "stage": "importing",
                            "message": f"Importing {filename}",
                            "completed": 0,
                            "total": path.stat().st_size,
                        }
                        response = await client.post(
                            self.base_url + "/api/blobs/" + digest,
                            content=content(),
                            headers={
                                "Content-Length": str(path.stat().st_size),
                                "Content-Type": "application/octet-stream",
                            },
                        )
                        response.raise_for_status()
                    blobs[filename] = digest
            async for event in self._stream(
                "/api/create", {"model": name, "files": blobs}
            ):
                yield {**event, "stage": "importing"}
        # Verify registration/metadata, without loading gigabytes into RAM or
        # pretending that registration proves thinking/tool calling works.
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(
                self.base_url + "/api/show", json={"model": name}
            )
            response.raise_for_status()
            info = response.json()
        yield {
            "stage": "verifying",
            "message": "Model registered; inference has not been tested",
            "capabilities": info.get("capabilities", []),
        }

    async def remove(self, name):
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.request(
                "DELETE",
                self.base_url + "/api/delete",
                json={"model": model_name(name)},
            )
            response.raise_for_status()
