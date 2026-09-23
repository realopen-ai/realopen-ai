"""add persistent sandboxes

Revision ID: d9a1e7b4c2f6
Revises: c4e8f2a1b6d3
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d9a1e7b4c2f6"
down_revision = "c4e8f2a1b6d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sandboxes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="stopped"),
        sa.Column(
            "desired_running", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("volume_name", sa.String(180), nullable=False, unique=True),
        sa.Column("container_name", sa.String(180), nullable=False, unique=True),
        sa.Column(
            "image",
            sa.String(255),
            nullable=False,
            server_default="realopenai-sandbox:latest",
        ),
        sa.Column("cpu_limit", sa.Float(), nullable=False, server_default="2"),
        sa.Column(
            "memory_limit_mb", sa.Integer(), nullable=False, server_default="2048"
        ),
        sa.Column(
            "workspace_quota_bytes",
            sa.BigInteger(),
            nullable=False,
            server_default=str(2 * 1024**3),
        ),
        sa.Column("usage_bytes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column(
            "idle_timeout_seconds", sa.Integer(), nullable=False, server_default="1800"
        ),
        sa.Column("last_active_at", sa.DateTime(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "conversations",
        sa.Column("sandbox_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversations_sandbox",
        "conversations",
        "sandboxes",
        ["sandbox_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_conversations_sandbox_id", "conversations", ["sandbox_id"])
    op.create_table(
        "sandbox_tasks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "sandbox_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sandboxes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "conversation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("status", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("request", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=True),
        sa.Column("worklog", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "files_changed", postgresql.JSONB(), nullable=False, server_default="[]"
        ),
        sa.Column("tests_run", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "cancellation_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_sandbox_tasks_sandbox_status", "sandbox_tasks", ["sandbox_id", "status"]
    )


def downgrade() -> None:
    op.drop_index("ix_sandbox_tasks_sandbox_status", table_name="sandbox_tasks")
    op.drop_table("sandbox_tasks")
    op.drop_index("ix_conversations_sandbox_id", table_name="conversations")
    op.drop_constraint("fk_conversations_sandbox", "conversations", type_="foreignkey")
    op.drop_column("conversations", "sandbox_id")
    op.drop_table("sandboxes")
