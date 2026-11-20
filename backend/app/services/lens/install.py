"""Install a profile's Lens models through the shared model manager adapters."""

import argparse
import asyncio
import logging
import re
import httpx
from contextlib import aclosing

from app.config import VALID_PROFILES
from app.services.model_manager.lens_models import (
    LensCatalog,
    LensRuntime,
    model_paths,
    recommended_lens_models,
)
from app.services.model_manager.types import ModelManagerError

logger = logging.getLogger(__name__)


async def stream_install_lens(entries, *, index_offset, total_models):
    catalog = LensCatalog()
    runtime = LensRuntime()
    for index, entry in enumerate(entries, start=index_offset):
        request = entry["install"]
        event = {
            "model": entry["id"],
            "module": "lens",
            "kind": "lens_model",
            "provider": "lens-onnx",
            "index": index,
            "total_models": total_models,
        }
        yield {**event, "event": "pull_start"}
        try:
            ident, precision = request["name"].split(":")
            installed = bool(await asyncio.to_thread(model_paths, ident, precision))
            if not installed:
                model = await catalog.inspect(request["model"], request["revision"])
                variant = next(v for v in model.variants if v.id == precision)
                files = {}
                async with aclosing(catalog.download(model, variant)) as download:
                    async for update in download:
                        if "files" in update:
                            files = update["files"]
                        else:
                            completed, total = update["completed"], update["total"]
                            yield {
                                **event,
                                "event": "pull_progress",
                                "status": update["message"],
                                "completed": completed,
                                "total": total,
                                "percent": min(100, int(completed * 100 / total)),
                            }
                async for update in runtime.install(model, variant, request["name"], files):
                    yield {**event, "event": "pull_status", "status": update["message"]}
        except ModelManagerError as exc:
            yield {**event, "event": "pull_error", "error": exc.message}
        except Exception as exc:
            logger.exception("Lens setup failed for %s", request["name"])
            yield {
                **event,
                "event": "pull_error",
                "error_details": re.sub(
                    r"hf_[A-Za-z0-9]+",
                    "[redacted]",
                    re.sub(r"https?://\S+", "[download URL]", f"{type(exc).__name__}: {exc}"),
                )[:2000],
                "error": (
                    "The download timed out. Retry to resume the saved download."
                    if isinstance(exc, httpx.TimeoutException)
                    else "The connection was interrupted. Retry to resume the saved download."
                    if isinstance(exc, httpx.TransportError)
                    else f"The download server returned HTTP {exc.response.status_code}. Please retry."
                    if isinstance(exc, httpx.HTTPStatusError)
                    else "There was a problem saving the model. Check free disk space and retry."
                    if isinstance(exc, OSError)
                    else f"Installation could not finish ({type(exc).__name__}). Retry the download."
                ),
            }
        else:
            yield {**event, "event": "pull_done", "already_installed": installed}


def lens_setup_entries(profile):
    return [
        {
            "id": f"{model['title']} · {model['variant'].upper()}",
            "description": model["title"],
            "role": model["name"].split(":")[0],
            "size": model["size"],
            "module": "lens",
            "kind": "lens_model",
            "provider": "lens-onnx",
            "install": model,
        }
        for model in recommended_lens_models(profile)
    ]


async def _run_cli(profile):
    entries = lens_setup_entries(profile)
    failed = False
    last_progress = 0
    async for event in stream_install_lens(entries, index_offset=0, total_models=len(entries)):
        kind = event["event"]
        if kind == "pull_start":
            print(f"Installing {event['model']}", flush=True)
            last_progress = 0
        elif kind == "pull_progress" and event["percent"] >= last_progress + 10:
            last_progress = event["percent"]
            print(f"  {last_progress}% · {event['status']}", flush=True)
        elif kind == "pull_done":
            print(
                "  Already installed" if event["already_installed"] else "  Installed",
                flush=True,
            )
        elif kind == "pull_error":
            print(f"  {event['error']}", flush=True)
            failed = True
    return int(failed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Install local Lens search models")
    parser.add_argument("--profile", required=True, choices=VALID_PROFILES)
    raise SystemExit(asyncio.run(_run_cli(parser.parse_args().profile)))
