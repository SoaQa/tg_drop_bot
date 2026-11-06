"""Durable participation inbox and giveaway admission cutoff."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006_participation_queue"
down_revision = "0005_membership"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("intake_closed_at", "drain_alerted_at"):
        op.add_column("giveaways", sa.Column(name, sa.DateTime(timezone=True)))
    op.create_table(
        "participation_flows",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "giveaway_id",
            sa.Integer(),
            sa.ForeignKey("giveaways.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("user_profile", JSONB(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("challenge_id", sa.Integer()),
        sa.Column("answer_hash", sa.String(64)),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("cooldown_until", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("giveaway_id", "user_id", name="uq_participation_flow_user"),
    )
    op.create_index("ix_participation_flows_user_id", "participation_flows", ["user_id"])
    op.create_table(
        "participation_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("update_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column(
            "flow_id",
            sa.Integer(),
            sa.ForeignKey("participation_flows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dispatch_at", sa.DateTime(timezone=True)),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("answer_hash", sa.String(64)),
        sa.Column("challenge_id", sa.Integer()),
        sa.Column("reply_text", sa.Text()),
        sa.Column("failures", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index("ix_participation_jobs_flow_id", "participation_jobs", ["flow_id", "id"])
    op.create_index(
        "ix_participation_jobs_dispatch_at", "participation_jobs", ["dispatch_at", "id"]
    )


def downgrade() -> None:
    op.drop_table("participation_jobs")
    op.drop_table("participation_flows")
    op.drop_column("giveaways", "drain_alerted_at")
    op.drop_column("giveaways", "intake_closed_at")
