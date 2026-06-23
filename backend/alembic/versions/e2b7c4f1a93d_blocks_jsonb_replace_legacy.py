"""replace legacy thinking/tool_calls_json with blocks JSONB

Revision ID: e2b7c4f1a93d
Revises: f1a5b3c9d2e7
Create Date: 2026-06-22 14:00:00.000000

Changes:
- Drop the legacy `thinking`, `thinking_duration`, and `tool_calls_json`
  columns from `messages`.
- Add `blocks` JSONB column to `messages` — an ordered array of
  rendering blocks that preserve the chronological flow of a multi-round
  agent turn (thinking → text → tool_call → thinking → text → ...).

Each block has this shape:
  {"type": "thinking", "content": "...", "duration": 3}
  {"type": "text", "content": "..."}
  {"type": "tool_call", "tool_call": {id, type, status, title, query, webResults, ...}}
  {"type": "error", "content": "..."}

The `generation_duration` column is kept (single int, used for the
response-time badge shown under assistant messages).

This migration is destructive — the old database should be deleted and
recreated from scratch. Run `alembic upgrade head` on a fresh DB.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "e2b7c4f1a93d"
down_revision: Union[str, Sequence[str], None] = "f1a5b3c9d2e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Drop legacy columns
    op.drop_column("messages", "thinking")
    op.drop_column("messages", "thinking_duration")
    op.drop_column("messages", "tool_calls_json")

    # Add blocks JSONB column (nullable — user messages and system messages
    # don't have blocks; only assistant messages do).
    op.add_column(
        "messages",
        sa.Column("blocks", JSONB(astext_type=sa.Text()), nullable=True),
    )

    # GIN index on blocks for future queries (e.g. "find messages with
    # a tool_call of type X"). Not critical now but cheap to add.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_messages_blocks_gin "
        "ON messages USING gin (blocks jsonb_path_ops)"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute("DROP INDEX IF EXISTS ix_messages_blocks_gin")
    op.drop_column("messages", "blocks")
    op.add_column("messages", sa.Column("thinking", sa.Text(), nullable=True))
    op.add_column(
        "messages", sa.Column("thinking_duration", sa.Integer(), nullable=True)
    )
    op.add_column("messages", sa.Column("tool_calls_json", sa.Text(), nullable=True))
