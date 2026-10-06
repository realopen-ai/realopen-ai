"""Preserve document page/chunk provenance without copying source content."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "d5a7b9c1e3f5"
down_revision = "c4f6a8b0d2e4"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("flashcards", sa.Column("source_page", sa.Integer(), nullable=True))
    op.add_column("flashcards", sa.Column("source_chunk_id", UUID(as_uuid=True), nullable=True))
    op.create_foreign_key(
        "fk_flashcard_source_chunk",
        "flashcards",
        "document_chunks",
        ["source_chunk_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade():
    op.drop_constraint("fk_flashcard_source_chunk", "flashcards", type_="foreignkey")
    op.drop_column("flashcards", "source_chunk_id")
    op.drop_column("flashcards", "source_page")
