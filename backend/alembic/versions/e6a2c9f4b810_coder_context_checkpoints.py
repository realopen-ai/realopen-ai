"""Persist coder continuation notes and bounded source context.

Revision ID: e6a2c9f4b810
Revises: d4e8a1b7c2f6
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "e6a2c9f4b810"
down_revision = "d4e8a1b7c2f6"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sandbox_tasks",
        sa.Column(
            "context_state",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )


def downgrade():
    op.drop_column("sandbox_tasks", "context_state")
