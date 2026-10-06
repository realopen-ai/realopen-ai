import uuid
from datetime import datetime, timezone

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
)
from sqlalchemy import JSON
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import DeclarativeBase, relationship


class Base(DeclarativeBase):
    pass


class FlashcardDeck(Base):
    __tablename__ = "flashcard_decks"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    source_conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True)
    source_document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Flashcard(Base):
    __tablename__ = "flashcards"
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    deck_id = Column(UUID(as_uuid=True), ForeignKey("flashcard_decks.id", ondelete="CASCADE"), nullable=False, index=True)
    front = Column(Text, nullable=False)
    back = Column(Text, nullable=False)
    position = Column(Integer, nullable=False)
    source_reference = Column(String(500), nullable=True)
    source_page = Column(Integer, nullable=True)
    source_chunk_id = Column(UUID(as_uuid=True), ForeignKey("document_chunks.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class FlashcardProgress(Base):
    __tablename__ = "flashcard_progress"
    card_id = Column(UUID(as_uuid=True), ForeignKey("flashcards.id", ondelete="CASCADE"), primary_key=True)
    due_at = Column(DateTime(timezone=True), nullable=False, index=True)
    interval_days = Column(Integer, nullable=False, default=0)
    ease = Column(Float, nullable=False, default=2.5)
    repetitions = Column(Integer, nullable=False, default=0)
    lapses = Column(Integer, nullable=False, default=0)
    reviews = Column(Integer, nullable=False, default=0)
    last_reviewed_at = Column(DateTime(timezone=True), nullable=True)


class FlashcardReview(Base):
    __tablename__ = "flashcard_reviews"
    id = Column(UUID(as_uuid=True), primary_key=True)  # Client idempotency key
    card_id = Column(UUID(as_uuid=True), ForeignKey("flashcards.id", ondelete="CASCADE"), nullable=False, index=True)
    rating = Column(String(8), nullable=False)
    reviewed_at = Column(DateTime(timezone=True), nullable=False)
    due_at = Column(DateTime(timezone=True), nullable=False)
    interval_days = Column(Integer, nullable=False)


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(255), nullable=True)
    model = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Sidebar organization flags. Pinned conversations float to the top of
    # the sidebar (most recently pinned first); archived conversations are
    # hidden from the main list and shown under a collapsible "Archived"
    # section. Both are toggled from the per-conversation three-dots menu.
    # See migration: b5d1e4f7a8c2.
    pinned = Column(Boolean, nullable=False, default=False, server_default="false")
    pinned_at = Column(DateTime, nullable=True)
    archived = Column(Boolean, nullable=False, default=False, server_default="false")
    archived_at = Column(DateTime, nullable=True)

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

    # A conversation may use one persistent workspace. The sandbox itself
    # can be shared by several conversations and outlives its containers.
    sandbox_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sandboxes.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    messages = relationship(
        "Message",
        back_populates="conversation",
        order_by="Message.created_at",
        foreign_keys="Message.conversation_id",
    )
    sandbox = relationship("Sandbox", back_populates="conversations")


class Sandbox(Base):
    """Persistent workspace metadata; compute containers are disposable."""

    __tablename__ = "sandboxes"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(120), nullable=False)
    status = Column(String(24), nullable=False, default="stopped")
    desired_running = Column(Boolean, nullable=False, default=False)
    volume_name = Column(String(180), nullable=False, unique=True)
    container_name = Column(String(180), nullable=False, unique=True)
    image = Column(String(255), nullable=False, default="realopenai-sandbox:latest")
    cpu_limit = Column(Float, nullable=False, default=2.0)
    memory_limit_mb = Column(Integer, nullable=False, default=2048)
    workspace_quota_bytes = Column(
        BigInteger, nullable=False, default=2 * 1024 * 1024 * 1024
    )
    usage_bytes = Column(BigInteger, nullable=False, default=0)
    idle_timeout_seconds = Column(Integer, nullable=False, default=1800)
    last_active_at = Column(DateTime, nullable=True)
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    conversations = relationship("Conversation", back_populates="sandbox")
    tasks = relationship(
        "SandboxTask",
        back_populates="sandbox",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    commands = relationship(
        "SandboxCommand",
        back_populates="sandbox",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SandboxTask(Base):
    """Auditable state for a delegated coding-agent run."""

    __tablename__ = "sandbox_tasks"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sandbox_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sandboxes.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id = Column(
        UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="SET NULL")
    )
    status = Column(String(24), nullable=False, default="queued")
    request = Column(Text, nullable=False)
    summary = Column(Text, nullable=True)
    worklog = Column(JSONB, nullable=False, default=list)
    files_changed = Column(JSONB, nullable=False, default=list)
    tests_run = Column(JSONB, nullable=False, default=list)
    cancellation_requested = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    sandbox = relationship("Sandbox", back_populates="tasks")
    commands = relationship("SandboxCommand", back_populates="task")


class SandboxCommand(Base):
    """Durable command and output history for a sandbox workspace."""

    __tablename__ = "sandbox_commands"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    sandbox_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sandboxes.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id = Column(
        UUID(as_uuid=True),
        ForeignKey("sandbox_tasks.id", ondelete="CASCADE"),
        nullable=True,
    )
    conversation_id = Column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
    )
    source = Column(String(32), nullable=False)
    tool_name = Column(String(80), nullable=False)
    sequence = Column(Integer, nullable=True)
    command = Column(Text, nullable=False)
    cwd = Column(String(1024), nullable=False, default="/workspace")
    stdout = Column(Text, nullable=False, default="")
    stderr = Column(Text, nullable=False, default="")
    exit_code = Column(Integer, nullable=True)
    output_truncated = Column(Boolean, nullable=False, default=False)
    started_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    duration_ms = Column(Integer, nullable=True)

    sandbox = relationship("Sandbox", back_populates="commands")
    task = relationship("SandboxTask", back_populates="commands")


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
    generation_duration = Column(Float, nullable=True)
    # Deliverable files (reports, etc.) produced by tool calls during this
    # message. Array of {type, format, filename, file_path, download_url,
    # created_at}. NULL when no deliverables were produced. See migration
    # f3c8d1e5b4a2.
    deliverables = Column(JSONB, nullable=True)
    # Input/output modality of this message — "voice" for messages captured
    # from the microphone / spoken through the voice pipeline; NULL (= "text")
    # for all pre-existing rows. Metadata only: a voice message is a normal
    # message everywhere else (same blocks, same search, same UI).
    # See migration c4e8f2a1b6d3.
    modality = Column(String(20), nullable=True)
    # Durable lifecycle for streamed assistant messages. Existing rows are
    # "completed"; live turns progress through "streaming" and may finish
    # as "interrupted" or "error".
    completion_status = Column(String(20), nullable=False, default="completed")

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

    # User-assignable knowledge groups (Workspace > Documents detail
    # modal, e.g. ["Company", "Strategy"]). JSON list of strings — kept
    # simple on purpose (no join table); only the documents API touches it.
    collections = Column(JSON, nullable=True, default=list)

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


class ToolConfig(Base):
    """Per-tool agent configuration (Brain ▸ Tools).

    One row per discovered tool, keyed by the stable tool identifier
    (BaseTool.name — e.g. "use_websearch"). The `config` JSONB blob holds
    BOTH the universal settings (enabled / always_load / tags /
    model override) and any tool-specific custom settings (e.g. the Web
    Search provider matrix). Defaults come from the tool configuration
    definitions under app/agent/tools/ (config_base) — they are used to
    SEED missing rows and to fill partially-missing keys at read time;
    the database is the source of truth after initialization and is
    never silently overwritten by changed defaults.

    Secrets (API keys) do NOT live here — see app/services/secrets.py
    (file store under the state dir). The config JSON only references
    them implicitly via the tool's declared secret fields.
    """

    __tablename__ = "tool_configs"

    tool_name = Column(String(128), primary_key=True)
    config = Column(JSONB, nullable=False, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class Template(Base):
    """A PPTX template for presentation generation.

    Templates are .pptx files stored under backend/app/templates/pptx/.
    The DB row holds metadata (display name, description, tags, thumbnail)
    so the Workspace UI can display and manage them dynamically.

    The `slug` is generated from `display_name` and is also the filename
    on disk (e.g. slug="research_template" → file="research_template.pptx").
    The `path` field stores the relative path from the templates directory.
    """

    __tablename__ = "templates"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    display_name = Column(String(255), nullable=False)
    slug = Column(String(255), nullable=False, unique=True, index=True)
    description = Column(Text, nullable=True)
    tags = Column(JSONB, nullable=True)  # ["corporate", "minimal", ...]
    thumbnail = Column(Text, nullable=True)  # base64-encoded small preview image
    path = Column(
        String(1024), nullable=False
    )  # relative path: "research_template.pptx"
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
