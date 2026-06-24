"""add deliverables JSONB to messages

Revision ID: f3c8d1e5b4a2
Revises: e2b7c4f1a93d
Create Date: 2026-06-24 12:00:00.000000

Changes:
- Add `deliverables` JSONB column to `messages` (nullable). Stores an
  array of deliverable file metadata so generated reports (and future
  deliverable types) survive page refresh.

Each deliverable has this shape:
  {"type": "report", "format": "pdf"|"docx"|"pptx", "filename": "...",
   "file_path": "reports/{id}.pdf", "download_url": "/api/reports/{id}/download",
   "created_at": 1234567890}
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "f3c8d1e5b4a2"
down_revision: Union[str, Sequence[str], None] = "e2b7c4f1a93d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "messages",
        sa.Column("deliverables", JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("messages", "deliverables")
