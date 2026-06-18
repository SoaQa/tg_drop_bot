"""add giveaway title

Revision ID: 0002_add_giveaway_title
Revises: 0001_initial
Create Date: 2026-06-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_add_giveaway_title"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("giveaways", sa.Column("title", sa.String(length=255), nullable=True))
    op.execute(
        """
        UPDATE giveaways
        SET title = left(nullif(trim(post_text), ''), 255)
        WHERE title IS NULL AND post_text IS NOT NULL
        """
    )


def downgrade() -> None:
    op.drop_column("giveaways", "title")
