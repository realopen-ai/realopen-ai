"""add message modality (voice)

Revision ID: c4e8f2a1b6d3
Revises: f7a9c3d5e8b2
Create Date: 2026-06-26 12:00:00.000000

Changes:
- Add `modality` VARCHAR(20) NULLable column to `messages`.

Semantics:
- NULL (or "text"): the message came from the regular text chat — all
  pre-existing rows stay NULL (no data migration needed).
- "voice": the message was captured from the microphone (user) or spoken
  through the voice pipeline (assistant). Metadata only — voice messages
  are normal messages everywhere else (blocks, search, UI).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c4e8f2a1b6d3"
down_revision: Union[str, Sequence[str], None] = "f7a9c3d5e8b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("messages", sa.Column("modality", sa.String(20), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("messages", "modality")
