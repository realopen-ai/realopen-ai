"""hnsw index on conversation summary_embedding + tsvector on messages

Revision ID: f1a5b3c9d2e7
Revises: d1e4a2b8c9f0
Create Date: 2026-06-22 12:00:00.000000

Changes:
- Add HNSW index on conversations.summary_embedding (cosine). Previously
  vector search on summary_embedding did a sequential scan — fine for
  <1k conversations, catastrophic at 10k+. HNSW gives sub-millisecond
  approximate nearest-neighbor search.
- Add a tsvector + GIN index on messages.content for past-conversation
  transcript search (the new search_past_conversations tool). Uses the
  'simple' dictionary so non-ASCII names survive tokenization, matching
  the existing pattern on memories.search_vector and
  document_chunks.search_vector.
- Add a partial btree index on conversations.summary_at where summary is
    not null (already exists in some forms; this is idempotent).
"""

from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f1a5b3c9d2e7"
down_revision: Union[str, Sequence[str], None] = "d1e4a2b8c9f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""

    # ── 1. HNSW index on conversations.summary_embedding ────────────────
    # Cosine distance operator class (vector_cosine_ops) — matches how
    # conversation_memory.get_relevant_summaries queries (1 - (emb <=> q)).
    # IF NOT EXISTS so re-running the migration is a no-op.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversations_summary_embedding_hnsw "
        "ON conversations USING hnsw (summary_embedding vector_cosine_ops)"
    )

    # ── 2. tsvector + GIN on messages.content ────────────────────────────
    # Used by the search_past_conversations tool to find specific past
    # turns by keyword. The 'simple' dictionary preserves non-English
    # names (Clémence, Søren) that 'english' would stem away.
    op.execute(
        "ALTER TABLE messages "
        "ADD COLUMN IF NOT EXISTS search_vector tsvector "
        "GENERATED ALWAYS AS (to_tsvector('simple', coalesce(content, ''))) STORED"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_search_vector_gin "
        "ON messages USING gin (search_vector)"
    )

    # ── 3. Partial btree on conversations.summary_at (idempotent) ────────
    # Speeds up get_recent_summaries() which orders by summary_at desc
    # filtering on summary is not null.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_conversations_summary_at "
        "ON conversations (summary_at DESC) "
        "WHERE summary IS NOT NULL"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_conversations_summary_at")
    op.execute("DROP INDEX IF EXISTS ix_messages_search_vector_gin")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS search_vector")
    op.execute("DROP INDEX IF EXISTS ix_conversations_summary_embedding_hnsw")
