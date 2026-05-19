"""
Conversation persistence service.

Handles CRUD operations for conversations and messages using
SQLAlchemy async sessions.
"""

import uuid
import json as _json
from datetime import datetime
from typing import List, Optional

from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Message, Document


async def create_conversation(
    db: AsyncSession,
    title: str = "New Chat",
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
    db: AsyncSession, limit: int = 50, offset: int = 0
) -> List[Conversation]:
    """List conversations, most recently updated first."""
    result = await db.execute(
        select(Conversation)
        .order_by(Conversation.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
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
    thinking: Optional[str] = None,
    thinking_duration: Optional[int] = None,
    generation_duration: Optional[int] = None,
    tool_calls_json: Optional[str] = None,
) -> Message:
    """Add a message to a conversation."""
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
        thinking=thinking,
        thinking_duration=thinking_duration,
        generation_duration=generation_duration,
        tool_calls_json=tool_calls_json,
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
    """Convert a Conversation ORM object to a dict for API responses."""
    return {
        "id": str(conv.id),
        "title": conv.title,
        "model": conv.model,
        "createdAt": int(conv.created_at.timestamp() * 1000) if conv.created_at else 0,
        "updatedAt": int(conv.updated_at.timestamp() * 1000) if conv.updated_at else 0,
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

    if msg.thinking is not None:
        result["thinking"] = msg.thinking
    if msg.thinking_duration is not None:
        result["thinkingDuration"] = msg.thinking_duration
    if msg.generation_duration is not None:
        result["generationDuration"] = msg.generation_duration

    # Parse tool_calls_json back into a list for the frontend
    if msg.tool_calls_json:
        try:
            result["toolCalls"] = _json.loads(msg.tool_calls_json)
        except _json.JSONDecodeError:
            result["toolCalls"] = []
    else:
        result["toolCalls"] = []

    return result
