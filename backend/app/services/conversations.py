"""
Conversation persistence service.

Handles CRUD operations for conversations and messages using
SQLAlchemy async sessions.
"""

import logging
import uuid
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Message, Document

logger = logging.getLogger(__name__)

# Default title applied to freshly created conversations. The auto-titling
# service (services/title_generator.py) replaces it with a short LLM-
# generated title after the first user message.
DEFAULT_TITLE = "New Chat"


async def create_conversation(
    db: AsyncSession,
    title: str = DEFAULT_TITLE,
    model: Optional[str] = None,
) -> Conversation:
    """Create a new conversation."""
    conv = Conversation(
        id=uuid.uuid4(),
        title=title,
        model=model,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    db.add(conv)
    await db.flush()
    return conv


async def get_conversation(
    db: AsyncSession, conversation_id: uuid.UUID
) -> Optional[Conversation]:
    """Get a conversation by ID."""
    result = await db.execute(
        select(Conversation).where(Conversation.id == conversation_id)
    )
    return result.scalar_one_or_none()


async def list_conversations(
    db: AsyncSession,
    limit: int = 50,
    offset: int = 0,
    archived: Optional[bool] = None,
) -> List[Conversation]:
    """List conversations.

    Ordering: pinned conversations first (most recently pinned at the top),
    then everything else by most recently updated.

    ``archived`` filters the result:
      * ``None``  — all conversations (pinned-first ordering still applies)
      * ``False`` — only active conversations (sidebar main list)
      * ``True``  — only archived conversations (sidebar "Archived" section)
    """
    stmt = select(Conversation)
    if archived is not None:
        stmt = stmt.where(Conversation.archived.is_(archived))
    stmt = stmt.order_by(
        Conversation.pinned.desc(),
        Conversation.pinned_at.desc(),
        Conversation.updated_at.desc(),
    )
    result = await db.execute(stmt.limit(limit).offset(offset))
    return list(result.scalars().all())


async def update_conversation_title(
    db: AsyncSession, conversation_id: uuid.UUID, title: str
) -> None:
    """Update a conversation's title."""
    await db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(title=title, updated_at=datetime.utcnow())
    )
    await db.flush()


async def set_conversation_flags(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    pinned: Optional[bool] = None,
    archived: Optional[bool] = None,
) -> bool:
    """Set a conversation's pinned / archived flags.

    Only flags explicitly passed as ``True`` / ``False`` are changed
    (``None`` = leave unchanged). Returns ``True`` if the conversation
    exists, ``False`` if it was not found.

    Pinning an archived conversation also unarchives it — a pinned chat
    would otherwise be invisible in the main sidebar list.
    """
    conv = await get_conversation(db, conversation_id)
    if not conv:
        return False

    now = datetime.utcnow()
    values: dict = {}

    if pinned is not None:
        values["pinned"] = pinned
        values["pinned_at"] = now if pinned else None
        # A pinned chat must be visible: pin implies unarchive.
        if pinned and conv.archived:
            values["archived"] = False
            values["archived_at"] = None

    if archived is not None:
        # Don't clobber the unarchive triggered by pinning above.
        if "archived" not in values:
            values["archived"] = archived
            values["archived_at"] = now if archived else None
        # Archiving unpins — an archived chat can't float on top of the
        # main list it no longer appears in.
        if archived and conv.pinned:
            values["pinned"] = False
            values["pinned_at"] = None

    if values:
        await db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(**values)
        )
        await db.flush()
    return True


async def delete_conversation(db: AsyncSession, conversation_id: uuid.UUID) -> None:
    """Delete a conversation and all its messages."""
    await db.execute(delete(Message).where(Message.conversation_id == conversation_id))
    await db.execute(delete(Conversation).where(Conversation.id == conversation_id))
    await db.flush()


async def add_message(
    db: AsyncSession,
    conversation_id: uuid.UUID,
    role: str,
    content: str,
    model: Optional[str] = None,
    tokens: Optional[int] = None,
    has_image: bool = False,
    has_document: bool = False,
    image_count: int = 0,
    document_count: int = 0,
    blocks: Optional[list] = None,
    generation_duration: Optional[int] = None,
    deliverables: Optional[list] = None,
    modality: Optional[str] = None,
    completion_status: str = "completed",
) -> Message:
    """Add a message to a conversation.

    For assistant messages, `blocks` is the ordered array of rendering
    blocks (thinking / text / tool_call / error) and `content` is the
    concatenation of text blocks (for tsvector search). For user messages,
    `blocks` is None and `content` is the user's text.

    `deliverables` is an optional array of file metadata for generated
    deliverables (reports, etc.) — persisted so the frontend can render
    download badges that survive page refresh.

    `modality` marks the input/output source of the message — "text"
    (default, NULL) or "voice". It is metadata only: voice messages are
    normal messages everywhere else. See migration c4e8f2a1b6d3.
    """
    msg = Message(
        id=uuid.uuid4(),
        conversation_id=conversation_id,
        role=role,
        content=content,
        model=model,
        tokens=tokens,
        has_image=has_image,
        has_document=has_document,
        image_count=image_count,
        document_count=document_count,
        blocks=blocks,
        generation_duration=generation_duration,
        deliverables=deliverables,
        modality=modality,
        completion_status=completion_status,
        created_at=datetime.utcnow(),
    )
    db.add(msg)
    # Update conversation's updated_at
    await db.execute(
        update(Conversation)
        .where(Conversation.id == conversation_id)
        .values(updated_at=datetime.utcnow())
    )
    await db.flush()
    return msg


async def get_messages(
    db: AsyncSession, conversation_id: uuid.UUID, limit: int = 100
) -> List[Message]:
    """Get messages for a conversation, ordered by creation time."""
    result = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def persist_message_standalone(
    conv_id: uuid.UUID,
    role: str,
    content: str,
    model: Optional[str] = None,
    **kwargs,
) -> Optional[uuid.UUID]:
    """Persist a message using an independent DB session with explicit commit.

    This is safe to call from inside a StreamingResponse generator or a
    WebSocket session task because it creates its own session — it does not
    depend on any request-scoped get_db() session.

    Shared by the text-chat streaming endpoints (previously the private
    ``_persist_message`` in app/api/chat.py) and the voice WebSocket session
    (app/voice/session.py).

    Returns the new message's ID on success, or None on failure.
    """
    from app.db.session import async_session_factory

    try:
        async with async_session_factory() as session:
            msg = await add_message(
                session, conv_id, role, content, model=model, **kwargs
            )
            await session.commit()
        logger.debug(
            "%s message committed to DB (conv_id=%s, content_len=%d, msg_id=%s)",
            role,
            conv_id,
            len(content),
            msg.id,
        )
        return msg.id
    except Exception as e:
        logger.error("Failed to persist %s message: %s", role, e)
        return None


async def update_message_standalone(
    message_id: uuid.UUID,
    *,
    content: str,
    blocks: Optional[list] = None,
    generation_duration: Optional[int] = None,
    deliverables: Optional[list] = None,
    completion_status: str = "streaming",
) -> bool:
    """Update a streamed assistant message using an independent session."""
    from app.db.session import async_session_factory

    try:
        async with async_session_factory() as session:
            result = await session.execute(
                update(Message)
                .where(Message.id == message_id)
                .values(
                    content=content,
                    blocks=blocks,
                    generation_duration=generation_duration,
                    deliverables=deliverables,
                    completion_status=completion_status,
                )
            )
            await session.commit()
        if result.rowcount != 1:
            logger.warning("Streamed message %s was not found for update", message_id)
            return False
        return True
    except Exception as e:
        logger.error("Failed to update streamed message %s: %s", message_id, e)
        return False


async def save_document(
    db: AsyncSession,
    filename: str,
    content: str,
    message_id: Optional[uuid.UUID] = None,
) -> Document:
    """Save an uploaded document."""
    doc = Document(
        id=uuid.uuid4(),
        filename=filename,
        content=content,
        message_id=message_id,
        created_at=datetime.utcnow(),
    )
    db.add(doc)
    await db.flush()
    return doc


async def conversation_to_dict(conv: Conversation) -> dict:
    """Convert a Conversation ORM object to a dict for API responses.

    Exposes the cross-session summary fields (summary, summary_at) so the
    frontend can display what was injected as past-conversation context.
    """
    return {
        "id": str(conv.id),
        "title": conv.title,
        "model": conv.model,
        "sandboxId": str(conv.sandbox_id) if conv.sandbox_id else None,
        "createdAt": int(conv.created_at.timestamp() * 1000) if conv.created_at else 0,
        "updatedAt": int(conv.updated_at.timestamp() * 1000) if conv.updated_at else 0,
        # Sidebar organization flags (three-dots menu)
        "pinned": bool(conv.pinned),
        "archived": bool(conv.archived),
        "pinnedAt": (
            int(conv.pinned_at.timestamp() * 1000)
            if getattr(conv, "pinned_at", None)
            else None
        ),
        "archivedAt": (
            int(conv.archived_at.timestamp() * 1000)
            if getattr(conv, "archived_at", None)
            else None
        ),
        # Cross-session context visibility — lets the Brain page show
        # which conversations have been summarized and what the summary is.
        "summary": conv.summary if hasattr(conv, "summary") else None,
        "summaryAt": (
            int(conv.summary_at.timestamp() * 1000)
            if hasattr(conv, "summary_at") and conv.summary_at
            else None
        ),
    }


async def message_to_dict(msg: Message) -> dict:
    """Convert a Message ORM object to a dict for API responses."""
    result = {
        "id": str(msg.id),
        "conversationId": str(msg.conversation_id),
        "role": msg.role,
        "content": msg.content,
        "model": msg.model,
        "tokens": msg.tokens,
        "hasImage": msg.has_image,
        "hasDocument": msg.has_document,
        "imageCount": msg.image_count,
        "documentCount": msg.document_count,
        "createdAt": int(msg.created_at.timestamp() * 1000) if msg.created_at else 0,
    }

    # blocks — ordered rendering blocks for assistant messages.
    # NULL for user/system messages. The frontend renders these in order
    # to preserve the chronological flow of multi-round agent turns.
    if msg.blocks is not None:
        result["blocks"] = msg.blocks
    else:
        result["blocks"] = None

    if msg.generation_duration is not None:
        result["generationDuration"] = msg.generation_duration

    # deliverables — array of file metadata for generated reports etc.
    # NULL when no deliverables were produced. The frontend renders
    # download badges from this so they survive page refresh.
    if msg.deliverables is not None:
        result["deliverables"] = msg.deliverables
    else:
        result["deliverables"] = None

    # modality — "voice" for messages captured/spoken through the voice
    # pipeline, NULL (omitted) for regular text messages.
    if getattr(msg, "modality", None):
        result["modality"] = msg.modality

    result["completionStatus"] = getattr(msg, "completion_status", "completed")

    return result
