"""documents and document_chunks for RAG

Revision ID: c7e2f9a01b3a
Revises: a8f3c2e1b7d4
Create Date: 2026-06-18 09:30:00.000000

Changes:
- Drop the legacy `documents` table (it was an empty placeholder).
  Its `embedding Vector(1536)` was also the wrong dimension for the
  nomic-embed-text (768) model used everywhere else.
- Create the new `documents` table:
    id, filename, original_filename, mime_type, file_path, file_size_bytes,
    content_hash, scope ('private'|'public'), conversation_id (nullable,
    CASCADE on conversation delete), message_id (nullable, SET NULL on
    message delete), total_pages, total_chunks, total_images,
    digestion_status, digestion_error, created_at, updated_at.
- Create the `document_chunks` table:
    id, document_id (CASCADE), chunk_index, text, page_number, line_start,
    line_end, chunk_type ('text'|'image_description'), image_path,
    embedding Vector(768), created_at.
- Add HNSW index on document_chunks.embedding (cosine).
- Add tsvector + GIN index on document_chunks.text for BM25 keyword ranking,
  GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED —
  same pattern as memories.search_vector.
- Add index on documents.scope + documents.conversation_id so the
  per-conversation RAG retrieval filter is index-backed.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "c7e2f9a01b3a"
down_revision: Union[str, Sequence[str], None] = "a8f3c2e1b7d4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ── 1. Drop the legacy empty documents table ──────────────────────────
    # The previous table had Vector(1536) which is the wrong dim for our
    # embedding model (nomic-embed-text, 768).
    op.execute("DROP TABLE IF EXISTS documents CASCADE")

    # ── 2. New documents table ───────────────────────────────────────────
    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column(
            "mime_type",
            sa.String(128),
            nullable=False,
            server_default="application/octet-stream",
        ),
        sa.Column("file_path", sa.String(1024), nullable=False),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("scope", sa.String(16), nullable=False, server_default="private"),
        sa.Column(
            "conversation_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "message_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("total_pages", sa.Integer(), nullable=True),
        sa.Column("total_chunks", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_images", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "digestion_status",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("digestion_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
        ),
    )

    op.create_index(
        "ix_documents_content_hash",
        "documents",
        ["content_hash"],
        unique=False,
    )
    op.create_index(
        "ix_documents_scope",
        "documents",
        ["scope"],
        unique=False,
    )
    op.create_index(
        "ix_documents_conversation_id",
        "documents",
        ["conversation_id"],
        unique=False,
    )

    # ── 3. document_chunks table ─────────────────────────────────────────
    op.create_table(
        "document_chunks",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "document_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("page_number", sa.Integer(), nullable=True),
        sa.Column("line_start", sa.Integer(), nullable=True),
        sa.Column("line_end", sa.Integer(), nullable=True),
        sa.Column("chunk_type", sa.String(32), nullable=False, server_default="text"),
        sa.Column("image_path", sa.String(1024), nullable=True),
        sa.Column("embedding", Vector(768), nullable=True),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )

    op.create_index(
        "ix_document_chunks_document_id",
        "document_chunks",
        ["document_id"],
        unique=False,
    )

    # ── 4. HNSW index on document_chunks.embedding (cosine similarity) ──
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_embedding_hnsw "
        "ON document_chunks USING hnsw (embedding vector_cosine_ops)"
    )

    # ── 5. tsvector + GIN for BM25 keyword ranking ──────────────────────
    # Same pattern as memories.search_vector: 'simple' dictionary so
    # non-English names survive tokenization; GENERATED ALWAYS AS STORED
    # keeps it in sync without triggers.
    op.execute(
        "ALTER TABLE document_chunks "
        "ADD COLUMN IF NOT EXISTS search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_document_chunks_search_vector_gin "
        "ON document_chunks USING gin (search_vector)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_search_vector_gin")
    op.execute("ALTER TABLE document_chunks DROP COLUMN IF EXISTS search_vector")
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw")

    op.drop_index("ix_document_chunks_document_id", table_name="document_chunks")
    op.drop_table("document_chunks")

    op.drop_index("ix_documents_conversation_id", table_name="documents")
    op.drop_index("ix_documents_scope", table_name="documents")
    op.drop_index("ix_documents_content_hash", table_name="documents")
    op.drop_table("documents")

    # Re-create the legacy placeholder table so a downgrade doesn't lose
    # the original schema shape. (It was never populated.)
    op.create_table(
        "documents",
        sa.Column("id", sa.UUID(as_uuid=True), primary_key=True),
        sa.Column("filename", sa.String(512), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(1536), nullable=True),
        sa.Column(
            "message_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("messages.id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
    )
