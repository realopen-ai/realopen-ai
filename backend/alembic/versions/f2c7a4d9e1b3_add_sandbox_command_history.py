"""add sandbox command history

Revision ID: f2c7a4d9e1b3
Revises: d9a1e7b4c2f6
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f2c7a4d9e1b3"
down_revision = "d9a1e7b4c2f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sandbox_commands",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("sandbox_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sandboxes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("sandbox_tasks.id", ondelete="CASCADE"), nullable=True),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("tool_name", sa.String(80), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=True),
        sa.Column("command", sa.Text(), nullable=False),
        sa.Column("cwd", sa.String(1024), nullable=False, server_default="/workspace"),
        sa.Column("stdout", sa.Text(), nullable=False, server_default=""),
        sa.Column("stderr", sa.Text(), nullable=False, server_default=""),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("output_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
    )
    op.create_index("ix_sandbox_commands_sandbox_started", "sandbox_commands", ["sandbox_id", "started_at"])
    op.create_index("ix_sandbox_commands_task_sequence", "sandbox_commands", ["task_id", "sequence"])


def downgrade() -> None:
    op.drop_index("ix_sandbox_commands_task_sequence", table_name="sandbox_commands")
    op.drop_index("ix_sandbox_commands_sandbox_started", table_name="sandbox_commands")
    op.drop_table("sandbox_commands")
