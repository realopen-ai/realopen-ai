"""add template table for workspace

Revision ID: a4f9c2e7b3d1
Revises: f3c8d1e5b4a2
Create Date: 2026-06-26 14:00:00.000000

Changes:
- Add `templates` table for PPTX template management.
  Fields: id, display_name, slug, description, tags (JSONB), thumbnail
  (Text, base64), path (relative path to .pptx file), created_at,
  updated_at.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID, JSONB

revision: str = "a4f9c2e7b3d1"
down_revision: Union[str, Sequence[str], None] = "f3c8d1e5b4a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "templates",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column("slug", sa.String(255), nullable=False, unique=True),
        sa.Column("description", sa.Text, nullable=True),
        sa.Column("tags", JSONB, nullable=True),
        sa.Column("thumbnail", sa.Text, nullable=True),
        sa.Column("path", sa.String(1024), nullable=False),
        sa.Column("created_at", sa.DateTime, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime, server_default=sa.text("now()")),
    )
    op.create_index("ix_templates_slug", "templates", ["slug"], unique=True)


def downgrade() -> None:
    op.drop_index("ix_templates_slug", table_name="templates")
    op.drop_table("templates")
