"""Separate delayed retries from active dispatch leases."""
from alembic import op

revision = "0010_retry_availability"
down_revision = "0009_runtime_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE participation_jobs ADD COLUMN IF NOT EXISTS available_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE participation_jobs DROP COLUMN IF EXISTS available_at")
