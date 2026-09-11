"""Provider management API (Settings ▸ AI ▸ Providers).

Endpoints:
    GET    /api/providers          — status of every provider
    POST   /api/providers/groq     — validate + save the Groq API key
    DELETE /api/providers/groq     — disconnect (remove the saved key)
"""

import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.services import providers as provider_service

router = APIRouter()

# Groq keys look like "gsk_..." — accept anything reasonably shaped but
# reject obvious garbage early (the real check is the mini request).
_KEY_SHAPE = re.compile(r"^[A-Za-z0-9_\-]{16,}$")


class GroqConnectRequest(BaseModel):
    api_key: str


@router.get("/providers")
async def get_providers():
    """Status of all configured providers (local + cloud)."""
    ollama = await provider_service.ollama_status()
    groq = await provider_service.groq_status()
    return {"providers": [ollama, groq]}


@router.post("/providers/groq")
async def connect_groq(request: GroqConnectRequest):
    """Validate a Groq API key with a mini request, then save it.

    The key is checked by calling GET /openai/v1/models on Groq with the
    Bearer key — no tokens are generated. The key is persisted ONLY when
    Groq accepts it.
    """
    api_key = (request.api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=400, detail="API key is required")
    if not _KEY_SHAPE.match(api_key):
        raise HTTPException(
            status_code=400, detail="This does not look like a Groq API key"
        )

    ok, detail = await provider_service.test_groq_key(api_key)
    if not ok:
        raise HTTPException(status_code=400, detail=detail)

    provider_service.set_groq_key(api_key)
    return {
        "connected": True,
        "key_masked": provider_service._mask_key(api_key),
        "model_count": len(provider_service.GROQ_MODELS),
    }


@router.delete("/providers/groq")
async def disconnect_groq():
    """Remove the saved Groq API key (provider disconnects)."""
    removed = provider_service.clear_groq_key()
    return {"connected": False, "removed": removed}
