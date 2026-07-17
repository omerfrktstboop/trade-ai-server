"""separate research candidates from trade-eligible symbols

Revision ID: 20260714_05
Revises: 20260713_04
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260714_05"
down_revision = "20260713_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "research_candidates" not in tables:
        _create_research_candidates()
    if "research_candidate_events" not in tables:
        _create_research_candidate_events()
    if "trade_watchlist_symbols" not in tables:
        _create_trade_watchlist_symbols()

    # Existing discovery rows are research-only after the split. They are not
    # copied into the trade watchlist, so migration cannot accidentally grant BUY.
    # Symbols already present in research_candidates (e.g. this table was
    # created out-of-band before this revision ran) are skipped to avoid a
    # uq_research_candidates_symbol violation.
    if "watchlist_symbols" in tables:
        existing_symbols = {
            row[0]
            for row in bind.execute(sa.text("SELECT symbol FROM research_candidates")).fetchall()
        }
        legacy = sa.table(
            "watchlist_symbols",
            sa.column("symbol"),
            sa.column("source"),
            sa.column("reason"),
            sa.column("change_pct"),
            sa.column("volume"),
            sa.column("is_active"),
            sa.column("added_at"),
            sa.column("last_seen_at"),
        )
        target = sa.table(
            "research_candidates",
            sa.column("symbol"),
            sa.column("status"),
            sa.column("source", sa.JSON()),
            sa.column("trend_pre_score"),
            sa.column("change_pct_daily"),
            sa.column("volume_tl"),
            sa.column("technical_summary", sa.JSON()),
            sa.column("first_detected_at"),
            sa.column("last_detected_at"),
            sa.column("consecutive_pass_count"),
        )
        rows = bind.execute(
            sa.select(legacy).where(legacy.c.is_active.is_(True))
        ).mappings()
        for row in rows:
            symbol = str(row["symbol"]).upper()
            if symbol in existing_symbols:
                continue
            bind.execute(
                target.insert().values(
                    symbol=symbol,
                    status="RESEARCH_PENDING",
                    source=[str(row["source"])],
                    trend_pre_score=0,
                    change_pct_daily=row["change_pct"],
                    volume_tl=row["volume"],
                    technical_summary={"legacyReason": row["reason"]},
                    first_detected_at=row["added_at"],
                    last_detected_at=row["last_seen_at"],
                    consecutive_pass_count=0,
                )
            )
            existing_symbols.add(symbol)


def _create_research_candidates() -> None:
    op.create_table(
        "research_candidates",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column(
            "status", sa.String(length=24), nullable=False, server_default="DETECTED"
        ),
        sa.Column("source", sa.JSON(), nullable=False),
        sa.Column("trend_pre_score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("change_pct_daily", sa.Float(), nullable=True),
        sa.Column("change_pct_30m", sa.Float(), nullable=True),
        sa.Column("change_pct_60m", sa.Float(), nullable=True),
        sa.Column("volume_tl", sa.Float(), nullable=True),
        sa.Column("relative_volume", sa.Float(), nullable=True),
        sa.Column("technical_summary", sa.JSON(), nullable=True),
        sa.Column("ai_action", sa.String(length=10), nullable=True),
        sa.Column("ai_research_score", sa.Float(), nullable=True),
        sa.Column("ai_confidence_score", sa.Float(), nullable=True),
        sa.Column("ai_risk_score", sa.Float(), nullable=True),
        sa.Column("ai_reason", sa.Text(), nullable=True),
        sa.Column("ai_stop_loss", sa.Float(), nullable=True),
        sa.Column("ai_target_price", sa.Float(), nullable=True),
        sa.Column(
            "first_detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "last_detected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_evaluated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "last_successful_evaluation_at", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "consecutive_pass_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("symbol", name="uq_research_candidates_symbol"),
    )
    op.create_index("ix_research_candidates_symbol", "research_candidates", ["symbol"])
    op.create_index("ix_research_candidates_status", "research_candidates", ["status"])
    op.create_index(
        "ix_research_candidates_expires_at", "research_candidates", ["expires_at"]
    )


def _create_research_candidate_events() -> None:
    op.create_table(
        "research_candidate_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "candidate_id",
            sa.Integer(),
            sa.ForeignKey("research_candidates.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("details", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index(
        "ix_research_candidate_events_candidate_id",
        "research_candidate_events",
        ["candidate_id"],
    )
    op.create_index(
        "ix_research_candidate_events_symbol", "research_candidate_events", ["symbol"]
    )
    op.create_index(
        "ix_research_candidate_events_event_type",
        "research_candidate_events",
        ["event_type"],
    )


def _create_trade_watchlist_symbols() -> None:
    op.create_table(
        "trade_watchlist_symbols",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "manual_override", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column(
            "source",
            sa.String(length=32),
            nullable=False,
            server_default="RESEARCH_PROMOTION",
        ),
        sa.Column("promotion_reason", sa.Text(), nullable=True),
        sa.Column("research_score", sa.Float(), nullable=True),
        sa.Column("confidence_score", sa.Float(), nullable=True),
        sa.Column("risk_score", sa.Float(), nullable=True),
        sa.Column(
            "consecutive_fail_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "eligible_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_qualified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("removal_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("symbol", name="uq_trade_watchlist_symbols_symbol"),
    )
    op.create_index(
        "ix_trade_watchlist_symbols_symbol", "trade_watchlist_symbols", ["symbol"]
    )
    op.create_index(
        "ix_trade_watchlist_symbols_is_active", "trade_watchlist_symbols", ["is_active"]
    )
    op.create_index(
        "ix_trade_watchlist_symbols_expires_at",
        "trade_watchlist_symbols",
        ["expires_at"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "research_candidate_events" in tables:
        op.drop_table("research_candidate_events")
    if "trade_watchlist_symbols" in tables:
        op.drop_table("trade_watchlist_symbols")
    if "research_candidates" in tables:
        op.drop_table("research_candidates")
