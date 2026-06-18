"""memory vector search and watermark

Revision ID: 3d03bc86fc5f
Revises: cd21b53e84c1
Create Date: 2026-06-17 10:53:39.485080

Changes:
- Resize memories.embedding from VECTOR(1536) -> VECTOR(768) to match
  the nomic-embed-text model (768-dim) configured in profiles.yml.
- Add HNSW index on memories.embedding (cosine) for fast similarity search.
- Add tsvector column + GIN index on memories.text for BM25 keyword ranking.
- Add conversations.memory_watermark_message_id (UUID -> messages.id) so
  the extractor can skip already-processed messages and only run every N
  new messages (default 4) instead of after every turn.
- Add app_state key/value table for persistent counters (audit cadence,
  last audit fingerprint, etc.) so they survive backend restarts.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "a8f3c2e1b7d4"
down_revision: Union[str, Sequence[str], None] = "cd21b53e84c1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ── 1. Resize memories.embedding 1536 -> 768 ─────────────────────────
    # Drop any existing index first (the auto index from the column type
    # doesn't exist, but be defensive). Then alter the column dimension.
    # NULL values are preserved; the column is nullable so existing rows
    # (which all have NULL embeddings today) are unaffected.
    op.execute(
        "ALTER TABLE memories ALTER COLUMN embedding TYPE vector(768) USING NULL"
    )

    # ── 2. HNSW index on memories.embedding (cosine similarity) ──────────
    # HNSW is preferred over IVFFlat for small-to-medium corpora and
    # supports incremental inserts without a manual rebuild.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memories_embedding_hnsw "
        "ON memories USING hnsw (embedding vector_cosine_ops)"
    )

    # ── 3. tsvector column + GIN index for BM25 keyword ranking ──────────
    # Use the 'simple' dictionary so non-English names (Clémence, Søren)
    # are not stemmed or dropped — only lowercased and split on punctuation.
    # GENERATED ALWAYS AS STORED keeps the column in sync with `text`
    # automatically; no triggers needed.
    op.execute(
        "ALTER TABLE memories "
        "ADD COLUMN IF NOT EXISTS search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_memories_search_vector_gin "
        "ON memories USING gin (search_vector)"
    )

    # ── 4. Watermark column on conversations ─────────────────────────────
    # Tracks the last message ID that has been processed by the memory
    # extractor. NULL means "never extracted" — all messages are new.
    op.add_column(
        "conversations",
        sa.Column(
            "memory_watermark_message_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # ── 5. app_state table for persistent counters ───────────────────────
    # Generic key/value table for things that should survive restarts:
    #   - 'memory.last_audit_at'    : timestamp of last audit
    #   - 'memory.audit_fingerprint': SHA-256 of memory set at last audit
    #   - 'memory.extractions_since_audit': counter (alternative to timestamp)
    op.create_table(
        "app_state",
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("app_state")

    op.drop_column("conversations", "memory_watermark_message_id")

    op.execute("DROP INDEX IF EXISTS ix_memories_search_vector_gin")
    op.execute("ALTER TABLE memories DROP COLUMN IF EXISTS search_vector")

    op.execute("DROP INDEX IF EXISTS ix_memories_embedding_hnsw")
    op.execute(
        "ALTER TABLE memories ALTER COLUMN embedding TYPE vector(1536) USING NULL"
    )
