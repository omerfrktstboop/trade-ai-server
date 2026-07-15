"""drop legacy watchlist_symbols mirror table

Revision ID: 20260715_06
Revises: 20260714_05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260715_06"
down_revision = "20260714_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No migration ever created this table (it was ORM-metadata-only), so it
    # may not exist in every environment. research_candidates/
    # trade_watchlist_symbols (20260714_05) fully replace it; nothing reads
    # or writes watchlist_symbols anymore.
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "watchlist_symbols" in inspector.get_table_names():
        op.drop_table("watchlist_symbols")


def downgrade() -> None:
    op.create_table(
        "watchlist_symbols",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("source", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=512), nullable=True),
        sa.Column("change_pct", sa.Float(), nullable=True),
        sa.Column("volume", sa.Float(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("symbol", name="uq_watchlist_symbols_symbol"),
    )
    op.create_index("ix_watchlist_symbols_symbol", "watchlist_symbols", ["symbol"])
