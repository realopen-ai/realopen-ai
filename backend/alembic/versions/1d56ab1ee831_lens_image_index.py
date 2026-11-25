"""Index saved image sources for Lens."""

from alembic import op
import sqlalchemy as sa
from pgvector.sqlalchemy import Vector

revision = "1d56ab1ee831"
down_revision = "c8d2e4f6a901"
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        "ix_messages_lens_image_outputs",
        "messages",
        ["id"],
        postgresql_where=sa.text(
            """role = 'assistant' AND blocks @? '$[*].tool_call.genResults[*] ? (@.type == "image")'"""
        ),
    )
    op.create_table(
        "lens_images",
        sa.Column(
            "id",
            sa.Uuid(),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "document_id",
            sa.Uuid(),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            unique=True,
        ),
        sa.Column(
            "message_id", sa.Uuid(), sa.ForeignKey("messages.id", ondelete="CASCADE")
        ),
        sa.Column("output_index", sa.Integer()),
        sa.Column("thumbnail", sa.LargeBinary()),
        sa.Column("retry_at", sa.DateTime(timezone=True)),
        sa.Column("error", sa.Text()),
        sa.Column("ocr_text", sa.Text()),
        sa.Column("visual_version", sa.String(128)),
        sa.Column("visual_embedding", Vector(768)),
        sa.Column("semantic_version", sa.String(128)),
        sa.UniqueConstraint("message_id", "output_index"),
        sa.CheckConstraint(
            "(document_id IS NOT NULL AND message_id IS NULL AND output_index IS NULL)"
            " OR (document_id IS NULL AND message_id IS NOT NULL AND output_index >= 0"
            " AND output_index IS NOT NULL)",
            name="lens_image_one_source",
        ),
    )
    op.create_table(
        "lens_passages",
        sa.Column(
            "image_id",
            sa.Uuid(),
            sa.ForeignKey("lens_images.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("position", sa.Integer(), primary_key=True),
        sa.Column("embedding", Vector(384), nullable=False),
    )


def downgrade():
    op.drop_table("lens_passages")
    op.drop_table("lens_images")
    op.drop_index("ix_messages_lens_image_outputs", table_name="messages")
