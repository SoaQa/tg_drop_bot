"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-06-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "known_groups",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("telegram_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("bot_is_admin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("telegram_chat_id"),
    )
    op.create_index("ix_known_groups_telegram_chat_id", "known_groups", ["telegram_chat_id"])

    op.create_table(
        "giveaways",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column("creator_user_id", sa.BigInteger(), nullable=False),
        sa.Column("group_id", sa.Integer(), sa.ForeignKey("known_groups.id"), nullable=True),
        sa.Column("post_text", sa.Text(), nullable=True),
        sa.Column("terms_text", sa.Text(), nullable=True),
        sa.Column("image_file_id", sa.String(length=512), nullable=True),
        sa.Column("winners_count", sa.Integer(), nullable=True),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("result_message_id", sa.BigInteger(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_giveaways_status", "giveaways", ["status"])
    op.create_index("ix_giveaways_creator_user_id", "giveaways", ["creator_user_id"])

    op.create_table(
        "participants",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("giveaway_id", sa.Integer(), sa.ForeignKey("giveaways.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=True),
        sa.Column("first_name", sa.String(length=255), nullable=True),
        sa.Column("last_name", sa.String(length=255), nullable=True),
        sa.Column("joined_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("captcha_passed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "membership_status", sa.String(length=32), nullable=False, server_default="member"
        ),
        sa.Column("membership_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("giveaway_id", "user_id", name="uq_participants_giveaway_user"),
    )
    op.create_index("ix_participants_giveaway_id", "participants", ["giveaway_id"])
    op.create_index("ix_participants_user_id", "participants", ["user_id"])

    op.create_table(
        "winners",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("giveaway_id", sa.Integer(), sa.ForeignKey("giveaways.id"), nullable=False),
        sa.Column("participant_id", sa.Integer(), sa.ForeignKey("participants.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("giveaway_id", "participant_id", name="uq_winners_participant"),
    )
    op.create_index("ix_winners_giveaway_id", "winners", ["giveaway_id"])
    op.create_index("ix_winners_user_id", "winners", ["user_id"])

    op.create_table(
        "captcha_challenges",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("giveaway_id", sa.Integer(), sa.ForeignKey("giveaways.id"), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("answer_hash", sa.String(length=128), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="pending"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cooldown_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("solved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_captcha_challenges_giveaway_id", "captcha_challenges", ["giveaway_id"])
    op.create_index("ix_captcha_challenges_user_id", "captcha_challenges", ["user_id"])
    op.create_index("ix_captcha_challenges_status", "captcha_challenges", ["status"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("giveaway_id", sa.Integer(), sa.ForeignKey("giveaways.id"), nullable=True),
        sa.Column("actor_user_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_audit_log_giveaway_id", "audit_log", ["giveaway_id"])
    op.create_index("ix_audit_log_actor_user_id", "audit_log", ["actor_user_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_log_actor_user_id", table_name="audit_log")
    op.drop_index("ix_audit_log_giveaway_id", table_name="audit_log")
    op.drop_table("audit_log")
    op.drop_index("ix_captcha_challenges_status", table_name="captcha_challenges")
    op.drop_index("ix_captcha_challenges_user_id", table_name="captcha_challenges")
    op.drop_index("ix_captcha_challenges_giveaway_id", table_name="captcha_challenges")
    op.drop_table("captcha_challenges")
    op.drop_index("ix_winners_user_id", table_name="winners")
    op.drop_index("ix_winners_giveaway_id", table_name="winners")
    op.drop_table("winners")
    op.drop_index("ix_participants_user_id", table_name="participants")
    op.drop_index("ix_participants_giveaway_id", table_name="participants")
    op.drop_table("participants")
    op.drop_index("ix_giveaways_creator_user_id", table_name="giveaways")
    op.drop_index("ix_giveaways_status", table_name="giveaways")
    op.drop_table("giveaways")
    op.drop_index("ix_known_groups_telegram_chat_id", table_name="known_groups")
    op.drop_table("known_groups")
