import json
import logging
from typing import List, Optional

import httpx
from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


# Request / Response Models


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    model: Optional[str] = (
        None  # Can be a role ("default", "vision"), type ("chat", "code"), or ID ("qwen3:4b")
    )
    stream: bool = False


class ChatResponse(BaseModel):
    model: str
    message: ChatMessage
    done: bool
    total_duration: Optional[int] = None
    eval_count: Optional[int] = None


# Endpoints


@router.post("/chat", response_model=ChatResponse)
async def chat(request: ChatRequest):
    """Send a chat completion request to Ollama (non-streaming).

    The `model` field accepts:
    - A role name: "default", "default_vision", "default_code"
    - A type name: "chat", "vision", "code" (resolved to default_<type>)
    - A direct Ollama model ID: "qwen3:4b"
    """
    # Resolve model role/type to actual model ID
    raw_model = request.model or "default"
    resolved_model = settings.resolve_model(raw_model)

    async with httpx.AsyncClient(timeout=300.0) as client:
        try:
            response = await client.post(
                f"{settings.OLLAMA_BASE_URL}/api/chat",
                json={
                    "model": resolved_model,
                    "messages": [
                        {"role": m.role, "content": m.content} for m in request.messages
                    ],
                    "stream": False,
                },
            )
            response.raise_for_status()
            return response.json()
        except httpx.ConnectError:
            logger.error("Cannot connect to Ollama at %s", settings.OLLAMA_BASE_URL)
            return ChatResponse(
                model=resolved_model,
                message=ChatMessage(
                    role="assistant",
                    content="Error: Cannot connect to AI engine. Please ensure Ollama is running.",
                ),
                done=True,
            )
        except httpx.HTTPStatusError as e:
            logger.error("Ollama error: %s", e)
            return ChatResponse(
                model=resolved_model,
                message=ChatMessage(
                    role="assistant",
                    content=f"Error from AI engine: {e.response.status_code}",
                ),
                done=True,
            )


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Send a chat completion request to Ollama (streaming via SSE).

    The `model` field accepts roles, types, or direct IDs (see /chat).
    """
    raw_model = request.model or "default"
    resolved_model = settings.resolve_model(raw_model)

    async def generate():
        try:
            async with httpx.AsyncClient(timeout=600.0) as client:
                async with client.stream(
                    "POST",
                    f"{settings.OLLAMA_BASE_URL}/api/chat",
                    json={
                        "model": resolved_model,
                        "messages": [
                            {"role": m.role, "content": m.content}
                            for m in request.messages
                        ],
                        "stream": True,
                    },
                ) as response:
                    async for line in response.aiter_lines():
                        if line.strip():
                            yield f"data: {line}\n\n"
        except httpx.ConnectError:
            error_msg = json.dumps(
                {"error": "Cannot connect to Ollama. Is it running?"}
            )
            yield f"data: {error_msg}\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/models")
async def list_models():
    """List available Ollama models (pulled on host)."""
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            response.raise_for_status()
            return response.json()
    except httpx.ConnectError:
        return {"models": [], "error": "Ollama not reachable"}
