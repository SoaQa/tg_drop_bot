"""Membership history and channel-local abuse exclusions."""

import sqlalchemy as sa
from alembic import op

revision = "0005_membership"
down_revision = "0004_add_results_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "giveaways",
        sa.Column("anti_abuse_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("giveaways", sa.Column("results_announced_at", sa.DateTime(timezone=True)))
    op.execute("""UPDATE giveaways g SET results_announced_at = COALESCE(
        (SELECT min(a.created_at) FROM audit_log a WHERE a.giveaway_id=g.id
         AND a.action IN ('giveaway.results.announced', 'results.announced')), g.finished_at)
        WHERE g.status='finished' AND g.results_state='announced'""")
    for column in [
        sa.Column("selection_weight", sa.Float(), nullable=False, server_default="1"),
        sa.Column("selection_reason", sa.String(64), nullable=False, server_default="unknown"),
        sa.Column("draw_excluded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("exclusion_reason", sa.Text()),
        sa.Column("membership_next_check_at", sa.DateTime(timezone=True)),
    ]:
        op.add_column("participants", column)
    op.create_index(
        "ix_participants_membership_next_check_at", "participants", ["membership_next_check_at"]
    )
    op.create_table(
        "membership_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("update_id", sa.BigInteger(), nullable=False, unique=True),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("known_channels.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index(
        "ix_membership_events_history",
        "membership_events",
        ["channel_id", "user_id", "occurred_at"],
    )
    op.create_index("ix_membership_events_occurred_at", "membership_events", ["occurred_at"])
    op.create_table(
        "channel_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("known_channels.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_event_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_update_id", sa.BigInteger(), nullable=False),
        sa.UniqueConstraint("channel_id", "user_id", name="uq_channel_membership_user"),
    )
    op.create_table(
        "channel_exclusions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("known_channels.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("evidence_event_id", sa.Integer(), sa.ForeignKey("membership_events.id")),
        sa.UniqueConstraint("channel_id", "user_id", name="uq_channel_exclusion_user"),
    )
    op.create_table(
        "channel_exclusion_periods",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("channel_id", sa.Integer(), sa.ForeignKey("known_channels.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_exclusion_period_history",
        "channel_exclusion_periods",
        ["channel_id", "user_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_table("channel_exclusion_periods")
    op.drop_table("channel_exclusions")
    op.drop_table("channel_memberships")
    op.drop_table("membership_events")
    for name in (
        "membership_next_check_at",
        "exclusion_reason",
        "draw_excluded",
        "selection_reason",
        "selection_weight",
    ):
        op.drop_column("participants", name)
    op.drop_column("giveaways", "results_announced_at")
    op.drop_column("giveaways", "anti_abuse_enabled")
