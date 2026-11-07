"""Host-owned jobs survive backend reloads; interrupted jobs can be retried safely."""

import asyncio
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import httpx

from app.config import settings
from .registry import CATALOGS, runtime_adapters
from .types import InstallRequest, ModelManagerError

logger = logging.getLogger(__name__)
ACTIVE = {"queued", "running"}


def now():
    return datetime.now(timezone.utc).isoformat()


def write_record(path, record):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, encoding="utf-8", delete=False
    ) as stream:
        temporary = Path(stream.name)
        try:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class ModelJobs:
    def __init__(self, directory=None, runtimes=None, catalogs=None):
        self.directory = directory or settings.get_data_dir() / "model-manager" / "jobs"
        self.runtimes = runtimes if runtimes is not None else runtime_adapters()
        self.catalogs = catalogs if catalogs is not None else CATALOGS
        self.records = {}
        self.tasks = {}
        self.lock = asyncio.Lock()
        self.worker = asyncio.Semaphore(1)
        self.closing = False

    async def start(self):
        def load():
            self.directory.mkdir(parents=True, exist_ok=True)
            rows = []
            for path in self.directory.glob("*.json"):
                try:
                    record = json.loads(path.read_text(encoding="utf-8"))
                    if (
                        not isinstance(record, dict)
                        or record.get("schema_version") != 1
                    ):
                        raise ValueError("Unknown model job schema")
                    if str(UUID(record["id"])) != path.stem:
                        raise ValueError("Invalid model job ID")
                    InstallRequest.model_validate(record["request"])
                    if record.get("status") not in ACTIVE | {
                        "completed",
                        "failed",
                        "cancelled",
                        "interrupted",
                    } or not isinstance(record.get("created_at"), str):
                        raise ValueError("Invalid model job state")
                    rows.append(record)
                except (OSError, ValueError, KeyError, TypeError):
                    logger.warning("Skipping unreadable model installation record")
            return rows

        for record in await asyncio.to_thread(load):
            if record["status"] in ACTIVE:
                record.update(
                    status="interrupted",
                    message="Host runtime restarted. Retry to resume supported downloads.",
                    updated_at=now(),
                )
                await self.save(record)
            self.records[record["id"]] = record

    def runtime(self, ident):
        if ident not in self.runtimes:
            raise ModelManagerError("Unknown model runtime.")
        return self.runtimes[ident]

    def catalog(self, ident):
        if ident not in self.catalogs:
            raise ModelManagerError("Unknown model catalog.")
        return self.catalogs[ident]

    async def save(self, record):
        task = asyncio.create_task(
            asyncio.to_thread(
                write_record, self.directory / (record["id"] + ".json"), record.copy()
            )
        )
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            # Do not let an older background write replace a later terminal
            # record after cancellation/shutdown.
            await task
            raise

    def snapshot(self):
        return sorted(
            (r.copy() for r in self.records.values()),
            key=lambda r: r["created_at"],
            reverse=True,
        )[:100]

    async def inventory(self):
        states, models = [], []
        for adapter in self.runtimes.values():
            state = await adapter.status()
            states.append(state)
            if state["available"]:
                try:
                    models.extend(await adapter.installed())
                except (httpx.HTTPError, ValueError):
                    state["available"] = False
        return {
            "runtimes": states,
            "models": models,
            "local_import": next(
                (
                    c.local_import_options()
                    for c in self.catalogs.values()
                    if hasattr(c, "local_import_options")
                ),
                None,
            ),
            "catalogs": [
                {"id": c.id, "label": c.label}
                for c in self.catalogs.values()
                if getattr(c, "searchable", True)
            ],
        }

    async def inspect(self, catalog, model, revision=None):
        for runtime in self.runtimes.values():
            await runtime.status()
        detail = await self.catalog(catalog).inspect(model, revision)
        result = detail.model_dump()
        for variant in result["variants"]:
            original = next(v for v in detail.variants if v.id == variant["id"])
            variant["compatibility"] = {
                r.id: r.compatibility(detail, original) for r in self.runtimes.values()
            }
            # Local-source configuration previews are optional runtime features,
            # not an Ollama-specific requirement of the catalog/job protocol.
            if hasattr(self.catalog(catalog), "file_paths"):
                files = await asyncio.to_thread(
                    self.catalog(catalog).file_paths, detail, original
                )
                variant["configurations"] = {
                    r.id: r.import_configuration(original, files)
                    for r in self.runtimes.values()
                    if hasattr(r, "import_configuration")
                }
        return result

    async def create(self, request: InstallRequest):
        if self.closing:
            raise ModelManagerError(
                "The model manager is shutting down. Try again shortly."
            )
        adapter = self.runtime(request.runtime)
        request = request.model_copy(
            update={"name": adapter.normalize_name(request.name)}
        )
        source = self.catalog(request.catalog)
        detail = await source.inspect(request.model, request.revision)
        variant = next((v for v in detail.variants if v.id == request.variant), None)
        if variant is None:
            raise ModelManagerError(
                "The selected variant is no longer available. Inspect the model again."
            )
        status = await adapter.status()
        compatible = adapter.compatibility(detail, variant)
        if not compatible["supported"]:
            raise ModelManagerError(compatible["reason"])
        if not status["available"]:
            raise ModelManagerError(
                "The selected runtime is offline. Start it before installing a model."
            )
        async with self.lock:
            if any(
                r["status"] in ACTIVE
                and r["request"]["runtime"] == request.runtime
                and (
                    r["request"]["name"] == request.name
                    or (
                        r["request"]["catalog"],
                        r["request"]["model"],
                        r["request"]["variant"],
                    )
                    == (detail.catalog, detail.id, variant.id)
                )
                for r in self.records.values()
            ):
                raise ModelManagerError(
                    "This model already has an active installation job."
                )
            if any(
                m["name"] == request.name or m["name"] == request.name + ":latest"
                for m in await adapter.installed()
            ):
                raise ModelManagerError(
                    "That model name is already installed. Choose another name or remove it first."
                )
            request = request.model_copy(
                update={"model": detail.id, "revision": detail.revision}
            )
            record = {
                "schema_version": 1,
                "id": str(uuid4()),
                "request": request.model_dump(),
                "status": "queued",
                "stage": "queued",
                "message": "Waiting for the current download",
                "completed": 0,
                "total": variant.size or 0,
                "created_at": now(),
                "updated_at": now(),
                "source_url": detail.url,
                "source_title": detail.title,
                "format": variant.format,
                "quantization": variant.quantization,
                "files": [f.model_dump() for f in variant.files],
            }
            await self.save(record)
            self.records[record["id"]] = record
            self.tasks[record["id"]] = asyncio.create_task(
                self.run(record, source, detail, variant, adapter)
            )
            return record.copy()

    async def run(self, record, source, detail, variant, adapter):
        last_save = 0.0
        try:
            async with self.worker:
                record.update(
                    status="running",
                    stage="preparing",
                    updated_at=now(),
                    message="Preparing installation",
                )
                await self.save(record)
                # Check again after queueing: another application may have installed it.
                if any(
                    m["name"] == record["request"]["name"]
                    for m in await adapter.installed()
                ):
                    raise ModelManagerError(
                        "The target model name is now installed. It will not be overwritten."
                    )
                files = {}
                async for event in source.download(detail, variant):
                    if "files" in event:
                        files = event["files"]
                        continue
                    record.update(event, updated_at=now())
                    if time.monotonic() - last_save > 0.5:
                        await self.save(record)
                        last_save = time.monotonic()
                async for event in adapter.install(
                    detail, variant, record["request"]["name"], files
                ):
                    record.update(event, updated_at=now())
                    if time.monotonic() - last_save > 0.5:
                        await self.save(record)
                        last_save = time.monotonic()
                record.update(
                    status="completed",
                    stage="completed",
                    message="Installed and registered. Select it in Settings → AI; inference has not been tested.",
                    updated_at=now(),
                )
        except asyncio.CancelledError:
            interrupted = self.closing
            record.update(
                status="interrupted" if interrupted else "cancelled",
                stage="interrupted" if interrupted else "cancelled",
                message=(
                    "Host stopped. Retry to resume."
                    if interrupted
                    else "Cancelled. Downloaded data may be reused when retried."
                ),
                updated_at=now(),
            )
        except ModelManagerError as exc:
            record.update(
                status="failed", stage="failed", message=exc.message, updated_at=now()
            )
        except Exception:
            logger.exception("Model installation failed for job %s", record["id"])
            record.update(
                status="failed",
                stage="failed",
                message="Model installation failed. Check host runtime logs and retry.",
                updated_at=now(),
            )
        finally:
            await self.save(record)
            self.tasks.pop(record["id"], None)

    async def cancel(self, ident):
        async with self.lock:
            record = self.records.get(ident)
            if record is None:
                raise ModelManagerError("Model installation job not found.")
            task = self.tasks.get(ident)
            if task:
                task.cancel()
                # A queued task may not have entered run() yet.
                record.update(
                    status="cancelled",
                    stage="cancelled",
                    message="Cancelled. Downloaded data may be reused when retried.",
                    updated_at=now(),
                )
                await self.save(record)
        if task:
            await asyncio.gather(task, return_exceptions=True)
            self.tasks.pop(ident, None)
        return record.copy()

    async def retry(self, ident):
        record = self.records.get(ident)
        if (
            record is None
            or record["status"] in ACTIVE
            or record["status"] == "completed"
        ):
            raise ModelManagerError(
                "Only failed, cancelled, or interrupted jobs can be retried."
            )
        return await self.create(InstallRequest.model_validate(record["request"]))

    async def remove(self, runtime, name):
        adapter = self.runtime(runtime)
        name = adapter.normalize_name(name)
        async with self.lock:
            if any(
                r["status"] in ACTIVE
                and r["request"]["runtime"] == runtime
                and r["request"]["name"] == name
                for r in self.records.values()
            ):
                raise ModelManagerError(
                    "Cancel the active installation before removing this model."
                )
            await adapter.remove(name)

    async def close(self):
        self.closing = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
