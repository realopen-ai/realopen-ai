"""
Chat API endpoints.

Supports:
- Plain text chat (streaming & non-streaming)
- Image input (auto-invokes vision model)
- Document upload (digested synchronously into the RAG knowledge base)
- AI agent with tool calling (web search, vision, code exec, rag_search)
- Conversation persistence via PostgreSQL
"""

import base64
import json
import logging
import time
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
from app.db.session import get_db, async_session_factory
from app.services import conversations as conv_service
from app.services import model_prefs
from app.services import providers
from app.services import rag as rag_service
from app.services.blocks import BlockBuilder as _BlockBuilder
from app.services.conversations import persist_message_standalone
from app.services.conversations import DEFAULT_TITLE
from app.services.memory_extractor import maybe_run_memory_extraction
from app.services.title_generator import maybe_generate_and_save_title
from app.services.conversation_memory import maybe_summarize_conversation
from app.services.background_queue import (
    mark_stream_active,
    mark_stream_idle,
)
from app.core import metrics as app_metrics

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
        resolved_model = await model_prefs.resolve_chat_request_model(raw_model)
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
            dbg("   ✅  saved user message to DB (conv_id=%s)", conv_id)

        try:
            dbg(
                "   ➡️  sending chat request (provider=%s)  model=%s",
                providers.provider_of(resolved_model),
                resolved_model,
            )
            # Provider-routed call;
            # always returns an Ollama-shaped response dict.
            data = await providers.chat_once(
                resolved_model,
                [{"role": m.role, "content": m.content} for m in request.messages],
                timeout=1200.0,
            )
            dbg(
                "   ⬅️  LLM responded  total_duration=%s  eval_count=%s",
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

                # Auto-title the conversation from the first user
                # message (LLM via default_utility; no-op on later turns
                # and when the title is no longer the default).
                try:
                    new_title = await maybe_generate_and_save_title(
                        conv_id,
                        request.messages[-1].content if request.messages else "",
                    )
                    if new_title:
                        dbg("   🏷️  conversation auto-titled: %r", new_title)
                except Exception as e:
                    dbg("   ⚠️  auto-title failed (non-fatal): %s", e)

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
            logger.error("Cannot connect to the model provider for %s", resolved_model)
            dbg("   ❌ ConnectError for model %s", resolved_model)
            return ChatResponse(
                model=resolved_model,
                message=ChatMessage(
                    role="assistant",
                    content=(
                        "Error: Cannot connect to AI engine. Please ensure Ollama "
                        "is running (or the cloud provider is reachable)."
                    ),
                ),
                done=True,
            )
        except httpx.HTTPStatusError as e:
            logger.error("LLM provider error: %s", e)
            dbg("   ❌ HTTPStatusError from provider: %s", e)
            return ChatResponse(
                model=resolved_model,
                message=ChatMessage(
                    role="assistant",
                    content=f"Error from AI engine: {e.response.status_code}",
                ),
                done=True,
            )


async def _persist_message(
    conv_id: uuid.UUID, role: str, content: str, model: str | None = None, **kwargs
) -> uuid.UUID | None:
    """Persist a message using an independent DB session with explicit commit.

    Thin alias over the shared ``persist_message_standalone`` in
    app/services/conversations.py (moved here so the voice WebSocket
    session can reuse the exact same persistence path). Behavior is
    identical to the previous inline implementation.

    Returns the new message's ID on success, or None on failure.
    """
    msg_id = await persist_message_standalone(conv_id, role, content, model, **kwargs)
    if msg_id is not None:
        _log(
            "   ✅ %s message committed to DB (conv_id=%s, content_len=%d, msg_id=%s)",
            role,
            conv_id,
            len(content),
            msg_id,
        )
    else:
        _log("  ❌ Failed to persist %s message", role)
    return msg_id


# ─── Memory extraction helper ──────────────────────────────────────────
#
# The implementation moved to app/services/memory_extractor.py (public
# function ``maybe_run_memory_extraction``) so the voice WebSocket session
# shares the exact same post-turn behavior. The private alias below keeps
# every internal reference in this module unchanged.


async def _maybe_run_memory_extraction(
    conv_id: uuid.UUID,
    full_assistant_content: str,
    request_messages: list,
) -> tuple[int, bool, bool]:
    """Check the conversation watermark and enqueue memory extraction if due.

    Thin alias over the shared ``maybe_run_memory_extraction`` in
    app/services/memory_extractor.py — same behavior, same return shape
    (added_count, did_run, pending).
    """
    return await maybe_run_memory_extraction(
        conv_id, full_assistant_content, request_messages
    )


@router.post("/chat/stream")
async def chat_stream(request: ChatRequest):
    """Send a chat completion request with agent tools (streaming via SSE).

    The `model` field accepts roles, types, or direct IDs (see /chat).
    The agent loop will automatically invoke tools when needed.

    IMPORTANT — Database sessions & StreamingResponse:
    The get_db() dependency commits and closes the session AFTER the route
    handler returns.  But with StreamingResponse, the route handler returns
    immediately while the generate() function runs later (during response
    streaming).  So by the time generate() tries to use the session, it is
    already committed/closed.

    Solution: use independent sessions (async_session_factory) with explicit
    commits for both user and assistant messages in streaming endpoints.
    We do NOT use Depends(get_db) on streaming endpoints.
    """
    _log(
        "🔵 POST /chat/stream  model=%s  model_override=%s  stream=%s  "
        "conv_id=%s  messages_count=%d",
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
    resolved_model = await model_prefs.resolve_chat_request_model(raw_model)
    _log("   resolved_model=%s", resolved_model)

    # Resolve conversation ID for persistence
    conv_id = None
    if request.conversation_id:
        try:
            conv_id = _parse_uuid(request.conversation_id)
            _log("   parsed conversation_id=%s", conv_id)
        except ValueError:
            _log("   ⚠️  invalid conversation_id=%s", request.conversation_id)

    # Persist the user message EAGERLY using an independent session.
    # We must commit before returning the StreamingResponse because there
    # is no request-scoped DB session to rely on.
    if conv_id and messages:
        await _persist_message(
            conv_id,
            "user",
            messages[-1]["content"],
            has_image=False,
            has_document=False,
        )

    # Capture for closure — these are used inside generate() which runs
    # after this function has already returned
    _conv_id = conv_id
    _resolved_model = resolved_model
    # The user's message — captured BEFORE generate() runs so the auto-
    # titler always sees the original text (not anything the agent loop
    # may have appended to the messages list).
    _user_msg_for_title = messages[-1]["content"] if messages else ""

    async def generate():
        _log("   🔄 generate() started — entering agent loop")
        # Mark this conversation's stream as active so the background
        # extraction queue knows to wait before running (KV-cache protection).
        if _conv_id:
            await mark_stream_active(str(_conv_id))
        chunk_count = 0
        request_start = None
        builder = _BlockBuilder()
        try:
            request_start = time.time()
            async for chunk in run_agent_stream(
                messages=messages,
                model=_resolved_model,
                images=None,
                conversation_id=str(_conv_id) if _conv_id else None,
            ):
                chunk_count += 1
                if is_debug() and chunk_count <= 5:
                    _log(
                        "   📦 yielding chunk #%d: %s",
                        chunk_count,
                        chunk[:200] if chunk else "(empty)",
                    )
                # Reconstruct ordered blocks from SSE events for DB persistence.
                data_str = chunk.strip()
                if data_str.startswith("data: "):
                    try:
                        parsed = json.loads(data_str[6:])
                        event_type = parsed.get("event")

                        if event_type == "thinking_start":
                            builder.on_thinking_start()

                        if event_type == "thinking" and parsed.get("thinking"):
                            builder.on_thinking_token(parsed["thinking"])

                        if (
                            event_type == "thinking_done"
                            and parsed.get("thinkingDuration") is not None
                        ):
                            builder.on_thinking_done(parsed["thinkingDuration"])

                        if event_type == "generation_done":
                            if parsed.get("generationDuration") is not None:
                                builder.on_generation_done(parsed["generationDuration"])

                        if event_type == "message" and parsed.get("message", {}).get(
                            "content"
                        ):
                            builder.on_message_token(parsed["message"]["content"])

                        if event_type == "tool_call" and parsed.get("tool_call"):
                            tc = parsed["tool_call"]
                            tc_id = tc.get("id", "")
                            if tc.get("status") == "running":
                                builder.on_tool_call_start(tc)
                            else:
                                # completed or error — update existing block
                                updates = {k: v for k, v in tc.items() if k != "id"}
                                builder.on_tool_call_update(tc_id, updates)

                        if event_type == "rag_sources" and parsed.get("sources"):
                            tc_id = parsed.get("tool_call_id", "")
                            builder.on_rag_sources(tc_id, parsed["sources"])

                        if event_type == "error" and parsed.get("error"):
                            builder.on_error(str(parsed["error"]))

                    except json.JSONDecodeError:
                        pass
                yield chunk
        except Exception as e:
            import traceback

            _log("   ❌ generate() exception: %s\n%s", e, traceback.format_exc())
            builder.on_error(str(e))
            yield f"data: {json.dumps({'event': 'error', 'error': str(e)})}\n\n"

        full_assistant_content = builder.get_text_content()
        _log(
            "   ✅ generate() finished — total chunks=%d  blocks=%d  content_len=%d",
            chunk_count,
            len(builder.blocks),
            len(full_assistant_content),
        )

        # Persist the assistant message BEFORE yielding [DONE].
        if _conv_id and builder.blocks:
            await _persist_message(
                _conv_id,
                "assistant",
                full_assistant_content,
                model=_resolved_model,
                blocks=builder.to_db_blocks(),
                generation_duration=builder.generation_duration,
                deliverables=builder.deliverables if builder.deliverables else None,
            )

            # Mark the stream as idle so the background extraction queue
            # knows it's safe to run without evicting the KV cache.
            if _conv_id:
                await mark_stream_idle(str(_conv_id))

            # Memory extraction — watermark check is inline (fast DB read),
            # the actual LLM extraction is enqueued to the background queue
            # which waits for stream idle before running. See
            # _maybe_run_memory_extraction() for details.
            try:
                yield "data: " + json.dumps(
                    {
                        "event": "memory_extraction_start",
                    }
                ) + "\n\n"
                added, did_run, pending = await _maybe_run_memory_extraction(
                    _conv_id,
                    full_assistant_content,
                    messages,
                )
                yield "data: " + json.dumps(
                    {
                        "event": "memory_extraction_done",
                        "count": added,
                        "ran": did_run,
                        "pending": pending,
                    }
                ) + "\n\n"
            except Exception as e:
                _log("   ⚠️  memory extraction failed: %s", e)
                yield "data: " + json.dumps(
                    {
                        "event": "memory_extraction_done",
                        "count": 0,
                        "ran": False,
                        "error": str(e),
                    }
                ) + "\n\n"

            # ── Conversation summarization for cross-session context ──
            # Runs after memory extraction. Summarizes the conversation
            # when it reaches a milestone (8+ messages) for future context.
            try:
                async with async_session_factory() as summ_db:
                    summarized = await maybe_summarize_conversation(
                        summ_db,
                        _conv_id,
                        _resolved_model,
                    )
                    if summarized:
                        await summ_db.commit()
                        _log("   📝 conversation summarized for cross-session memory")
            except Exception as e:
                _log("   ⚠️  conversation summarization failed (non-fatal): %s", e)

        # ── Auto-title from the first user message (LLM via ──
        # default_utility). Runs at most once per conversation: the service
        # skips unless the title is still the default and this is the first
        # turn. The SSE event updates the sidebar live; the DB write inside
        # the service makes the title survive reloads.
        if _conv_id and _user_msg_for_title:
            try:
                new_title = await maybe_generate_and_save_title(
                    _conv_id,
                    _user_msg_for_title,
                )
                if new_title:
                    _log("   🏷️  conversation auto-titled: %r", new_title)
                    yield "data: " + json.dumps(
                        {"event": "conversation_title", "title": new_title}
                    ) + "\n\n"
            except Exception as e:
                _log("   ⚠️  auto-title failed (non-fatal): %s", e)

        # ── Record metrics for the chat request ──
        if request_start:
            elapsed = time.time() - request_start
            app_metrics.chat_duration_seconds.labels(model=_resolved_model).observe(
                elapsed
            )
            app_metrics.chat_requests_total.labels(
                model=_resolved_model, status="success"
            ).inc()
            token_estimate = len(full_assistant_content) + sum(
                len(m.get("content", "")) for m in messages
            )
            app_metrics.chat_tokens_total.labels(model=_resolved_model).inc(
                int(token_estimate * 0.4)
            )

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


@router.post("/chat/stream/multipart")
async def chat_stream_multipart(
    messages: str = Form(...),
    model: Optional[str] = Form(None),
    model_override: Optional[str] = Form(None),
    conversation_id: Optional[str] = Form(None),
    images: List[UploadFile] = File(default=[]),
    documents: List[UploadFile] = File(default=[]),
):
    """Stream chat with optional image and document uploads.

    This endpoint accepts multipart/form-data with:
    - messages: JSON string of [{role, content}, ...]
    - model: Optional model role/type/id
    - model_override: Optional model override from slash commands
    - conversation_id: Optional conversation ID for persistence
    - images: Image files (auto-invokes vision model)
    - documents: Document files (stored for future RAG, no processing yet)

    Same independent-session pattern as /chat/stream for DB persistence.
    No Depends(get_db) - we use async_session_factory explicitly.
    """
    _log(
        "🔵 POST /chat/stream/multipart  model=%s  model_override=%s  "
        "conv_id=%s  images=%d  docs=%d",
        model,
        model_override,
        conversation_id,
        len(images),
        len(documents),
    )

    # Parse messages
    try:
        parsed_messages = json.loads(messages)
        _log("   parsed_messages count=%d", len(parsed_messages))
    except json.JSONDecodeError:
        _log("   ⚠️  JSONDecodeError parsing messages, using raw string")
        parsed_messages = [{"role": "user", "content": messages}]

    raw_model = model_override or model or "default"
    _log("   raw_model=%s", raw_model)
    resolved_model = await model_prefs.resolve_chat_request_model(raw_model)
    _log("   resolved_model=%s", resolved_model)

    # Resolve conversation ID for persistence
    conv_id = None
    if conversation_id:
        try:
            conv_id = _parse_uuid(conversation_id)
            _log("   parsed conversation_id=%s", conv_id)
        except ValueError:
            _log("   ⚠️  invalid conversation_id=%s", conversation_id)

    # Encode images to base64
    image_b64_list = []
    for img_file in images:
        content = await img_file.read()
        b64 = base64.b64encode(content).decode("utf-8")
        image_b64_list.append(b64)
        _log(
            "   encoded image: %s (%d bytes, %d b64 chars)",
            img_file.filename,
            len(content),
            len(b64),
        )

    # Process documents: digest synchronously into RAG with SSE progress.
    # Documents uploaded through chat are PRIVATE to this conversation —
    # they're retrievable only when the agent's rag_search tool fires from
    # this same conversation. (Public docs go through the Brain page.)
    doc_count = len(documents)
    if doc_count > 0:
        logger.info(
            "[chat] received %d document(s) for RAG digestion (conv=%s)",
            doc_count,
            conv_id,
        )
        _log("   documents received: %s", [d.filename for d in documents])

    # Read all file bytes up-front — UploadFile streams can't be re-read.
    # Store (filename, bytes) pairs so the SSE generator can re-use them.
    doc_buffers: List[tuple] = []
    for d in documents:
        try:
            buf = await d.read()
            doc_buffers.append((d.filename or "upload", buf))
        except Exception as e:
            logger.warning("[chat] failed to read uploaded doc %s: %s", d.filename, e)

    # Persist the user message EAGERLY using an independent session
    if conv_id and parsed_messages:
        await _persist_message(
            conv_id,
            "user",
            parsed_messages[-1]["content"],
            has_image=len(images) > 0,
            has_document=len(documents) > 0,
            image_count=len(images),
            document_count=len(documents),
        )

    # Capture for closure
    _conv_id = conv_id
    _resolved_model = resolved_model
    # The user's message — captured BEFORE generate() runs, because the
    # document-digestion step appends a system hint to parsed_messages
    # inside the generator (which would otherwise become the last message).
    _user_msg_for_title = parsed_messages[-1]["content"] if parsed_messages else ""

    async def generate():
        dbg("   🔄 generate() started (multipart) — entering agent loop")
        # Mark this conversation's stream as active (KV-cache protection).
        if _conv_id:
            await mark_stream_active(str(_conv_id))
        chunk_count = 0
        request_start = None
        builder = _BlockBuilder()
        digested_doc_ids: List[str] = []  # for DB link to user message
        # Track digested document filenames so we can inject a system
        # hint into the agent's messages telling it these files are now
        # searchable via rag_search. Without this hint the LLM doesn't
        # know it has fresh documents to look up.
        digested_doc_filenames: List[str] = []

        # ── Digest uploaded documents BEFORE running the agent loop.
        # This way the agent's rag_search tool can find the docs on the
        # very same turn. Each doc emits document_digest_* SSE events
        # in REAL TIME via an asyncio.Queue — the user sees progress
        # immediately in the chat conversation, not after digestion
        # completes.
        if doc_buffers and _conv_id:
            import asyncio as _asyncio

            for fname, fbytes in doc_buffers:
                progress_queue: _asyncio.Queue = _asyncio.Queue()
                _DONE_SENTINEL = object()

                def collect(p: "rag_service.DigestProgress", _f=fname) -> None:
                    try:
                        progress_queue.put_nowait(p)
                    except Exception as _e:
                        _log("   ⚠️  failed to enqueue progress event: %s", _e)

                _log("   📄 digesting %s (%d bytes) for RAG", fname, len(fbytes))

                # Digestion runs as a background task while we drain
                # progress events from the queue in real-time. This is
                # the same pattern as /documents/upload/stream.
                digestion_err: List[Optional[Exception]] = [None]
                digestion_doc: List[Optional[object]] = [None]

                async def run_digest(_f=fname, _b=fbytes):
                    try:
                        async with async_session_factory() as db:
                            doc = await rag_service.digest_document(
                                db,
                                file_bytes=_b,
                                filename=_f,
                                scope="private",
                                conversation_id=_conv_id,
                                progress=collect,
                            )
                            digestion_doc[0] = doc
                    except Exception as e:
                        digestion_err[0] = e
                    finally:
                        try:
                            progress_queue.put_nowait(_DONE_SENTINEL)
                        except Exception:
                            pass

                digest_task = _asyncio.create_task(run_digest())

                # Drain the queue and yield each progress event as an
                # SSE chunk in real-time. Loop until we see the sentinel.
                while True:
                    try:
                        item = await _asyncio.wait_for(
                            progress_queue.get(), timeout=0.1
                        )
                    except _asyncio.TimeoutError:
                        if digest_task.done():
                            break
                        continue
                    if item is _DONE_SENTINEL:
                        break
                    yield "data: " + json.dumps(
                        {
                            "event": "document_digest_progress",
                            "stage": item.stage,
                            "percent": item.percent,
                            "details": item.details,
                            "filename": fname,
                            "document_id": item.document_id,
                            "total_chunks": item.total_chunks,
                            "total_images": item.total_images,
                        }
                    ) + "\n\n"

                # Await the task to surface any exception
                try:
                    await digest_task
                except Exception as e:
                    _log("   ⚠️  digest task crashed for %s: %s", fname, e)

                if digestion_err[0] is not None:
                    e = digestion_err[0]
                    logger.exception("[chat] doc digestion failed for %s: %s", fname, e)
                    yield "data: " + json.dumps(
                        {
                            "event": "document_digest_error",
                            "filename": fname,
                            "error": str(e),
                        }
                    ) + "\n\n"
                    continue

                if digestion_doc[0] is not None:
                    doc = digestion_doc[0]
                    digested_doc_ids.append(str(doc.id))
                    digested_doc_filenames.append(doc.filename)
                    yield "data: " + json.dumps(
                        {
                            "event": "document_digest_done",
                            "filename": fname,
                            "document_id": str(doc.id),
                            "total_chunks": doc.total_chunks,
                            "total_images": doc.total_images,
                        }
                    ) + "\n\n"

        # ── Inject a system hint about freshly-uploaded documents.
        # If we just digested any docs, prepend a user-role hint telling
        # the agent these files are now searchable via rag_search. This
        # dramatically increases the chance the LLM uses rag_search
        # proactively instead of answering from generic knowledge.
        if digested_doc_filenames:
            hint = (
                f"[System: The user just uploaded {len(digested_doc_filenames)} "
                f"document(s): {', '.join(digested_doc_filenames)}. "
                f"These are now indexed in your knowledge base. "
                f"If the user's question is about content in these files, "
                f"USE the rag_search tool to retrieve relevant excerpts "
                f"BEFORE answering. Do not answer from generic knowledge "
                f"when the documents may contain the specific information.]"
            )
            parsed_messages.append({"role": "user", "content": hint})

        try:
            request_start = time.time()
            async for chunk in run_agent_stream(
                messages=parsed_messages,
                model=_resolved_model,
                images=image_b64_list if image_b64_list else None,
                conversation_id=str(_conv_id) if _conv_id else None,
            ):
                chunk_count += 1
                if is_debug() and chunk_count <= 5:
                    _log(
                        "   📦 yielding chunk #%d: %s",
                        chunk_count,
                        chunk[:200] if chunk else "(empty)",
                    )
                # Reconstruct ordered blocks from SSE events for DB persistence.
                data_str = chunk.strip()
                if data_str.startswith("data: "):
                    try:
                        parsed = json.loads(data_str[6:])
                        event_type = parsed.get("event")

                        if event_type == "thinking_start":
                            builder.on_thinking_start()

                        if event_type == "thinking" and parsed.get("thinking"):
                            builder.on_thinking_token(parsed["thinking"])

                        if (
                            event_type == "thinking_done"
                            and parsed.get("thinkingDuration") is not None
                        ):
                            builder.on_thinking_done(parsed["thinkingDuration"])

                        if event_type == "generation_done":
                            if parsed.get("generationDuration") is not None:
                                builder.on_generation_done(parsed["generationDuration"])

                        if event_type == "message" and parsed.get("message", {}).get(
                            "content"
                        ):
                            builder.on_message_token(parsed["message"]["content"])

                        if event_type == "tool_call" and parsed.get("tool_call"):
                            tc = parsed["tool_call"]
                            tc_id = tc.get("id", "")
                            if tc.get("status") == "running":
                                builder.on_tool_call_start(tc)
                            else:
                                updates = {k: v for k, v in tc.items() if k != "id"}
                                builder.on_tool_call_update(tc_id, updates)

                        if event_type == "rag_sources" and parsed.get("sources"):
                            tc_id = parsed.get("tool_call_id", "")
                            builder.on_rag_sources(tc_id, parsed["sources"])

                        if event_type == "error" and parsed.get("error"):
                            builder.on_error(str(parsed["error"]))

                    except json.JSONDecodeError:
                        pass
                yield chunk
        except Exception as e:
            dbg("   ❌ generate() exception: %s", e)
            builder.on_error(str(e))
            yield f"data: {json.dumps({'event': 'error', 'error': str(e)})}\n\n"

        full_assistant_content = builder.get_text_content()
        _log(
            "   ✅ generate() finished (multipart) — total chunks=%d  "
            "blocks=%d  content_len=%d",
            chunk_count,
            len(builder.blocks),
            len(full_assistant_content),
        )

        # Persist the assistant message BEFORE yielding [DONE].
        if _conv_id and builder.blocks:
            await _persist_message(
                _conv_id,
                "assistant",
                full_assistant_content,
                model=_resolved_model,
                blocks=builder.to_db_blocks(),
                generation_duration=builder.generation_duration,
                deliverables=builder.deliverables if builder.deliverables else None,
            )

            # Mark stream idle so background extraction can proceed.
            if _conv_id:
                await mark_stream_idle(str(_conv_id))

            # Memory extraction — watermark check inline, LLM extraction
            # enqueued to background queue (KV-cache protection).
            try:
                yield "data: " + json.dumps(
                    {
                        "event": "memory_extraction_start",
                    }
                ) + "\n\n"
                added, did_run, pending = await _maybe_run_memory_extraction(
                    _conv_id,
                    full_assistant_content,
                    parsed_messages,
                )
                yield "data: " + json.dumps(
                    {
                        "event": "memory_extraction_done",
                        "count": added,
                        "ran": did_run,
                        "pending": pending,
                    }
                ) + "\n\n"
            except Exception as e:
                _log("   ⚠️  memory extraction failed (multipart): %s", e)
                yield "data: " + json.dumps(
                    {
                        "event": "memory_extraction_done",
                        "count": 0,
                        "ran": False,
                        "error": str(e),
                    }
                ) + "\n\n"

            # ── Conversation summarization for cross-session context ──
            try:
                async with async_session_factory() as summ_db:
                    summarized = await maybe_summarize_conversation(
                        summ_db,
                        _conv_id,
                        _resolved_model,
                    )
                    if summarized:
                        await summ_db.commit()
                        _log("   📝 conversation summarized (multipart)")
            except Exception as e:
                _log("   ⚠️  summarization failed (non-fatal): %s", e)

        # ── Auto-title from the first user message (LLM via ──
        # default_utility). Same guard + SSE event as /chat/stream.
        if _conv_id and _user_msg_for_title:
            try:
                new_title = await maybe_generate_and_save_title(
                    _conv_id,
                    _user_msg_for_title,
                )
                if new_title:
                    _log("   🏷️  conversation auto-titled (multipart): %r", new_title)
                    yield "data: " + json.dumps(
                        {"event": "conversation_title", "title": new_title}
                    ) + "\n\n"
            except Exception as e:
                _log("   ⚠️  auto-title failed (non-fatal): %s", e)

        # ── Record metrics for the multipart chat request ──
        if request_start:
            elapsed = time.time() - request_start
            app_metrics.chat_duration_seconds.labels(model=_resolved_model).observe(
                elapsed
            )
            app_metrics.chat_requests_total.labels(
                model=_resolved_model, status="success"
            ).inc()
            token_estimate = len(full_assistant_content) + sum(
                len(m.get("content", "")) for m in messages
            )
            app_metrics.chat_tokens_total.labels(model=_resolved_model).inc(
                int(token_estimate * 0.4)
            )

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


# ─── Conversation CRUD ───────────────────────────────────────────────


@router.get("/conversations")
async def list_conversations(
    limit: int = 50,
    offset: int = 0,
    archived: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
):
    """List conversations.

    Ordering: pinned conversations first (most recently pinned at the top),
    then by most recently updated.

    ``archived`` query param filters the result:
      * omitted — all conversations
      * ``false`` — only active conversations (sidebar main list)
      * ``true``  — only archived conversations (sidebar "Archived" section)
    """
    dbg(
        "🔵 GET /conversations  limit=%d  offset=%d  archived=%s",
        limit,
        offset,
        archived,
    )
    convs = await conv_service.list_conversations(
        db, limit=limit, offset=offset, archived=archived
    )
    dbg("   returning %d conversations", len(convs))
    return {
        "conversations": [await conv_service.conversation_to_dict(c) for c in convs]
    }


@router.post("/conversations")
async def create_conversation(
    title: str = DEFAULT_TITLE,
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
    pinned: Optional[bool] = None,
    archived: Optional[bool] = None,
    db: AsyncSession = Depends(get_db),
):
    """Update a conversation.

    Supports rename (``title``), pin/unpin (``pinned``) and
    archive/unarchive (``archived``) — all as optional query params; only
    the params that are passed are applied. Powers the sidebar's
    per-conversation three-dots menu.
    """
    dbg(
        "🔵 PATCH /conversations/%s  title=%s  pinned=%s  archived=%s",
        conversation_id,
        title,
        pinned,
        archived,
    )
    conv_id = _parse_uuid(conversation_id)

    if title:
        await conv_service.update_conversation_title(db, conv_id, title)

    if pinned is not None or archived is not None:
        found = await conv_service.set_conversation_flags(
            db, conv_id, pinned=pinned, archived=archived
        )
        if not found:
            dbg("   ⚠️  conversation not found: %s", conversation_id)
            raise HTTPException(status_code=404, detail="Conversation not found")

    conv = await conv_service.get_conversation(db, conv_id)
    dbg("   updated conversation %s", conversation_id)
    return {
        "status": "updated",
        "conversation": (
            await conv_service.conversation_to_dict(conv) if conv else None
        ),
    }


# ─── Past-conversation search ────────────────────────────────────────


@router.post("/conversations/search")
async def search_past_conversations(
    query: str,
    exclude_conversation_id: Optional[str] = None,
    limit: int = 10,
):
    """Search past conversation transcripts by keyword.

    Uses PostgreSQL tsvector + ts_rank_cd on messages.content for fast
    BM25-ranked search. Returns matching messages with their source
    conversation title, role, snippet, and rank.

    This endpoint powers the Brain page's past-conversations search UI
    and is also callable by the LLM via the search_past_conversations tool.
    """
    from app.services.session_search import search_past_messages

    try:
        async with async_session_factory() as db:
            results = await search_past_messages(
                db,
                query=query,
                limit=limit,
                exclude_conversation_id=exclude_conversation_id,
            )
        return {
            "results": results,
            "total": len(results),
            "query": query,
        }
    except Exception as e:
        _log("past-conversation search failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


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
    return uuid.UUID(s)
