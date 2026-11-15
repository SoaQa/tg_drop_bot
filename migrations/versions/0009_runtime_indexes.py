"""Index the unfinished inbox and chronological hard-game leaderboard."""

import sqlalchemy as sa
from alembic import op

revision = "0009_runtime_indexes"
down_revision = "0008_hard_game_attempts"
branch_labels = None
depends_on = None


def _create_index(name: str, table: str, columns: list[str], predicate: str) -> None:
    # PostgreSQL can leave an invalid index after an interrupted concurrent build.
    # Repair only this migration's index on its intended table, never another object.
    invalid = False
    if not op.get_context().as_sql:
        invalid = (
            op.get_bind()
            .execute(
                sa.text("""
            SELECT NOT i.indisvalid
            FROM pg_index i
            JOIN pg_class idx ON idx.oid = i.indexrelid
            JOIN pg_class tab ON tab.oid = i.indrelid
            JOIN pg_namespace ns ON ns.oid = idx.relnamespace
            WHERE ns.nspname = current_schema() AND idx.relname = :name AND tab.relname = :table
        """),
                {"name": name, "table": table},
            )
            .scalar()
        )
    if invalid:
        op.drop_index(name, table_name=table, postgresql_concurrently=True, if_exists=True)
    op.create_index(
        name,
        table,
        columns,
        postgresql_where=sa.text(predicate),
        postgresql_concurrently=True,
        if_not_exists=True,
    )


def upgrade() -> None:
    # Keep reads and writes available while indexing historical production rows.
    with op.get_context().autocommit_block():
        _create_index(
            "ix_participation_jobs_unfinished",
            "participation_jobs",
            ["kind", "dispatch_at", "id"],
            "processed_at IS NULL OR reply_text IS NOT NULL",
        )
        _create_index(
            "ix_hard_game_finish",
            "hard_game_attempts",
            ["giveaway_id", "completed_at", "user_id"],
            "completed_at IS NOT NULL",
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_hard_game_finish",
            table_name="hard_game_attempts",
            postgresql_concurrently=True,
            if_exists=True,
        )
        op.drop_index(
            "ix_participation_jobs_unfinished",
            table_name="participation_jobs",
            postgresql_concurrently=True,
            if_exists=True,
        )
