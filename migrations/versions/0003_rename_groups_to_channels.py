"""rename groups to channels

Revision ID: 0003_rename_groups_to_channels
Revises: 0002_add_giveaway_title
Create Date: 2026-06-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_rename_groups_to_channels"
down_revision: str | None = "0002_add_giveaway_title"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.rename_table("known_groups", "known_channels")
    op.alter_column(
        "giveaways",
        "group_id",
        new_column_name="channel_id",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )
    op.execute(
        "ALTER INDEX IF EXISTS ix_known_groups_telegram_chat_id "
        "RENAME TO ix_known_channels_telegram_chat_id"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'known_groups_telegram_chat_id_key'
            ) THEN
                ALTER TABLE known_channels
                RENAME CONSTRAINT known_groups_telegram_chat_id_key
                TO known_channels_telegram_chat_id_key;
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'giveaways_group_id_fkey'
            ) THEN
                ALTER TABLE giveaways
                RENAME CONSTRAINT giveaways_group_id_fkey
                TO giveaways_channel_id_fkey;
            END IF;
        END $$;
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'giveaways_channel_id_fkey'
            ) THEN
                ALTER TABLE giveaways
                RENAME CONSTRAINT giveaways_channel_id_fkey
                TO giveaways_group_id_fkey;
            END IF;
            IF EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'known_channels_telegram_chat_id_key'
            ) THEN
                ALTER TABLE known_channels
                RENAME CONSTRAINT known_channels_telegram_chat_id_key
                TO known_groups_telegram_chat_id_key;
            END IF;
        END $$;
        """
    )
    op.execute(
        "ALTER INDEX IF EXISTS ix_known_channels_telegram_chat_id "
        "RENAME TO ix_known_groups_telegram_chat_id"
    )
    op.alter_column(
        "giveaways",
        "channel_id",
        new_column_name="group_id",
        existing_type=sa.Integer(),
        existing_nullable=True,
    )
    op.rename_table("known_channels", "known_groups")
