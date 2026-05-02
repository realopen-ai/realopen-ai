"""
Chat API endpoints.

Supports:
- Plain text chat (streaming & non-streaming)
- Image input (auto-invokes vision model)
- Document upload (stored for future RAG)
- AI agent with tool calling (web search, vision, code exec)
- Conversation persistence via PostgreSQL
"""

import base64
import json
import logging
from typing import List, Optional
import uuid

import httpx
from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.service import run_agent_stream
from app.config import settings
from app.db.session import get_db
from app.services import conversations as conv_service

logger = logging.getLogger("uvicorn.error")  # Use uvicorn's logger for consistency
logger.setLevel(logging.INFO)  # Set to INFO or DEBUG as needed
logger.info("Chat API router initialized")

router = APIRouter()


# ─── Request / Response Models ───────────────────────────────────────


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    messages: List[ChatMessage]
    model: Optional[str] = None
    stream: bool = False
    conversation_id: Optional[str] = None
    # Slash command options from frontend
    model_override: Optional[str] = None
    shrug: Optional[bool] = None


class ChatResponse(BaseModel):
    model: str
    message: ChatMessage
    done: bool
    total_duration: Optional[int] = None
    eval_count: Optional[int] = None
    conversation_id: Optional[str] = None


class ConversationListResponse(BaseModel):
    conversations: List[dict]


class ConversationDetailResponse(BaseModel):
    conversation: dict
    messages: List[dict]


# ─── Endpoints ───────────────────────────────────────────────────────


@router.post("/chat", response_model=ChatResponse)
async def chat(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send a chat completion request to Ollama (non-streaming).

    The `model` field accepts:
    - A role name: "default", "default_vision", "default_code"
    - A type name: "chat", "vision", "code" (resolved to default_<type>)
    - A direct Ollama model ID: "qwen3:4b"
    """
    raw_model = request.model_override or request.model or "default"
    resolved_model = settings.resolve_model(raw_model)

    # Persist conversation if ID provided
    conv_id = None
    if request.conversation_id:
        try:
            conv_id = _parse_uuid(request.conversation_id)
        except ValueError:
            pass

    if conv_id:
        # Save user message
        await conv_service.add_message(
            db,
            conv_id,
            "user",
            request.messages[-1].content if request.messages else "",
        )

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
            data = response.json()

            # Save assistant message
            if conv_id:
                content = data.get("message", {}).get("content", "")
                await conv_service.add_message(
                    db, conv_id, "assistant", content, model=resolved_model
                )

            return ChatResponse(
                model=resolved_model,
                message=ChatMessage(
                    **data.get("message", {"role": "assistant", "content": ""})
                ),
                done=True,
                total_duration=data.get("total_duration"),
                eval_count=data.get("eval_count"),
                conversation_id=str(conv_id) if conv_id else None,
            )
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
async def chat_stream(request: ChatRequest, db: AsyncSession = Depends(get_db)):
    """Send a chat completion request with agent tools (streaming via SSE).

    The `model` field accepts roles, types, or direct IDs (see /chat).
    The agent loop will automatically invoke tools when needed.
    """
    logger.info(
        "Received chat stream request: model=%s, messages=%d, conversation_id=%s",
        request.model_override or request.model or "default",
        len(request.messages),
        request.conversation_id,
    )
    raw_model = request.model_override or request.model or "default"
    messages = [{"role": m.role, "content": m.content} for m in request.messages]

    # Resolve conversation ID for persistence
    conv_id = None
    if request.conversation_id:
        try:
            conv_id = _parse_uuid(request.conversation_id)
        except ValueError:
            logger.warning("Invalid conversation_id: %s", request.conversation_id)

    # Persist the user message
    if conv_id and messages:
        try:
            await conv_service.add_message(
                db,
                conv_id,
                "user",
                messages[-1]["content"],
                has_image=False,
                has_document=False,
            )
        except Exception as e:
            logger.warning("Failed to persist user message: %s", e)

    logger.info(
        "Starting chat stream with model=%s, messages=%d, conversation_id=%s",
        raw_model,
        len(messages),
        request.conversation_id,
    )

    resolved_model = settings.resolve_model(raw_model)

    async def generate():
        full_assistant_content = ""
        async for chunk in run_agent_stream(
            messages=messages,
            model=raw_model,
            images=None,
        ):
            # Track assistant content for persistence
            try:
                data_str = chunk.strip()
                if data_str.startswith("data: "):
                    import json as _json

                    parsed = _json.loads(data_str[6:])
                    if parsed.get("event") == "message" and parsed.get(
                        "message", {}
                    ).get("content"):
                        full_assistant_content += parsed["message"]["content"]
            except Exception:
                pass
            yield chunk
        yield "data: [DONE]\n\n"

        # Persist the assistant message after streaming completes
        if conv_id and full_assistant_content:
            try:
                await conv_service.add_message(
                    db,
                    conv_id,
                    "assistant",
                    full_assistant_content,
                    model=resolved_model,
                )
            except Exception as e:
                logger.warning("Failed to persist assistant message: %s", e)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/chat/stream/multipart")
async def chat_stream_multipart(
    messages: str = Form(...),
    model: Optional[str] = Form(None),
    model_override: Optional[str] = Form(None),
    conversation_id: Optional[str] = Form(None),
    images: List[UploadFile] = File(default=[]),
    documents: List[UploadFile] = File(default=[]),
    db: AsyncSession = Depends(get_db),
):
    """Stream chat with optional image and document uploads.

    This endpoint accepts multipart/form-data with:
    - messages: JSON string of [{role, content}, ...]
    - model: Optional model role/type/id
    - model_override: Optional model override from slash commands
    - conversation_id: Optional conversation ID for persistence
    - images: Image files (auto-invokes vision model)
    - documents: Document files (stored for future RAG, no processing yet)
    """
    logger.info(
        "Received multipart chat stream request: model=%s, messages=%d, images=%d, documents=%d, conversation_id=%s",
        model_override or model or "default",
        len(json.loads(messages) if messages else []),
        len(images),
        len(documents),
        conversation_id,
    )

    # Parse messages
    try:
        parsed_messages = json.loads(messages)
    except json.JSONDecodeError:
        parsed_messages = [{"role": "user", "content": messages}]

    logger.info(
        "Starting multipart chat stream with model=%s, messages=%d, images=%d, documents=%d, conversation_id=%s",
        model_override or model or "default",
        len(parsed_messages),
        len(images),
        len(documents),
        conversation_id,
    )

    raw_model = model_override or model or "default"
    resolved_model = settings.resolve_model(raw_model)

    # Resolve conversation ID for persistence
    conv_id = None
    if conversation_id:
        try:
            conv_id = _parse_uuid(conversation_id)
        except ValueError:
            logger.warning("Invalid conversation_id: %s", conversation_id)

    # Encode images to base64
    image_b64_list = []
    for img_file in images:
        content = await img_file.read()
        b64 = base64.b64encode(content).decode("utf-8")
        image_b64_list.append(b64)
        logger.info(
            "Received image: filename=%s, size=%d bytes",
            img_file.filename,
            len(content),
        )

    # Process documents (store for now, RAG later)
    # TODO: Implement RAG pipeline for documents
    doc_count = len(documents)
    if doc_count > 0:
        logger.info("Received %d documents (RAG not yet implemented)", doc_count)

    # Persist the user message
    if conv_id and parsed_messages:
        try:
            await conv_service.add_message(
                db,
                conv_id,
                "user",
                parsed_messages[-1]["content"],
                has_image=len(images) > 0,
                has_document=len(documents) > 0,
                image_count=len(images),
                document_count=len(documents),
            )
        except Exception as e:
            logger.warning("Failed to persist user message: %s", e)

    async def generate():
        full_assistant_content = ""
        async for chunk in run_agent_stream(
            messages=parsed_messages,
            model=raw_model,
            images=image_b64_list if image_b64_list else None,
        ):
            # Track assistant content for persistence
            try:
                data_str = chunk.strip()
                if data_str.startswith("data: "):
                    parsed = json.loads(data_str[6:])
                    if parsed.get("event") == "message" and parsed.get(
                        "message", {}
                    ).get("content"):
                        full_assistant_content += parsed["message"]["content"]
            except Exception:
                pass
            yield chunk
        yield "data: [DONE]\n\n"

        # Persist the assistant message after streaming completes
        if conv_id and full_assistant_content:
            try:
                await conv_service.add_message(
                    db,
                    conv_id,
                    "assistant",
                    full_assistant_content,
                    model=resolved_model,
                )
            except Exception as e:
                logger.warning("Failed to persist assistant message: %s", e)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ─── Conversation CRUD ───────────────────────────────────────────────


@router.get("/conversations")
async def list_conversations(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """List all conversations."""
    convs = await conv_service.list_conversations(db, limit=limit, offset=offset)
    return {
        "conversations": [await conv_service.conversation_to_dict(c) for c in convs]
    }


@router.post("/conversations")
async def create_conversation(
    title: str = "New Chat",
    model: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Create a new conversation."""
    conv = await conv_service.create_conversation(db, title=title, model=model)
    return await conv_service.conversation_to_dict(conv)


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get a conversation with its messages."""
    from fastapi import HTTPException

    conv_id = _parse_uuid(conversation_id)
    conv = await conv_service.get_conversation(db, conv_id)
    if not conv:
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs = await conv_service.get_messages(db, conv_id)
    return {
        "conversation": await conv_service.conversation_to_dict(conv),
        "messages": [await conv_service.message_to_dict(m) for m in msgs],
    }


@router.delete("/conversations/{conversation_id}")
async def delete_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Delete a conversation."""
    conv_id = _parse_uuid(conversation_id)
    await conv_service.delete_conversation(db, conv_id)
    return {"status": "deleted"}


@router.patch("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    title: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Update a conversation (e.g. rename)."""
    conv_id = _parse_uuid(conversation_id)
    if title:
        await conv_service.update_conversation_title(db, conv_id, title)
    return {"status": "updated"}


# ─── Models endpoint ─────────────────────────────────────────────────


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


# ─── Helpers ─────────────────────────────────────────────────────────


def _parse_uuid(s: str) -> "uuid.UUID":
    """Parse a UUID string, raising ValueError if invalid."""
    import uuid as _uuid

    return _uuid.UUID(s)
