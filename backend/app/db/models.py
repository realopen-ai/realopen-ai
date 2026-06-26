import uuid
from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, String, Text, DateTime, ForeignKey, Integer, Boolean
from sqlalchemy.dialects.postgresql import UUID, JSONB
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

    # Conversation summary for cross-session context memory.
    # Generated automatically when a conversation reaches min_messages.
    # summary_embedding enables pgvector search across past conversations.
    # See migration: add_conversation_summary_embedding.
    summary = Column(Text, nullable=True)
    summary_embedding = Column(Vector(768), nullable=True)
    summary_at = Column(DateTime, nullable=True)

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

    # Ordered rendering blocks for multi-round agent turns. Each block is
    # one of: thinking, text, tool_call, error. NULL for user/system
    # messages. See migration e2b7c4f1a93d.
    #
    # For assistant messages, `content` holds the concatenation of all
    # text blocks (for tsvector search via search_vector); `blocks` holds
    # the full ordered structure for chronological display.
    blocks = Column(JSONB, nullable=True)
    # Total generation duration across all agent rounds (seconds). Used
    # for the response-time badge under assistant messages.
    generation_duration = Column(Integer, nullable=True)
    # Deliverable files (reports, etc.) produced by tool calls during this
    # message. Array of {type, format, filename, file_path, download_url,
    # created_at}. NULL when no deliverables were produced. See migration
    # f3c8d1e5b4a2.
    deliverables = Column(JSONB, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    # search_vector is a GENERATED ALWAYS AS tsvector column managed by
    # PostgreSQL (see migration f1a5b3c9d2e7). It is NOT mapped as a
    # regular Column here to avoid ORM write attempts — queries that need
    # it use raw SQL (see app/services/session_search.py).

    conversation = relationship(
        "Conversation",
        back_populates="messages",
        foreign_keys=[conversation_id],
    )


class Document(Base):
    """A user-uploaded document digested for RAG.

    Scope:
      - "public":  retrievable by ANY conversation (knowledge base)
      - "private": retrievable ONLY by the conversation whose ID matches
                   `conversation_id` (per-conversation document)

    The raw file lives on disk under {data_dir}/documents/{id}/{filename};
    only metadata lives in the DB. Text + image-description chunks live in
    `document_chunks` with their own 768-dim embedding (nomic-embed-text).

    Digestion is synchronous with SSE progress events. The `digestion_status`
    column drives the Brain page UI:
      pending  → just uploaded, not yet processed
      digesting → extraction/chunking/embedding in progress
      ready    → chunks created, searchable
      failed   → see `digestion_error`
    """

    __tablename__ = "documents"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename = Column(String(512), nullable=False)
    # Original user-facing filename (preserved on rename, used for display)
    original_filename = Column(String(512), nullable=False)
    mime_type = Column(String(128), nullable=False, default="application/octet-stream")

    # Relative path under the data dir, e.g. "documents/abc-123/file.pdf".
    # Joined with the data dir at read time so we never store absolute paths
    # (which would break across container vs host).
    file_path = Column(String(1024), nullable=False)
    file_size_bytes = Column(Integer, nullable=False, default=0)
    content_hash = Column(String(64), nullable=True, index=True)  # sha256 hex

    scope = Column(
        String(16), nullable=False, default="private"
    )  # "private" | "public"
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=True,
    )
    message_id = Column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="SET NULL"),
        nullable=True,
    )

    total_pages = Column(Integer, nullable=True)
    total_chunks = Column(Integer, nullable=False, default=0)
    total_images = Column(Integer, nullable=False, default=0)

    digestion_status = Column(String(16), nullable=False, default="pending")
    digestion_error = Column(Text, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    chunks = relationship(
        "DocumentChunk",
        back_populates="document",
        cascade="all, delete-orphan",
        foreign_keys="DocumentChunk.document_id",
    )


class DocumentChunk(Base):
    """A retrievable chunk of a document.

    Chunks are either:
      - chunk_type="text":              a slice of the document's text
      - chunk_type="image_description": a vision-LLM-generated description
                                        of an image embedded in the document
                                        (PDF/DOCX), stored alongside the
                                        image bytes on disk for download.

    Each chunk has a 768-dim embedding (nomic-embed-text) for vector search
    and a tsvector (DB-managed) for BM25 keyword ranking.

    `page_number`, `line_start`, `line_end` power the source citation shown
    below assistant messages that called the RAG tool.
    """

    __tablename__ = "document_chunks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
    )
    chunk_index = Column(Integer, nullable=False, default=0)

    text = Column(Text, nullable=False)
    page_number = Column(Integer, nullable=True)
    line_start = Column(Integer, nullable=True)
    line_end = Column(Integer, nullable=True)

    chunk_type = Column(
        String(32), nullable=False, default="text"
    )  # "text" | "image_description"
    # For chunk_type="image_description": relative path to the saved image.
    image_path = Column(String(1024), nullable=True)

    # 768-dim to match nomic-embed-text (same as memories.embedding).
    embedding = Column(Vector(768), nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    document = relationship(
        "Document",
        back_populates="chunks",
        foreign_keys=[document_id],
    )


class Memory(Base):
    __tablename__ = "memories"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    text = Column(Text, nullable=False)
    category = Column(
        String(50), default="fact"
    )  # "identity" | "preference" | "fact" | "contact" | "project" | "goal"
    source = Column(String(20), default="auto")  # "auto" | "user" | "ai_agent"
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
