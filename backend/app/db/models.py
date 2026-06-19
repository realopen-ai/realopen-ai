import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Integer, Boolean
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=True)
    model = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Watermark for memory extraction: the ID of the last message that has
    # been processed by the memory extractor. NULL means "never extracted" —
    # all messages in this conversation are considered new.
    # See migration a8f3c2e1b7d4.
    memory_watermark_message_id = Column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )

    messages = relationship(
        "Message",
        back_populates="conversation",
        order_by="Message.created_at",
        foreign_keys="Message.conversation_id",
    )


class Message(Base):
    __tablename__ = "messages"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=False
    )
    role = Column(String(20), nullable=False)  # "user", "assistant", "system"
    content = Column(Text, nullable=False)
    model = Column(String(100), nullable=True)
    tokens = Column(Integer, nullable=True)

    has_image = Column(Boolean, default=False)
    has_document = Column(Boolean, default=False)
    image_count = Column(Integer, default=0)
    document_count = Column(Integer, default=0)

    thinking = Column(Text, nullable=True)
    thinking_duration = Column(Integer, nullable=True)
    generation_duration = Column(Integer, nullable=True)
    tool_calls_json = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    conversation = relationship(
        "Conversation",
        back_populates="messages",
        foreign_keys=[conversation_id],
    )


class Document(Base):
    __tablename__ = "documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename = Column(String(512), nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(Vector(1536), nullable=True)  # pgvector column
    message_id = Column(UUID(as_uuid=True), ForeignKey("messages.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class Memory(Base):
    __tablename__ = "memories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    text = Column(Text, nullable=False)
    category = Column(
        String(50), default="fact"
    )  # identity, preference, fact, contact, project, goal
    source = Column(String(20), default="auto")  # auto, user, ai_agent
    pinned = Column(Boolean, default=False)
    uses = Column(Integer, default=0)
    conversation_id = Column(
        UUID(as_uuid=True), ForeignKey("conversations.id"), nullable=True
    )
    # 768-dim to match nomic-embed-text (see migration a8f3c2e1b7d4).
    # Populated by app.services.embeddings.get_embedding() on insert/update.
    embedding = Column(Vector(768), nullable=True)
    # GENERATED ALWAYS AS (to_tsvector('simple', text)) STORED — managed by
    # PostgreSQL, used for BM25 keyword ranking in hybrid retrieval.
    # Not mapped as a Python-side column to avoid ORM write attempts.
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppState(Base):
    """Generic key/value store for persistent application state.

    Used for things that must survive backend restarts:
      - 'memory.last_audit_at'     : ISO timestamp of last audit run
      - 'memory.audit_fingerprint' : SHA-256 of memory set at last audit
      - 'memory.extractions_since_audit' : int counter (alternative to timestamp)
    """
    __tablename__ = "app_state"

    key = Column(String(128), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
