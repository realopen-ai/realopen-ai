"""Versioned generated and uploaded file artifacts."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "a8c2e6f0b4d8"
down_revision = "e6b8c0d2f4a6"
branch_labels = depends_on = None


def upgrade():
    for table in ("study_notes", "flashcard_decks", "flashcards"):
        op.add_column(table, sa.Column("source_artifact", sa.JSON(), nullable=True))
    op.create_table(
        "artifacts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
        ),
        sa.Column(
            "document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            unique=True,
        ),
        sa.Column("current_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_artifacts_conversation_id", "artifacts", ["conversation_id"])
    op.create_table(
        "artifact_versions",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "artifact_id",
            UUID(as_uuid=True),
            sa.ForeignKey("artifacts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("sections", sa.JSON(), nullable=False),
        sa.Column("outputs", sa.JSON(), nullable=False),
        sa.Column("restored_from", sa.Integer()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("artifact_id", "number"),
    )
    op.create_index("ix_artifact_versions_artifact_id", "artifact_versions", ["artifact_id"])


def downgrade():
    op.drop_table("artifact_versions")
    op.drop_table("artifacts")
    for table in ("study_notes", "flashcard_decks", "flashcards"):
        op.drop_column(table, "source_artifact")
