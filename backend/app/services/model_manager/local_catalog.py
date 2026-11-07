"""Local model sources: read-only host references and bounded browser uploads.

Only model weights and known companion files are inspected. No repository
scripts or supplied Modelfiles are executed; originals are never changed.
"""

import asyncio
import hashlib
import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.config import settings
from app.services.safe_paths import bounded_text, confined_path
from .bundles import AUXILIARY_FILES, SPLIT_GGUF, discover_variants, supported_file
from .types import CatalogModel, ModelFile, ModelManagerError
from .validation import file_name

CHUNK_BYTES = 4 * 1024 * 1024
FORMAT_HINT = Literal["auto", "safetensors", "mlx"]


class LocalPathRequest(BaseModel):
    path: str = Field(min_length=1, max_length=4096)
    format_hint: FORMAT_HINT = "auto"


class UploadFileSpec(BaseModel):
    path: str = Field(min_length=1, max_length=500)
    size: int = Field(ge=0)


class LocalUploadRequest(BaseModel):
    title: str = Field(default="Local model", min_length=1, max_length=200)
    files: list[UploadFileSpec] = Field(min_length=1, max_length=4096)
    format_hint: FORMAT_HINT = "auto"


def _ident(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError):
        raise ModelManagerError("Invalid local import ID.") from None


def _json_file(root, name, limit=4 * 1024 * 1024):
    path = confined_path(root, name)
    if not path.exists():
        return {}
    try:
        result = json.loads(bounded_text(path, limit))
        if not isinstance(result, dict):
            raise ValueError("Expected an object")
        return result
    except (OSError, ValueError):
        raise ModelManagerError(
            "A model configuration file is invalid or too large."
        ) from None


def _weight_header(path):
    with path.open("rb") as stream:
        if path.name.lower().endswith(".gguf"):
            magic = stream.read(4)
            version = int.from_bytes(
                stream.read(4), "big" if magic == b"FUGG" else "little"
            )
            if magic not in {b"GGUF", b"FUGG"} or version not in {2, 3}:
                raise ModelManagerError(
                    "A selected GGUF file has an invalid header or unsupported version."
                )
        else:
            length = int.from_bytes(stream.read(8), "little")
            if not 2 <= length <= min(16 * 1024 * 1024, path.stat().st_size - 8):
                raise ModelManagerError(
                    "A selected Safetensors file has an invalid header."
                )
            try:
                if not isinstance(json.loads(stream.read(length)), dict):
                    raise ValueError("Expected tensor metadata")
            except ValueError:
                raise ModelManagerError(
                    "A selected Safetensors file has invalid metadata."
                ) from None


def _metadata(root, names):
    files, receipts = [], []
    for name in names:
        path = confined_path(root, file_name(name))
        if not path.is_file():
            raise ModelManagerError(
                "A selected model file is missing or is not a regular file."
            )
        stat = path.stat()
        if name.lower().endswith((".gguf", ".safetensors")):
            _weight_header(path)
        receipts.append(
            {
                "path": name,
                "size": stat.st_size,
                "mtime_ns": stat.st_mtime_ns,
                "inode": stat.st_ino,
            }
        )
        files.append(ModelFile(path=name, size=stat.st_size))
    config = _json_file(root, "config.json") if "config.json" in names else {}
    # An index must only reference selected files, never an absolute path or a
    # missing shard. Ollama must not follow index references outside the bundle.
    if "model.safetensors.index.json" in names:
        index = _json_file(root, "model.safetensors.index.json")
        weight_map = index.get("weight_map")
        if not isinstance(weight_map, dict) or not weight_map:
            raise ModelManagerError("The Safetensors shard index is invalid.")
        for referenced in weight_map.values():
            if (
                not isinstance(referenced, str)
                or file_name(referenced) not in names
                or not referenced.endswith(".safetensors")
            ):
                raise ModelManagerError(
                    "The Safetensors index references a missing or unsafe shard."
                )
    return files, receipts, config


def _write_json(path, value):
    # Only one writer per source (guarded by its async lock for uploads).
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


class LocalCatalog:
    id = "local"
    label = "Local files"
    searchable = False

    def __init__(self):
        self.locks = {}

    def local_import_options(self):
        return {
            "extensions": [".gguf", ".safetensors"],
            "companion_files": sorted(AUXILIARY_FILES),
            "chunk_size": CHUNK_BYTES,
            "max_bytes": settings.MODEL_IMPORT_MAX_BYTES,
        }

    def directory(self, ident):
        return confined_path(
            settings.get_data_dir() / "model-manager" / "imports", _ident(ident)
        )

    def _receipt(self, ident):
        path = self.directory(ident) / "source.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("id") != _ident(ident) or value.get("schema_version") != 1:
                raise ValueError("Invalid receipt")
            return value
        except (OSError, ValueError, AttributeError):
            raise ModelManagerError(
                "Local import not found. Select the files or host path again."
            ) from None

    async def search(self, query):
        raise ModelManagerError("Use Import local model to select local files.")

    async def prepare_path(self, path, format_hint="auto"):
        def prepare():
            selected = Path(path).expanduser()
            if not selected.is_absolute() or selected.is_symlink():
                raise ModelManagerError(
                    "Choose an absolute host file/folder path, not a symbolic link."
                )
            selected = selected.resolve()
            if selected.is_file():
                if not selected.name.lower().endswith(".gguf"):
                    raise ModelManagerError(
                        "Choose a GGUF file, or the folder containing Safetensors/MLX weights and tokenizer files."
                    )
                root = selected.parent
                split = SPLIT_GGUF.fullmatch(selected.name)
                names = sorted(
                    p.name
                    for p in root.iterdir()
                    if (
                        split
                        and (match := SPLIT_GGUF.fullmatch(p.name))
                        and match[1] == split[1]
                    )
                    or p.name == selected.name
                )
            elif selected.is_dir():
                root = selected
                names = []
                for item in root.iterdir():
                    if supported_file(item.name):
                        names.append(item.name)
                    if len(names) > 4096:
                        raise ModelManagerError(
                            "Too many model files. Choose a single model folder."
                        )
            else:
                raise ModelManagerError(
                    "That host path does not exist or cannot be read."
                )
            return self._create_ready(
                root, names, selected.stem, format_hint, mode="path"
            )

        try:
            return await asyncio.to_thread(prepare)
        except (OSError, ValueError):
            raise ModelManagerError(
                "Cannot read that model path. Check its location and permissions on the host."
            ) from None

    def _create_ready(self, root, names, title, format_hint, *, mode, ident=None):
        if not names:
            raise ModelManagerError(
                "No supported model files found. Choose a GGUF file or a model folder."
            )
        files, receipts, config = _metadata(root, names)
        variants = discover_variants(files, config, format_hint=format_hint)
        if not variants:
            raise ModelManagerError(
                "No complete model variant found. Include all model shards and companion files."
            )
        ident = ident or str(uuid4())
        directory = self.directory(ident)
        directory.mkdir(parents=True, exist_ok=True)
        receipt = {
            "schema_version": 1,
            "id": ident,
            "mode": mode,
            "state": "ready",
            "root": str(root),
            "title": title[:200],
            "format_hint": format_hint,
            "files": receipts,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        receipt["revision"] = hashlib.sha256(
            json.dumps(receipts, sort_keys=True).encode()
        ).hexdigest()
        _write_json(directory / "source.json", receipt)
        return ident

    async def begin_upload(self, request: LocalUploadRequest):
        names = [file_name(item.path) for item in request.files]
        if len({name.casefold() for name in names}) != len(names) or any(
            not supported_file(name) for name in names
        ):
            raise ModelManagerError(
                "Choose model weights and supported companion files with unique names."
            )
        weights = [
            item
            for item in request.files
            if item.path.lower().endswith((".gguf", ".safetensors"))
        ]
        if not weights or any(item.size == 0 for item in weights):
            raise ModelManagerError("Select at least one nonempty model weight file.")
        total = sum(item.size for item in request.files)
        if total > settings.MODEL_IMPORT_MAX_BYTES:
            raise ModelManagerError(
                "This model exceeds the configured local import size limit."
            )
        root = settings.get_data_dir() / "model-manager" / "imports"
        root.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(root).free < total + 256 * 1024 * 1024:
            raise ModelManagerError(
                "Not enough free disk space to upload these model files."
            )
        ident = str(uuid4())
        directory = self.directory(ident)
        (directory / "files").mkdir(parents=True)
        for item in request.files:
            if item.size == 0:
                target = confined_path(directory / "files", item.path)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.touch()
        receipt = {
            "schema_version": 1,
            "id": ident,
            "mode": "upload",
            "state": "uploading",
            "root": str(directory / "files"),
            "title": request.title,
            "format_hint": request.format_hint,
            "files": [item.model_dump() for item in request.files],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        await asyncio.to_thread(_write_json, directory / "source.json", receipt)
        return {"id": ident, "chunk_size": CHUNK_BYTES}

    async def upload_chunk(self, ident, name, offset, chunks):
        ident = _ident(ident)
        async with self.locks.setdefault(ident, asyncio.Lock()):
            receipt = await asyncio.to_thread(self._receipt, ident)
            if receipt["mode"] != "upload" or receipt["state"] != "uploading":
                raise ModelManagerError("This upload is already closed.")
            expected = next((f for f in receipt["files"] if f["path"] == name), None)
            if expected is None:
                raise ModelManagerError(
                    "This file is not part of the selected model upload."
                )
            path = confined_path(Path(receipt["root"]), file_name(name))
            path.parent.mkdir(parents=True, exist_ok=True)
            if offset < 0 or (path.stat().st_size if path.exists() else 0) != offset:
                raise ModelManagerError(
                    "Upload position changed. Retry the file selection."
                )
            received = 0
            with path.open("ab") as target:
                try:
                    async for chunk in chunks:
                        received += len(chunk)
                        if (
                            received > CHUNK_BYTES
                            or offset + received > expected["size"]
                        ):
                            raise ModelManagerError(
                                "Upload chunk exceeds the declared file size or chunk limit."
                            )
                        if (
                            shutil.disk_usage(path.parent).free
                            < len(chunk) + 16 * 1024 * 1024
                        ):
                            raise ModelManagerError(
                                "Not enough disk space to continue the upload."
                            )
                        target.write(chunk)
                except BaseException:
                    target.truncate(offset)
                    raise
            receipt["updated_at"] = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(
                _write_json, self.directory(ident) / "source.json", receipt
            )
            return {"offset": offset + received}

    async def finish_upload(self, ident):
        ident = _ident(ident)
        async with self.locks.setdefault(ident, asyncio.Lock()):
            receipt = await asyncio.to_thread(self._receipt, ident)
            if receipt["mode"] != "upload" or receipt["state"] != "uploading":
                raise ModelManagerError("This upload is already closed.")
            root = Path(receipt["root"])
            for item in receipt["files"]:
                path = confined_path(root, item["path"])
                if not path.exists() or path.stat().st_size != item["size"]:
                    raise ModelManagerError(
                        "The model upload is incomplete. Include every selected file."
                    )
            await asyncio.to_thread(
                self._create_ready,
                root,
                [f["path"] for f in receipt["files"]],
                receipt["title"],
                receipt["format_hint"],
                mode="upload",
                ident=ident,
            )
        self.locks.pop(ident, None)
        return ident

    async def discard(self, ident):
        ident = _ident(ident)
        async with self.locks.setdefault(ident, asyncio.Lock()):
            receipt = await asyncio.to_thread(self._receipt, ident)
            if receipt["mode"] != "upload":
                raise ModelManagerError(
                    "Host source files cannot be deleted by the model manager."
                )
            await asyncio.to_thread(shutil.rmtree, self.directory(ident))
        self.locks.pop(ident, None)

    async def inspect(self, model, revision=None):
        receipt = await asyncio.to_thread(self._receipt, model)
        if receipt["state"] != "ready":
            raise ModelManagerError(
                "Finish uploading the model files before inspecting or importing them."
            )

        def inspect():
            root = Path(receipt["root"])
            files, stats, config = _metadata(
                root, [f["path"] for f in receipt["files"]]
            )
            if stats != receipt["files"] or (
                revision and revision != receipt["revision"]
            ):
                raise ModelManagerError(
                    "The local model files changed. Inspect the source again before importing."
                )
            return CatalogModel(
                catalog=self.id,
                id=model,
                title=receipt["title"],
                url="",
                revision=receipt["revision"],
                description="Local model files",
                variants=discover_variants(
                    files, config, format_hint=receipt["format_hint"]
                ),
            )

        try:
            return await asyncio.to_thread(inspect)
        except (OSError, ValueError):
            raise ModelManagerError(
                "Cannot read the local model files. Check their location and permissions on the host."
            ) from None

    def file_paths(self, model, variant):
        receipt = self._receipt(model.id)
        return {
            f.path: str(confined_path(Path(receipt["root"]), f.path))
            for f in variant.files
        }

    async def download(self, model, variant):
        # Revalidate after queueing: originals may have been edited or moved.
        current = await self.inspect(model.id, model.revision)
        selected = next((v for v in current.variants if v.id == variant.id), None)
        if selected is None:
            raise ModelManagerError(
                "The selected local model variant is no longer available."
            )
        yield {
            "stage": "preparing",
            "message": "Using local files; no download required",
            "completed": selected.size or 0,
            "total": selected.size or 0,
        }
        yield {"files": await asyncio.to_thread(self.file_paths, model, selected)}
