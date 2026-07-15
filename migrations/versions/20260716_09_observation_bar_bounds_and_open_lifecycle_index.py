"""market_observation bar bounds + NOT NULL dedup sentinels, and the
hard-failing partial unique open-lifecycle index (Fix 1/4/7/9)

Revision ID: 20260716_09
Revises: 20260715_08
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260716_09"
down_revision = "20260715_08"
branch_labels = None
depends_on = None


class DuplicateOpenLifecycleError(RuntimeError):
    """Raised to abort the migration (rather than silently skipping the
    index) when more than one OPEN position_lifecycle exists for a symbol."""


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    is_sqlite = bind.dialect.name == "sqlite"

    # ── Fix 4/7: market_observations bar bounds + NOT NULL sentinels ────────
    if "market_observations" in tables:
        columns = {c["name"] for c in inspector.get_columns("market_observations")}
        if "bar_start_at" not in columns:
            op.add_column(
                "market_observations", sa.Column("bar_start_at", sa.DateTime(timezone=True))
            )
        if "bar_end_at" not in columns:
            op.add_column(
                "market_observations", sa.Column("bar_end_at", sa.DateTime(timezone=True))
            )

        # Backfill NULL period/source to the explicit sentinels *before*
        # enforcing NOT NULL, so no existing row is deleted or invalidated.
        bind.execute(
            sa.text(
                "UPDATE market_observations SET bar_period = 'UNKNOWN_PERIOD' "
                "WHERE bar_period IS NULL"
            )
        )
        bind.execute(
            sa.text(
                "UPDATE market_observations SET price_source = 'UNKNOWN_SOURCE' "
                "WHERE price_source IS NULL"
            )
        )
        # SQLite cannot ALTER a column to NOT NULL in place; batch_alter_table
        # rebuilds the table. On PostgreSQL a plain ALTER is used. The unique
        # constraint already covers these columns (from 20260715_08), so once
        # they are non-NULL the dedup key is fully effective.
        with op.batch_alter_table("market_observations") as batch:
            batch.alter_column(
                "bar_period",
                existing_type=sa.String(16),
                nullable=False,
                server_default="UNKNOWN_PERIOD",
            )
            batch.alter_column(
                "price_source",
                existing_type=sa.String(32),
                nullable=False,
                server_default="UNKNOWN_SOURCE",
            )

    # ── Fix 1: hard-failing partial unique open-lifecycle index ─────────────
    if "position_lifecycles" in tables:
        existing_indexes = {
            idx["name"] for idx in inspector.get_indexes("position_lifecycles")
        }
        if "uq_position_lifecycle_open_symbol" not in existing_indexes:
            duplicate_rows = bind.execute(
                sa.text(
                    "SELECT symbol, COUNT(*) AS c FROM position_lifecycles "
                    "WHERE status = 'OPEN' GROUP BY symbol HAVING COUNT(*) > 1"
                )
            ).fetchall()
            if duplicate_rows:
                duplicate_symbols = [row[0] for row in duplicate_rows]
                # Do NOT create the index and do NOT report success: abort so
                # the operator resolves the duplicates and re-runs. The rows
                # are also flagged MANUAL_REVIEW for visibility, but the
                # migration still fails hard (Fix 1).
                for symbol in duplicate_symbols:
                    bind.execute(
                        sa.text(
                            "UPDATE position_lifecycles SET data_quality = "
                            "'MANUAL_REVIEW' WHERE status = 'OPEN' AND symbol = :symbol"
                        ),
                        {"symbol": symbol},
                    )
                raise DuplicateOpenLifecycleError(
                    "Cannot create uq_position_lifecycle_open_symbol: "
                    f"{len(duplicate_symbols)} symbol(s) have more than one OPEN "
                    "position_lifecycle. They have been marked MANUAL_REVIEW. "
                    "Resolve the duplicates and re-run this migration: "
                    f"{duplicate_symbols}"
                )
            op.create_index(
                "uq_position_lifecycle_open_symbol",
                "position_lifecycles",
                ["symbol"],
                unique=True,
                postgresql_where=sa.text("status = 'OPEN'"),
                sqlite_where=sa.text("status = 'OPEN'"),
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "position_lifecycles" in tables:
        existing_indexes = {
            idx["name"] for idx in inspector.get_indexes("position_lifecycles")
        }
        if "uq_position_lifecycle_open_symbol" in existing_indexes:
            op.drop_index(
                "uq_position_lifecycle_open_symbol", table_name="position_lifecycles"
            )

    if "market_observations" in tables:
        columns = {c["name"] for c in inspector.get_columns("market_observations")}
        # Revert NOT NULL so the column definitions match the 20260715_08 state.
        with op.batch_alter_table("market_observations") as batch:
            batch.alter_column(
                "price_source", existing_type=sa.String(32), nullable=True
            )
            batch.alter_column(
                "bar_period", existing_type=sa.String(16), nullable=True
            )
        if "bar_end_at" in columns:
            with op.batch_alter_table("market_observations") as batch:
                batch.drop_column("bar_end_at")
        if "bar_start_at" in columns:
            with op.batch_alter_table("market_observations") as batch:
                batch.drop_column("bar_start_at")
