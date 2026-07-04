"""add giveaway results state

Revision ID: 0004_add_results_state
Revises: 0003_rename_groups_to_channels
Create Date: 2026-07-04
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_add_results_state"
down_revision: str | None = "0003_rename_groups_to_channels"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "giveaways",
        sa.Column(
            "results_state",
            sa.String(length=32),
            nullable=False,
            server_default="announced",
        ),
    )


def downgrade() -> None:
    op.drop_column("giveaways", "results_state")
