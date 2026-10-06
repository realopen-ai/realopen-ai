"""Persistent flashcard decks, cards, scheduling state and review history."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "c4f6a8b0d2e4"
down_revision = "b2e5f8a0c3d7"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("flashcard_decks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("source_conversation_id", UUID(as_uuid=True), sa.ForeignKey("conversations.id", ondelete="SET NULL")),
        sa.Column("source_document_id", UUID(as_uuid=True), sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_table("flashcards",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("deck_id", UUID(as_uuid=True), sa.ForeignKey("flashcard_decks.id", ondelete="CASCADE"), nullable=False),
        sa.Column("front", sa.Text(), nullable=False), sa.Column("back", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False), sa.Column("source_reference", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False), sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False))
    op.create_index("ix_flashcards_deck_id", "flashcards", ["deck_id"])
    op.create_table("flashcard_progress",
        sa.Column("card_id", UUID(as_uuid=True), sa.ForeignKey("flashcards.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("interval_days", sa.Integer(), nullable=False), sa.Column("ease", sa.Float(), nullable=False),
        sa.Column("repetitions", sa.Integer(), nullable=False), sa.Column("lapses", sa.Integer(), nullable=False),
        sa.Column("reviews", sa.Integer(), nullable=False), sa.Column("last_reviewed_at", sa.DateTime(timezone=True)))
    op.create_index("ix_flashcard_progress_due_at", "flashcard_progress", ["due_at"])
    op.create_table("flashcard_reviews",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("card_id", UUID(as_uuid=True), sa.ForeignKey("flashcards.id", ondelete="CASCADE"), nullable=False),
        sa.Column("rating", sa.String(8), nullable=False), sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False), sa.Column("interval_days", sa.Integer(), nullable=False))
    op.create_index("ix_flashcard_reviews_card_id", "flashcard_reviews", ["card_id"])


def downgrade():
    for table in ("flashcard_reviews", "flashcard_progress", "flashcards", "flashcard_decks"):
        op.drop_table(table)
