"""store precise response duration seconds

Revision ID: b2e5f8a0c3d7
Revises: a1d4e7f9b2c6
"""

import sqlalchemy as sa
from alembic import op

revision = "b2e5f8a0c3d7"
down_revision = "a1d4e7f9b2c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "messages",
        "generation_duration",
        existing_type=sa.Integer(),
        type_=sa.Float(),
        existing_nullable=True,
    )


def downgrade() -> None:
    op.alter_column(
        "messages",
        "generation_duration",
        existing_type=sa.Float(),
        type_=sa.Integer(),
        existing_nullable=True,
        postgresql_using="generation_duration::integer",
    )
