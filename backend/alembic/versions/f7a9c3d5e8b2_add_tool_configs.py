"""add tool_configs table (Brain > Tools configuration)

Revision ID: f7a9c3d5e8b2
Revises: e6c4a9d2f8b1
Create Date: 2026-09-13 10:00:00.000000

Changes:
- New `tool_configs` table: one row per discovered agent tool, keyed by
  the stable tool identifier (BaseTool.name, e.g. "use_websearch").
- `config` is a JSONB blob holding the universal settings
  (enabled / always_load / keyword_gate / model override) plus any
  tool-specific custom settings (e.g. the Web Search provider matrix).
- Rows are seeded on startup from the tool configuration definitions
  under app/agent/tools/ (config_base) when missing; existing rows are
  never overwritten when defaults change (the DB is the source of truth
  after initialization).
- Secrets never live in this table — provider API keys are stored via
  app/services/secrets.py (file store under the state dir).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers, used by Alembic.
revision: str = "f7a9c3d5e8b2"
down_revision: Union[str, Sequence[str], None] = "e6c4a9d2f8b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create the tool_configs table."""
    op.create_table(
        "tool_configs",
        sa.Column("tool_name", sa.String(length=128), primary_key=True),
        sa.Column(
            "config",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    """Drop the tool_configs table."""
    op.drop_table("tool_configs")
