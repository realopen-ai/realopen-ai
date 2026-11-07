"""Streaming, resumable, revision-pinned downloads into the shared model cache."""

import asyncio
import hashlib
import re
import shutil
from urllib.parse import quote, urlsplit

import httpx

from app.config import settings
from app.services.safe_paths import confined_path
from .types import ModelManagerError


def cache_root(model):
    # IDs and revisions have already been validated by the source adapter.
    key = hashlib.sha256(
        f"{model.catalog}:{model.id}:{model.revision}".encode()
    ).hexdigest()
    root = settings.get_data_dir() / "models" / "catalog" / key
    root.mkdir(parents=True, exist_ok=True)
    return root


def checksum(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _download_host(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in {None, 443}
        or not any(
            host == suffix or host.endswith("." + suffix)
            for suffix in ("huggingface.co", "hf.co", "xethub.hf.co")
        )
    ):
        raise ModelManagerError("The model download redirected to an unsupported host.")


async def _response(client, url, offset):
    for _ in range(8):
        _download_host(url)
        request = client.build_request(
            "GET", url, headers={"Range": f"bytes={offset}-"} if offset else {}
        )
        response = await client.send(request, stream=True)
        if response.is_redirect:
            destination = response.headers.get("location", "")
            await response.aclose()
            url = str(httpx.URL(url).join(destination))
            continue
        if response.status_code in {401, 403}:
            await response.aclose()
            raise ModelManagerError(
                "Download requires access approval or authentication on Hugging Face."
            )
        try:
            response.raise_for_status()
        except httpx.HTTPError:
            await response.aclose()
            raise
        return response
    raise ModelManagerError("Too many redirects while downloading the model.")


async def download_files(model, variant):
    root = cache_root(model)
    required = variant.size or sum(f.size for f in variant.files)
    # Keep room for Ollama's blob import as well as the source cache. Ollama may
    # use another disk; its adapter must still report import-side disk failures.
    if shutil.disk_usage(root).free < required * 2 + 256 * 1024 * 1024:
        raise ModelManagerError(
            "Not enough free space for the model cache and runtime import. Free disk space and retry."
        )
    completed = 0
    paths = {}
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60, connect=15), follow_redirects=False
    ) as client:
        for item in variant.files:
            path = confined_path(root, item.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            valid = path.is_file() and path.stat().st_size == item.size
            if valid and item.sha256:
                valid = await asyncio.to_thread(checksum, path) == item.sha256
            if valid:
                paths[item.path] = str(path)
                completed += item.size
                yield {
                    "stage": "downloading",
                    "message": f"Using cached {item.path}",
                    "completed": completed,
                    "total": required,
                }
                continue
            partial = confined_path(root, item.path + ".part")
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > item.size:
                partial.unlink()
                offset = 0
            if offset != item.size or not partial.exists():
                url = f"https://huggingface.co/{quote(model.id, safe='/')}/resolve/{model.revision}/{quote(item.path, safe='/')}"
                response = await _response(client, url, offset)
                try:
                    if response.status_code == 206:
                        content_range = response.headers.get("content-range", "")
                        match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", content_range)
                        if (
                            not match
                            or int(match[1]) != offset
                            or int(match[3]) != item.size
                        ):
                            raise ModelManagerError(
                                "Invalid resume response. Retry the model download."
                            )
                    elif offset:
                        offset = 0  # Server ignored Range: restart, never append a full file.
                    with partial.open("ab" if offset else "wb") as stream:
                        async for chunk in response.aiter_bytes(1024 * 1024):
                            offset += len(chunk)
                            if offset > item.size:
                                raise ModelManagerError(
                                    "The downloaded file exceeds its catalog size."
                                )
                            await asyncio.to_thread(stream.write, chunk)
                            yield {
                                "stage": "downloading",
                                "message": f"Downloading {item.path}",
                                "completed": completed + offset,
                                "total": required,
                            }
                finally:
                    await response.aclose()
            if partial.stat().st_size != item.size:
                raise ModelManagerError(
                    "The downloaded file is incomplete. Retry to resume it."
                )
            if (
                item.sha256
                and await asyncio.to_thread(checksum, partial) != item.sha256
            ):
                partial.unlink()
                raise ModelManagerError(
                    "Model checksum verification failed. Retry to download a fresh copy."
                )
            partial.replace(path)
            completed += item.size
            paths[item.path] = str(path)
            yield {
                "stage": "downloading",
                "message": f"Downloaded {item.path}",
                "completed": completed,
                "total": required,
            }
    yield {"files": paths}
