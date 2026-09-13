"""add documents.collections (user-assignable knowledge groups)

Revision ID: e6c4a9d2f8b1
Revises: b5d1e4f7a8c2
Create Date: 2026-09-11 18:30:00.000000

Changes:
- Add `documents.collections` as a JSON list of strings (default '[]').
  Collections are user-assignable groups shown in the Workspace >
  Documents detail modal ("Collections: • Company • Strategy") so the
  knowledge base can be organized beyond public/private scope.
- Kept deliberately simple (a JSON column, not a join table): the
  number of collections per document is small, they are only edited
  from the detail modal, and they never need to be joined against.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "e6c4a9d2f8b1"
down_revision: Union[str, Sequence[str], None] = "b5d1e4f7a8c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Add the collections JSON column to documents."""
    op.add_column(
        "documents",
        sa.Column(
            "collections",
            sa.JSON(),
            nullable=True,
            server_default=sa.text("'[]'::json"),
        ),
    )


def downgrade() -> None:
    """Drop the collections column."""
    op.drop_column("documents", "collections")
