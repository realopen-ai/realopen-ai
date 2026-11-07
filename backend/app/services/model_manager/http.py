"""Bounded public metadata requests; errors never include response bodies or credentials."""

import json

import httpx

from .types import ModelManagerError


async def metadata(url: str, *, params: dict | None = None, as_json=True):
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True) as client:
            async with client.stream("GET", url, params=params) as response:
                if response.status_code in {401, 403}:
                    raise ModelManagerError(
                        "This repository is gated or private. Public, ungated models are supported; check access and license on the source site."
                    )
                if response.status_code == 404:
                    raise ModelManagerError(
                        "Model or variant not found in this catalog."
                    )
                if response.status_code == 429:
                    raise ModelManagerError(
                        "The catalog is rate limited. Please try again later."
                    )
                response.raise_for_status()
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk)
                    if len(chunks) > 8 * 1024 * 1024:
                        raise ModelManagerError(
                            "Catalog metadata is too large. Try a more specific model."
                        )
                if as_json:
                    return json.loads(chunks)
                return chunks.decode("utf-8")
    except (httpx.HTTPError, ValueError) as exc:
        raise ModelManagerError(
            "Cannot reach the model catalog or read its metadata. Check connectivity and try again."
        ) from exc
