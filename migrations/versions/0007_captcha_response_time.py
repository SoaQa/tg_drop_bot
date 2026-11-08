"""Track captcha response times without retaining solved challenges."""

import sqlalchemy as sa
from alembic import op

revision = "0007_captcha_response_time"
down_revision = "0006_participation_queue"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("captcha_challenges", "participation_flows"):
        op.add_column(table, sa.Column("captcha_sent_at", sa.DateTime(timezone=True)))
    op.add_column("participants", sa.Column("captcha_response_ms", sa.Integer()))


def downgrade() -> None:
    op.drop_column("participants", "captcha_response_ms")
    for table in ("participation_flows", "captcha_challenges"):
        op.drop_column(table, "captcha_sent_at")
