"""add streamed message completion status

Revision ID: a1d4e7f9b2c6
Revises: f2c7a4d9e1b3
"""

from alembic import op
import sqlalchemy as sa

revision = "a1d4e7f9b2c6"
down_revision = "f2c7a4d9e1b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "messages",
        sa.Column(
            "completion_status",
            sa.String(length=20),
            nullable=False,
            server_default="completed",
        ),
    )


def downgrade() -> None:
    op.drop_column("messages", "completion_status")
