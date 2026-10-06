"""Native study notes and note-backed flashcard decks."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "e6b8c0d2f4a6"
down_revision = "d5a7b9c1e3f5"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "study_notes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "source_conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "source_document_id",
            UUID(as_uuid=True),
            sa.ForeignKey("documents.id", ondelete="SET NULL"),
        ),
        sa.Column("source_page", sa.Integer()),
        sa.Column(
            "source_chunk_id",
            UUID(as_uuid=True),
            sa.ForeignKey("document_chunks.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_study_notes_updated_at", "study_notes", ["updated_at"])
    op.add_column("flashcard_decks", sa.Column("source_note_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_flashcard_source_note",
        "flashcard_decks",
        "study_notes",
        ["source_note_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint("fk_flashcard_source_note", "flashcard_decks", type_="foreignkey")
    op.drop_column("flashcard_decks", "source_note_id")
    op.drop_table("study_notes")
