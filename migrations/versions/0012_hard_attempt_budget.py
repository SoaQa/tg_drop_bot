"""Track the number of started hard games per player and giveaway."""

import sqlalchemy as sa
from alembic import op

revision = "0012_hard_attempt_budget"
down_revision = "0011_post_text_entities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hard_game_attempts",
        sa.Column("attempts_used", sa.Integer(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("hard_game_attempts", "attempts_used")
