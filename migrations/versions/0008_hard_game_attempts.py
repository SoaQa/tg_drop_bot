"""One hard challenge per user and giveaway, with verified completions."""
import sqlalchemy as sa
from alembic import op

revision = "0008_hard_game_attempts"
down_revision = "0007_captcha_response_time"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hard_game_attempts",
        sa.Column("giveaway_id", sa.Integer(),
                  sa.ForeignKey("giveaways.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("user_id", sa.BigInteger(), primary_key=True),
        sa.Column("attempt_key", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
    )
    op.create_index("ix_hard_game_top", "hard_game_attempts",
                    ["giveaway_id", "duration_ms", "user_id"])


def downgrade() -> None:
    op.drop_table("hard_game_attempts")
