"""Linked learning notebooks with dedicated conversations."""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = "b9d3f7a1c5e8"
down_revision = "a8c2e6f0b4d8"
branch_labels = depends_on = None


def upgrade():
    op.add_column(
        "conversations",
        sa.Column("is_notebook", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_table(
        "notebooks",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column(
            "conversation_id",
            UUID(as_uuid=True),
            sa.ForeignKey("conversations.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    targets = {
        "document_id": "documents",
        "artifact_id": "artifacts",
        "note_id": "study_notes",
        "deck_id": "flashcard_decks",
    }
    op.create_table(
        "notebook_items",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "notebook_id",
            UUID(as_uuid=True),
            sa.ForeignKey("notebooks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        *(
            sa.Column(field, UUID(as_uuid=True), sa.ForeignKey(f"{table}.id", ondelete="CASCADE"))
            for field, table in targets.items()
        ),
        sa.Column("selected", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "("
            + " + ".join(f"CASE WHEN {field} IS NULL THEN 0 ELSE 1 END" for field in targets)
            + ") = 1",
            name="notebook_item_one_target",
        ),
        *(
            sa.UniqueConstraint("notebook_id", field, name=f"uq_notebook_{field}")
            for field in targets
        ),
    )
    op.create_index("ix_notebook_items_notebook_id", "notebook_items", ["notebook_id"])


def downgrade():
    op.drop_table("notebook_items")
    op.drop_table("notebooks")
    op.drop_column("conversations", "is_notebook")
