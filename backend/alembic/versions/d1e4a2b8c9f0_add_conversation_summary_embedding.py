"""Add conversation summary and summary_embedding for cross-session context memory

Revision ID: d1e4a2b8c9f0
Revises: c7e2f9a01b3a
Create Date: 2025-06-21

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

# revision identifiers, used by Alembic.
revision: str = "d1e4a2b8c9f0"
down_revision: Union[str, None] = "c7e2f9a01b3a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("conversations", sa.Column("summary", sa.Text(), nullable=True))
    op.add_column(
        "conversations",
        sa.Column("summary_embedding", Vector(768), nullable=True),
    )
    op.add_column(
        "conversations", sa.Column("summary_at", sa.DateTime(), nullable=True)
    )
    # Index for retrieval ordering
    op.create_index(
        "ix_conversations_summary_at",
        "conversations",
        ["summary_at"],
        postgresql_where=sa.text("summary IS NOT NULL AND summary != ''"),
    )


def downgrade() -> None:
    op.drop_index("ix_conversations_summary_at", table_name="conversations")
    op.drop_column("conversations", "summary_at")
    op.drop_column("conversations", "summary_embedding")
    op.drop_column("conversations", "summary")
