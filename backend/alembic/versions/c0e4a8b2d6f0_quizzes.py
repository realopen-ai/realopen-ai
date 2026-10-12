"""Native quizzes, immutable attempt snapshots and notebook links."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "c0e4a8b2d6f0"
down_revision = "b9d3f7a1c5e8"
branch_labels = depends_on = None


def target_check(include_quiz):
    fields = ["document_id", "artifact_id", "note_id", "deck_id"] + (
        ["quiz_id"] if include_quiz else []
    )
    return (
        "("
        + " + ".join(f"CASE WHEN {field} IS NULL THEN 0 ELSE 1 END" for field in fields)
        + ") = 1"
    )


def upgrade():
    op.create_table(
        "quizzes",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("questions", sa.JSON(), nullable=False),
        sa.Column(
            "source_conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "source_note_id",
            UUID(as_uuid=True),
            sa.ForeignKey("study_notes.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_quizzes_updated_at", "quizzes", ["updated_at"])
    op.create_table(
        "quiz_attempts",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "quiz_id",
            UUID(as_uuid=True),
            sa.ForeignKey("quizzes.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("answers", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON()),
        sa.Column(
            "flashcard_deck_id",
            UUID(as_uuid=True),
            sa.ForeignKey("flashcard_decks.id", ondelete="SET NULL"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_quiz_attempts_quiz_id", "quiz_attempts", ["quiz_id"])
    op.add_column(
        "notebook_items",
        sa.Column("quiz_id", UUID(as_uuid=True), sa.ForeignKey("quizzes.id", ondelete="CASCADE")),
    )
    op.create_unique_constraint("uq_notebook_quiz_id", "notebook_items", ["notebook_id", "quiz_id"])
    op.drop_constraint("notebook_item_one_target", "notebook_items", type_="check")
    op.create_check_constraint("notebook_item_one_target", "notebook_items", target_check(True))


def downgrade():
    op.execute(sa.text("DELETE FROM notebook_items WHERE quiz_id IS NOT NULL"))
    op.drop_constraint("notebook_item_one_target", "notebook_items", type_="check")
    op.drop_constraint("uq_notebook_quiz_id", "notebook_items", type_="unique")
    op.drop_column("notebook_items", "quiz_id")
    op.create_check_constraint("notebook_item_one_target", "notebook_items", target_check(False))
    op.drop_table("quiz_attempts")
    op.drop_table("quizzes")
