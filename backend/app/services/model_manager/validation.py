"""Accept model identifiers and known catalog URLs, never arbitrary fetch URLs."""

import re
from pathlib import PurePosixPath
from urllib.parse import unquote, urlsplit

from .types import ModelManagerError

_NAME = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_.-]*(?:/[A-Za-z0-9][A-Za-z0-9_.-]*)?(?::[A-Za-z0-9][A-Za-z0-9_.-]*)?"
)
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*")


def model_name(value: str) -> str:
    if (
        len(value) > 200
        or not _NAME.fullmatch(value)
        or any(p in {".", ".."} for p in value.split("/"))
    ):
        raise ModelManagerError("Use a model name such as qwen3:8b or my-model:q4.")
    return value


def repository(value: str) -> str:
    if (
        len(value) > 240
        or not _REPO.fullmatch(value)
        or any(p in {".", ".."} for p in value.split("/"))
    ):
        raise ModelManagerError(
            "Use a Hugging Face repository such as publisher/model."
        )
    return value


def revision_id(value: str | None) -> str | None:
    if value is not None and not re.fullmatch(r"[a-fA-F0-9]{40,64}", value):
        raise ModelManagerError("The model revision must be an immutable commit ID.")
    return value


def file_name(value: str) -> str:
    path = PurePosixPath(value)
    if (
        not value
        or path.is_absolute()
        or ".." in path.parts
        or "\\" in value
        or ":" in value
        or any(ord(c) < 32 for c in value)
    ):
        raise ModelManagerError("The repository contains an unsafe file path.")
    return value


def resolve_reference(
    value: str, catalog: str
) -> tuple[str, str, str | None, str | None]:
    """Return catalog, ID, optional exact variant, optional pinned revision."""
    value = value.strip()
    if "://" not in value:
        return (
            catalog,
            repository(value) if catalog == "huggingface" else model_name(value),
            None,
            None,
        )
    url = urlsplit(value)
    if (
        url.scheme != "https"
        or url.username
        or url.password
        or url.port not in {None, 443}
        or url.query
        or url.fragment
    ):
        raise ModelManagerError(
            "Paste a public HTTPS Ollama or Hugging Face model link without query parameters."
        )
    parts = unquote(url.path).strip("/").split("/")
    if url.hostname in {"ollama.com", "www.ollama.com"}:
        ident = "/".join(parts[1:] if parts[0] == "library" else parts)
        return "ollama", model_name(ident), None, None
    if url.hostname in {"huggingface.co", "hf.co"} and len(parts) >= 2:
        ident = repository("/".join(parts[:2]))
        variant = None
        revision = None
        if len(parts) > 2:
            if len(parts) < 4 or parts[2] not in {"tree", "blob", "resolve"}:
                raise ModelManagerError(
                    "Use a repository link or a link to a model file."
                )
            # Default-branch links are resolved to a commit by the catalog.
            revision = None if parts[3] == "main" else revision_id(parts[3])
            if parts[2] in {"blob", "resolve"}:
                variant = file_name("/".join(parts[4:]))
        return "huggingface", ident, variant, revision
    raise ModelManagerError("Only Ollama and Hugging Face model links are supported.")
