"""Index images extracted from documents."""

from alembic import op
import sqlalchemy as sa

revision = "abc690ab4e21"
down_revision = "1d56ab1ee831"
branch_labels = None
depends_on = None

OLD_CHECK = (
    "(document_id IS NOT NULL AND message_id IS NULL AND output_index IS NULL)"
    " OR (document_id IS NULL AND message_id IS NOT NULL AND output_index >= 0"
    " AND output_index IS NOT NULL)"
)
SOURCE_CHECK = (
    "(document_id IS NOT NULL AND document_chunk_id IS NULL AND message_id IS NULL AND output_index IS NULL)"
    " OR (document_id IS NULL AND document_chunk_id IS NOT NULL AND message_id IS NULL AND output_index IS NULL)"
    " OR (document_id IS NULL AND document_chunk_id IS NULL AND message_id IS NOT NULL"
    " AND output_index IS NOT NULL AND output_index >= 0)"
)


def upgrade():
    op.add_column("lens_images", sa.Column("document_chunk_id", sa.Uuid()))
    op.create_foreign_key(
        "fk_lens_images_document_chunk",
        "lens_images",
        "document_chunks",
        ["document_chunk_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_lens_images_document_chunk", "lens_images", ["document_chunk_id"]
    )
    op.drop_constraint("lens_image_one_source", "lens_images", type_="check")
    op.create_check_constraint("lens_image_one_source", "lens_images", SOURCE_CHECK)
    op.create_index(
        "ix_document_chunks_lens_images",
        "document_chunks",
        ["id"],
        postgresql_where=sa.text(
            "chunk_type = 'image_description' AND image_path IS NOT NULL"
        ),
    )


def downgrade():
    op.drop_index("ix_document_chunks_lens_images", table_name="document_chunks")
    op.execute(sa.text("DELETE FROM lens_images WHERE document_chunk_id IS NOT NULL"))
    op.drop_constraint("lens_image_one_source", "lens_images", type_="check")
    op.drop_constraint("uq_lens_images_document_chunk", "lens_images", type_="unique")
    op.drop_constraint(
        "fk_lens_images_document_chunk", "lens_images", type_="foreignkey"
    )
    op.drop_column("lens_images", "document_chunk_id")
    op.create_check_constraint("lens_image_one_source", "lens_images", OLD_CHECK)
