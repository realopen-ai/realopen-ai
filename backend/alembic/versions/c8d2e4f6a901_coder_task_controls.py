"""Persist coder task monitoring and continuation controls."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "c8d2e4f6a901"
down_revision = "e6a2c9f4b810"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "sandbox_tasks",
        sa.Column(
            "stop_after_round", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "sandbox_tasks",
        sa.Column(
            "progress",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "sandbox_tasks",
        sa.Column("resumed_from", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_sandbox_task_resumed_from",
        "sandbox_tasks",
        "sandbox_tasks",
        ["resumed_from"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint(
        "fk_sandbox_task_resumed_from", "sandbox_tasks", type_="foreignkey"
    )
    for column in ("resumed_from", "progress", "stop_after_round"):
        op.drop_column("sandbox_tasks", column)
