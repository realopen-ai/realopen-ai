"""Persist sandbox preview commands across compute-container restarts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d4e8a1b7c2f6"
down_revision = "c0e4a8b2d6f0"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "sandboxes",
        sa.Column(
            "preview_commands",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.alter_column("sandboxes", "preview_commands", server_default=None)


def downgrade():
    op.drop_column("sandboxes", "preview_commands")
