"""Preserve native Telegram formatting in giveaway descriptions."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011_post_text_entities"
down_revision = "0010_retry_availability"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("giveaways", sa.Column("post_text_entities", JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("giveaways", "post_text_entities")
