"""Mark the first claim against a reopened hard-attempt budget."""

import sqlalchemy as sa
from alembic import op

revision = "0013_hard_transition_budget"
down_revision = "0012_hard_attempt_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "hard_game_attempts",
        sa.Column("transition_budget_applied", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("hard_game_attempts", "transition_budget_applied")
