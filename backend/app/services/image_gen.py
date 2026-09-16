"""Image generation service — provider implementations and dispatch.

This is the EXECUTION layer for the Image Generation tool
(use_image_gen), following the same layering as web_search /
report_gen / pptx_gen / excel_gen:

    TOOL IMPLEMENTATION      → this service (app/services/image_gen.py)
    TOOL CONFIG DEFINITION   → app/agent/tools/image_gen.py (config_base)
    TOOL CONFIG DATA         → PostgreSQL (tool_configs, via config_store)

Providers (each independently enable/disable-able and configured via the
persisted tool configuration — Brain ▸ Tools ▸ Image Generation):

- Ollama   — local image models through the standard /api/generate
             endpoint (e.g. x/flux2-klein). Base URL defaults to
             settings.OLLAMA_BASE_URL.
- ComfyUI  — PLANNED (workflow-based local generation). The provider
             matrix, config schema and UI structure are already in
             place; adding it later means: implementing
             ``generate_comfyui`` here, adding its defaults/schema to
             the tool's config definition, and a card in the frontend
             ImageGenCustom component — no architectural change.

``generate()`` reads the persisted provider matrix (config store) and
runs the enabled provider with the resolved model — UI changes
therefore change runtime behavior immediately.
"""

import logging
from typing import Any, Dict, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Default generation timeout (seconds) — image diffusion is slow on
# CPU-only machines, so this is generous by design and configurable in
# the tool UI.
DEFAULT_TIMEOUT_S = 600.0


# ── Provider: Ollama ────────────────────────────────────────────────


async def check_model_available(model: str) -> bool:
    """Whether the image model is installed in Ollama."""
    from app.config import ModuleConfig

    return await ModuleConfig.check_model_downloaded(model)


async def generate_ollama(
    prompt: str,
    model: str,
    base_url: Optional[str] = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> bytes:
    """Generate an image with an Ollama image model.

    Calls the Ollama /api/generate endpoint which, for image generation
    models, returns the image as base64 (``images`` list, or a single
    ``image`` field for x/flux2-klein style models).
    """
    url = (base_url or settings.OLLAMA_BASE_URL).rstrip("/")
    async with httpx.AsyncClient(timeout=float(timeout_s)) as client:
        response = await client.post(
            f"{url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
            },
        )
        response.raise_for_status()
        data = response.json()

    # Standard image-model response: base64 strings under "images".
    if "images" in data and data["images"]:
        raw = data["images"][0]
        if isinstance(raw, bytes):
            return raw
        from base64 import b64decode

        return b64decode(raw)

    # x/flux2-klein style models return a single "image" field.
    if model.startswith("x/flux2-klein") and "image" in data and data["image"]:
        from base64 import b64decode

        return b64decode(data["image"])

    # No image payload — the model probably isn't an image model.
    raise ValueError(
        f"Model '{model}' did not return image data. "
        "It may not support image generation."
    )


# ── Provider matrix: read config → run the enabled provider ────────


def _providers_config() -> Dict[str, Any]:
    """The persisted provider matrix (Brain ▸ Tools ▸ Image Generation).

    Local import: keeps this service importable from the tool module
    during package initialization without circular imports (same
    pattern as app/services/web_search.py).
    """
    from app.agent.tools import config_store

    cfg = config_store.get_tool_config("use_image_gen") or {}
    return (cfg.get("custom") or {}).get("providers") or {}


async def generate(
    prompt: str,
    model: Optional[str] = None,
    providers_cfg: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Run the enabled image-generation provider.

    ``model`` is the resolved image model (Brain ▸ Tools override →
    image task slot) — used by providers that need one (Ollama).
    ``providers_cfg`` overrides the persisted configuration (tests).

    Returns ``{"image": bytes, "provider": "Ollama"}`` — or raises
    RuntimeError when no provider is enabled.
    """
    if providers_cfg is None:
        providers_cfg = _providers_config()
    providers_cfg = providers_cfg or {}

    ollama = providers_cfg.get("ollama", {})
    if ollama.get("enabled", True):
        resolved = model or settings.resolve_model("default_image_gen")
        if not resolved:
            raise RuntimeError("No image generation model configured")

        if not await check_model_available(resolved):
            raise RuntimeError(
                f"Model '{resolved}' not installed. " f"Run: ollama pull {resolved}"
            )

        image = await generate_ollama(
            prompt,
            model=resolved,
            base_url=ollama.get("base_url"),
            timeout_s=ollama.get("timeout_s", DEFAULT_TIMEOUT_S),
        )
        return {"image": image, "provider": "Ollama"}

    raise RuntimeError(
        "No image generation provider enabled — enable one in "
        "Brain → Tools → Image Generation."
    )
