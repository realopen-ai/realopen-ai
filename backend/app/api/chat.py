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
from typing import Any, List, Optional
import uuid

import httpx
from fastapi import APIRouter, Depends, File, Form, UploadFile
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.service import run_agent_stream
from app.config import settings
from app.core.logger import get_debug_logger, RequestTimer, is_debug
from app.db.session import get_db
from app.services import conversations as conv_service

logger = logging.getLogger(__name__)
dbg = get_debug_logger(__name__)

router = APIRouter()


def _log(msg: str, *args: Any) -> None:
    """Print + log a debug message. Uses print() for guaranteed visibility."""
    if is_debug():
        try:
            formatted = msg % args if args else msg
        except (TypeError, ValueError):
            formatted = f"{msg} {args}"
        print(f"[chat] {formatted}", flush=True)
        logger.debug(msg, *args)


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
    dbg(
        "🔵 POST /chat  model=%s  model_override=%s  stream=%s  conv_id=%s  messages_count=%d",
        request.model,
        request.model_override,
        request.stream,
        request.conversation_id,
        len(request.messages),
    )

    with RequestTimer("chat_endpoint", logger):
        raw_model = request.model_override or request.model or "default"
        resolved_model = settings.resolve_model(raw_model)
        dbg("   resolved_model=%s (raw=%s)", resolved_model, raw_model)

        # Persist conversation if ID provided
        conv_id = None
        if request.conversation_id:
            try:
                conv_id = _parse_uuid(request.conversation_id)
                dbg("   parsed conversation_id=%s", conv_id)
            except ValueError:
                dbg("   ⚠️  invalid conversation_id=%s", request.conversation_id)

        if conv_id:
            user_content = request.messages[-1].content if request.messages else ""
            await conv_service.add_message(db, conv_id, "user", user_content)
            dbg("   saved user message to DB (conv_id=%s)", conv_id)

        async with httpx.AsyncClient(timeout=300.0) as client:
            try:
                dbg(
                    "   ➡️  sending to Ollama %s/api/chat  model=%s",
                    settings.OLLAMA_BASE_URL,
                    resolved_model,
                )
                response = await client.post(
                    f"{settings.OLLAMA_BASE_URL}/api/chat",
                    json={
                        "model": resolved_model,
                        "messages": [
                            {"role": m.role, "content": m.content}
                            for m in request.messages
                        ],
                        "stream": False,
                    },
                )
                response.raise_for_status()
                data = response.json()
                dbg(
                    "   ⬅️  Ollama responded  total_duration=%s  eval_count=%s",
                    data.get("total_duration"),
                    data.get("eval_count"),
                )

                # Save assistant message
                if conv_id:
                    content = data.get("message", {}).get("content", "")
                    await conv_service.add_message(
                        db, conv_id, "assistant", content, model=resolved_model
                    )
                    dbg("   saved assistant message to DB (conv_id=%s)", conv_id)

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
                dbg("   ❌ ConnectError to Ollama at %s", settings.OLLAMA_BASE_URL)
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
                dbg("   ❌ HTTPStatusError from Ollama: %s", e)
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
    _log(
        "🔵 POST /chat/stream  model=%s  model_override=%s  stream=%s  conv_id=%s  messages_count=%d",
        request.model,
        request.model_override,
        request.stream,
        request.conversation_id,
        len(request.messages),
    )
    _log(
        "   messages=%s",
        [{"role": m.role, "content": m.content[:100]} for m in request.messages],
    )

    messages = [{"role": m.role, "content": m.content} for m in request.messages]

    raw_model = request.model_override or request.model or "default"
    _log("   raw_model=%s", raw_model)
    resolved_model = settings.resolve_model(raw_model)
    _log("   resolved_model=%s", resolved_model)

    # Resolve conversation ID for persistence
    conv_id = None
    if request.conversation_id:
        try:
            conv_id = _parse_uuid(request.conversation_id)
            dbg("   parsed conversation_id=%s", conv_id)
        except ValueError:
            dbg("   ⚠️  invalid conversation_id=%s", request.conversation_id)

    # Persist the user message at the start of the stream
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

    async def generate():
        dbg("   🔄 generate() started — entering agent loop")
        chunk_count = 0
        full_assistant_content = (
            ""  # To accumulate assistant response for DB persistence
        )
        try:
            async for chunk in run_agent_stream(
                messages=messages,
                model=resolved_model,
                images=None,
            ):
                chunk_count += 1
                if is_debug() and chunk_count <= 5:
                    dbg(
                        "   📦 yielding chunk #%d: %s",
                        chunk_count,
                        chunk[:200] if chunk else "(empty)",
                    )
                data_str = chunk.strip()
                if data_str.startswith("data: "):
                    parsed = json.loads(data_str[6:])
                    if parsed.get("event") == "message" and parsed.get(
                        "message", {}
                    ).get("content"):
                        full_assistant_content += parsed["message"]["content"]
                yield chunk
        except Exception as e:
            import traceback

            _log("   ❌ generate() exception: %s\n%s", e, traceback.format_exc())
            yield f"data: {json.dumps({'event': 'error', 'error': str(e)})}\n\n"
        _log("   ✅ generate() finished — total chunks=%d", chunk_count)
        yield "data: [DONE]\n\n"

        # Persist the assistant message after streaming completes
        if conv_id and full_assistant_content:
            try:
                _log(
                    "   💾 Saving assistant message to DB (conv_id=%s, content_length=%d)",
                    conv_id,
                    len(full_assistant_content),
                )
                await conv_service.add_message(
                    db,
                    conv_id,
                    "assistant",
                    full_assistant_content,
                    model=resolved_model,
                )
                _log("   ✅ Assistant message saved to DB (conv_id=%s)", conv_id)
            except Exception as e:
                dbg("   ❌ Failed to persist assistant message: %s", e)

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
    dbg(
        "🔵 POST /chat/stream/multipart  model=%s  model_override=%s  conv_id=%s  images=%d  docs=%d",
        model,
        model_override,
        conversation_id,
        len(images),
        len(documents),
    )

    # Parse messages
    try:
        parsed_messages = json.loads(messages)
        dbg("   parsed_messages count=%d", len(parsed_messages))
    except json.JSONDecodeError:
        dbg("   ⚠️  JSONDecodeError parsing messages, using raw string")
        parsed_messages = [{"role": "user", "content": messages}]

    raw_model = model_override or model or "default"
    dbg("   raw_model=%s", raw_model)
    resolved_model = settings.resolve_model(raw_model)
    dbg("   resolved_model=%s", resolved_model)

    # Resolve conversation ID for persistence
    conv_id = None
    if conversation_id:
        try:
            conv_id = _parse_uuid(conversation_id)
            dbg("   parsed conversation_id=%s", conv_id)
        except ValueError:
            dbg("   ⚠️  invalid conversation_id=%s", conversation_id)

    # Encode images to base64
    image_b64_list = []
    for img_file in images:
        content = await img_file.read()
        b64 = base64.b64encode(content).decode("utf-8")
        image_b64_list.append(b64)
        dbg(
            "   encoded image: %s (%d bytes → %d b64 chars)",
            img_file.filename,
            len(content),
            len(b64),
        )

    # Process documents (store for now, RAG later)
    # TODO: Implement RAG pipeline for documents
    doc_count = len(documents)
    if doc_count > 0:
        logger.info("Received %d documents (RAG not yet implemented)", doc_count)
        dbg("   documents received: %s", [d.filename for d in documents])

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
            dbg("   ❌ Failed to save user message with media info: %s", e)

    async def generate():
        dbg("   🔄 generate() started (multipart) — entering agent loop")
        chunk_count = 0
        full_assistant_content = (
            ""  # To accumulate assistant response for DB persistence
        )
        try:
            async for chunk in run_agent_stream(
                messages=parsed_messages,
                model=resolved_model,
                images=image_b64_list if image_b64_list else None,
            ):
                chunk_count += 1
                data_str = chunk.strip()
                if data_str.startswith("data: "):
                    parsed = json.loads(data_str[6:])
                    if parsed.get("event") == "message" and parsed.get(
                        "message", {}
                    ).get("content"):
                        full_assistant_content += parsed["message"]["content"]
                yield chunk
        except Exception as e:
            dbg("   ❌ generate() exception: %s", e)
            yield f"data: {json.dumps({'event': 'error', 'error': str(e)})}\n\n"
        dbg("   ✅ generate() finished (multipart) — total chunks=%d", chunk_count)
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
    dbg("🔵 GET /conversations  limit=%d  offset=%d", limit, offset)
    convs = await conv_service.list_conversations(db, limit=limit, offset=offset)
    dbg("   returning %d conversations", len(convs))
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
    dbg("🔵 POST /conversations  title=%s  model=%s", title, model)
    conv = await conv_service.create_conversation(db, title=title, model=model)
    result = await conv_service.conversation_to_dict(conv)
    dbg("   created conversation id=%s", result.get("id"))
    return result


@router.get("/conversations/{conversation_id}")
async def get_conversation(
    conversation_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Get a conversation with its messages."""
    dbg("🔵 GET /conversations/%s", conversation_id)
    conv_id = _parse_uuid(conversation_id)
    conv = await conv_service.get_conversation(db, conv_id)
    if not conv:
        dbg("   ⚠️  conversation not found: %s", conversation_id)
        raise HTTPException(status_code=404, detail="Conversation not found")

    msgs = await conv_service.get_messages(db, conv_id)
    dbg("   returning conversation with %d messages", len(msgs))
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
    dbg("🔵 DELETE /conversations/%s", conversation_id)
    conv_id = _parse_uuid(conversation_id)
    await conv_service.delete_conversation(db, conv_id)
    dbg("   deleted conversation %s", conversation_id)
    return {"status": "deleted"}


@router.patch("/conversations/{conversation_id}")
async def update_conversation(
    conversation_id: str,
    title: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Update a conversation (e.g. rename)."""
    dbg("🔵 PATCH /conversations/%s  title=%s", conversation_id, title)
    conv_id = _parse_uuid(conversation_id)
    if title:
        await conv_service.update_conversation_title(db, conv_id, title)
    dbg("   updated conversation %s", conversation_id)
    return {"status": "updated"}


# ─── Models endpoint ─────────────────────────────────────────────────


@router.get("/models")
async def list_models():
    """List available Ollama models (pulled on host)."""
    dbg("🔵 GET /models  ollama_url=%s", settings.OLLAMA_BASE_URL)
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            response.raise_for_status()
            data = response.json()
            model_count = len(data.get("models", []))
            dbg("   Ollama returned %d models", model_count)
            return data
    except httpx.ConnectError:
        dbg("   ❌ Cannot connect to Ollama at %s", settings.OLLAMA_BASE_URL)
        return {"models": [], "error": "Ollama not reachable"}


# ─── Helpers ─────────────────────────────────────────────────────────


def _parse_uuid(s: str) -> "uuid.UUID":
    """Parse a UUID string, raising ValueError if invalid."""
    import uuid as _uuid

    return _uuid.UUID(s)
