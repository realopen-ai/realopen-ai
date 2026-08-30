"""add pinned/archived flags to conversations

Revision ID: b5d1e4f7a8c2
Revises: a4f9c2e7b3d1
Create Date: 2026-06-27 10:00:00.000000

Changes:
- Add `pinned` / `pinned_at` to conversations — pinned conversations float
  to the top of the sidebar (most recently pinned first).
- Add `archived` / `archived_at` to conversations — archived conversations
  are hidden from the main sidebar list and shown under a collapsible
  "Archived" section.
- Both default to false; existing rows are unaffected.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "b5d1e4f7a8c2"
down_revision: Union[str, Sequence[str], None] = "a4f9c2e7b3d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column(
            "pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column("conversations", sa.Column("pinned_at", sa.DateTime(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column(
            "archived", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "conversations", sa.Column("archived_at", sa.DateTime(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("conversations", "archived_at")
    op.drop_column("conversations", "archived")
    op.drop_column("conversations", "pinned_at")
    op.drop_column("conversations", "pinned")
