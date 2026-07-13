"""reconcile legacy profile and market depth columns

Revision ID: 20260713_04
Revises: 20260713_03
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260713_04"
down_revision = "20260713_03"
branch_labels = None
depends_on = None


PROFILE_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column(
        "max_spread_pct_for_buy",
        sa.Float(),
        nullable=False,
        server_default="0.50",
    ),
    sa.Column(
        "min_depth_bid_ask_ratio_top10_for_buy",
        sa.Float(),
        nullable=False,
        server_default="0.60",
    ),
    sa.Column(
        "max_depth_sell_pressure_score_for_buy",
        sa.Float(),
        nullable=False,
        server_default="80.0",
    ),
    sa.Column(
        "block_buy_on_strong_sell_pressure",
        sa.Boolean(),
        nullable=False,
        server_default=sa.true(),
    ),
    sa.Column(
        "block_buy_on_near_ask_wall",
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
    ),
    sa.Column(
        "near_wall_distance_pct",
        sa.Float(),
        nullable=False,
        server_default="0.30",
    ),
)

MARKET_SNAPSHOT_COLUMNS: tuple[sa.Column, ...] = (
    sa.Column("spread_pct", sa.Float(), nullable=True),
    sa.Column("bid_ask_ratio_top5", sa.Float(), nullable=True),
    sa.Column("bid_ask_ratio_top10", sa.Float(), nullable=True),
    sa.Column("bid_ask_ratio_top25", sa.Float(), nullable=True),
    sa.Column("imbalance_top10", sa.Float(), nullable=True),
    sa.Column("imbalance_top25", sa.Float(), nullable=True),
    sa.Column("largest_bid_wall_distance_pct", sa.Float(), nullable=True),
    sa.Column("largest_ask_wall_distance_pct", sa.Float(), nullable=True),
    sa.Column("depth_buy_pressure_score", sa.Float(), nullable=True),
    sa.Column("depth_sell_pressure_score", sa.Float(), nullable=True),
    sa.Column("depth_order_book_signal", sa.String(length=32), nullable=True),
    sa.Column("depth_reliable", sa.Boolean(), nullable=True),
)


def _table_columns(table_name: str) -> set[str]:
    inspector = sa.inspect(op.get_bind())
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    profile_columns = _table_columns("trade_profiles")
    if profile_columns:
        for column in PROFILE_COLUMNS:
            if column.name not in profile_columns:
                op.add_column("trade_profiles", column)

    snapshot_columns = _table_columns("market_snapshots")
    if snapshot_columns:
        for column in MARKET_SNAPSHOT_COLUMNS:
            if column.name not in snapshot_columns:
                op.add_column("market_snapshots", column)


def downgrade() -> None:
    snapshot_columns = _table_columns("market_snapshots")
    if snapshot_columns:
        with op.batch_alter_table("market_snapshots") as batch:
            for column in reversed(MARKET_SNAPSHOT_COLUMNS):
                if column.name in snapshot_columns:
                    batch.drop_column(column.name)

    profile_columns = _table_columns("trade_profiles")
    if profile_columns:
        with op.batch_alter_table("trade_profiles") as batch:
            for column in reversed(PROFILE_COLUMNS):
                if column.name in profile_columns:
                    batch.drop_column(column.name)
