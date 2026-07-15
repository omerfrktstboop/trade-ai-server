"""measurement repair jobs, market observations, fill_source, lifecycle
data-quality/provenance fields, decision-outcome raw/final action split,
partial unique open-lifecycle index (Task 1-9 measurement infrastructure)

Revision ID: 20260715_08
Revises: 20260715_07
"""

from __future__ import annotations

import logging

from alembic import op
import sqlalchemy as sa

revision = "20260715_08"
down_revision = "20260715_07"
branch_labels = None
depends_on = None

logger = logging.getLogger("alembic.runtime.migration")


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    # ── Task 1: measurement_repair_jobs ────────────────────────────────────
    if "measurement_repair_jobs" not in tables:
        op.create_table(
            "measurement_repair_jobs",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("request_id", sa.String(64)),
            sa.Column("order_log_id", sa.Integer()),
            sa.Column("symbol", sa.String(16)),
            sa.Column("repair_type", sa.String(32), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="PENDING"),
            sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_error", sa.Text()),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column("completed_at", sa.DateTime(timezone=True)),
        )
        op.create_index(
            "ix_measurement_repair_jobs_request_id",
            "measurement_repair_jobs",
            ["request_id"],
        )
        op.create_index(
            "ix_measurement_repair_jobs_order_log_id",
            "measurement_repair_jobs",
            ["order_log_id"],
        )
        op.create_index(
            "ix_measurement_repair_jobs_symbol", "measurement_repair_jobs", ["symbol"]
        )
        op.create_index(
            "ix_measurement_repair_jobs_repair_type",
            "measurement_repair_jobs",
            ["repair_type"],
        )
        op.create_index(
            "ix_measurement_repair_jobs_status", "measurement_repair_jobs", ["status"]
        )
        op.create_index(
            "ix_measurement_repair_jobs_next_attempt_at",
            "measurement_repair_jobs",
            ["next_attempt_at"],
        )
        tables.add("measurement_repair_jobs")

    # ── Task 3: market_observations ─────────────────────────────────────────
    if "market_observations" not in tables:
        op.create_table(
            "market_observations",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("observed_at_source", sa.String(24), nullable=False),
            sa.Column("last_price", sa.Numeric(28, 10)),
            sa.Column("open", sa.Numeric(28, 10)),
            sa.Column("high", sa.Numeric(28, 10)),
            sa.Column("low", sa.Numeric(28, 10)),
            sa.Column("close", sa.Numeric(28, 10)),
            sa.Column("bar_period", sa.String(16)),
            sa.Column("bar_closed", sa.Boolean()),
            sa.Column("quote_reliable", sa.Boolean()),
            sa.Column("ohlc_reliable", sa.Boolean()),
            sa.Column("quote_age_seconds", sa.Numeric(20, 6)),
            sa.Column("ohlcv_age_seconds", sa.Numeric(20, 6)),
            sa.Column("price_source", sa.String(32)),
            sa.Column("request_id", sa.String(64)),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "symbol",
                "observed_at",
                "bar_period",
                "price_source",
                name="uq_market_observations_symbol_time_period_source",
            ),
        )
        op.create_index("ix_market_observations_symbol", "market_observations", ["symbol"])
        op.create_index(
            "ix_market_observations_observed_at", "market_observations", ["observed_at"]
        )
        op.create_index(
            "ix_market_observations_request_id", "market_observations", ["request_id"]
        )
        tables.add("market_observations")

    # ── Task 1: order_fills.fill_source ─────────────────────────────────────
    if "order_fills" in tables:
        columns = {c["name"] for c in inspector.get_columns("order_fills")}
        if "fill_source" not in columns:
            op.add_column(
                "order_fills",
                sa.Column(
                    "fill_source",
                    sa.String(16),
                    nullable=False,
                    server_default="CALLBACK_DELTA",
                ),
            )

    # ── Task 5/9: decision_outcomes new columns ─────────────────────────────
    if "decision_outcomes" in tables:
        columns = {c["name"] for c in inspector.get_columns("decision_outcomes")}
        outcome_new_columns = [
            ("raw_ai_action", sa.String(8)),
            ("final_action", sa.String(8)),
            ("allow_order", sa.Boolean()),
            ("block_reason", sa.String(64)),
            ("decision_source", sa.String(32)),
            ("raw_ai_confidence", sa.Numeric(28, 10)),
            ("final_confidence", sa.Numeric(28, 10)),
            ("raw_ai_risk_score", sa.Numeric(28, 10)),
            ("final_risk_score", sa.Numeric(28, 10)),
            ("decision_context_schema_version", sa.String(64)),
            ("ai_provider", sa.String(32)),
            ("ai_model", sa.String(64)),
        ]
        for name, col_type in outcome_new_columns:
            if name not in columns:
                op.add_column("decision_outcomes", sa.Column(name, col_type))

        if "final_action" not in columns:
            # decision_action was always the final action before this split
            # existed - backfilling final_action=decision_action for old rows
            # is a known fact, not a guess. raw_ai_action has no equivalent
            # source and is deliberately left NULL (Task 10's explicit rule:
            # never invent a historical raw AI action).
            bind.execute(
                sa.text(
                    "UPDATE decision_outcomes SET final_action = decision_action "
                    "WHERE final_action IS NULL"
                )
            )

    # ── Task 7/9: position_lifecycles new columns ───────────────────────────
    if "position_lifecycles" in tables:
        columns = {c["name"] for c in inspector.get_columns("position_lifecycles")}
        data_quality_is_new = "data_quality" not in columns

        if "data_quality" not in columns:
            # Never defaults to VERIFIED (explicit Task 10 rule) - PARTIAL is
            # a transient placeholder immediately reclassified by the two
            # UPDATEs below, in the same migration run only.
            op.add_column(
                "position_lifecycles",
                sa.Column(
                    "data_quality", sa.String(24), nullable=False, server_default="PARTIAL"
                ),
            )
        if "is_backfilled" not in columns:
            op.add_column(
                "position_lifecycles",
                sa.Column(
                    "is_backfilled", sa.Boolean(), nullable=False, server_default=sa.false()
                ),
            )
        if "backfill_reason" not in columns:
            op.add_column("position_lifecycles", sa.Column("backfill_reason", sa.Text()))
        if "pnl_verified" not in columns:
            op.add_column(
                "position_lifecycles",
                sa.Column(
                    "pnl_verified", sa.Boolean(), nullable=False, server_default=sa.true()
                ),
            )
        if "measurement_source" not in columns:
            op.add_column(
                "position_lifecycles",
                sa.Column(
                    "measurement_source",
                    sa.String(32),
                    nullable=False,
                    server_default="FILL_LEDGER",
                ),
            )
        for name, col_type in (
            ("decision_context_schema_version", sa.String(64)),
            ("ai_provider", sa.String(32)),
            ("ai_model", sa.String(64)),
            ("decision_source", sa.String(32)),
        ):
            if name not in columns:
                op.add_column("position_lifecycles", sa.Column(name, col_type))

        if data_quality_is_new:
            # A lifecycle opened by a real fill (entry_request_id set) has a
            # determinable, trustworthy source; one seeded by the legacy
            # BotPosition backfill (entry_request_id NULL - see
            # position_lifecycle_backfill.py) never had one recorded at all.
            bind.execute(
                sa.text(
                    "UPDATE position_lifecycles SET data_quality = 'VERIFIED', "
                    "measurement_source = 'FILL_LEDGER', is_backfilled = "
                    + ("0" if bind.dialect.name == "sqlite" else "FALSE")
                    + ", pnl_verified = "
                    + ("1" if bind.dialect.name == "sqlite" else "TRUE")
                    + " WHERE entry_request_id IS NOT NULL AND data_quality = 'PARTIAL'"
                )
            )
            bind.execute(
                sa.text(
                    "UPDATE position_lifecycles SET data_quality = "
                    "'BACKFILL_UNAVAILABLE', measurement_source = "
                    "'LEGACY_POSITION_BACKFILL', is_backfilled = "
                    + ("1" if bind.dialect.name == "sqlite" else "TRUE")
                    + ", pnl_verified = "
                    + ("0" if bind.dialect.name == "sqlite" else "FALSE")
                    + ", backfill_reason = "
                    "'pre_existing_position_without_recorded_fills' "
                    "WHERE entry_request_id IS NULL AND data_quality = 'PARTIAL'"
                )
            )

        # NOTE: the partial unique open-lifecycle index is NOT created here.
        # An earlier version of this migration silently skipped the index (and
        # marked rows MANUAL_REVIEW) when duplicates existed, which let the
        # revision report success while leaving the constraint absent (Fix 1).
        # Index creation now lives in 20260715_09, which hard-fails on
        # duplicates instead of silently skipping.


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "position_lifecycles" in tables:
        # The partial unique index is owned by 20260715_09 (created there,
        # dropped in its downgrade) - this revision no longer manages it.
        columns = {c["name"] for c in inspector.get_columns("position_lifecycles")}
        drop_cols = [
            name
            for name in (
                "data_quality",
                "is_backfilled",
                "backfill_reason",
                "pnl_verified",
                "measurement_source",
                "decision_context_schema_version",
                "ai_provider",
                "ai_model",
                "decision_source",
            )
            if name in columns
        ]
        if drop_cols:
            with op.batch_alter_table("position_lifecycles") as batch:
                for name in drop_cols:
                    batch.drop_column(name)

    if "decision_outcomes" in tables:
        columns = {c["name"] for c in inspector.get_columns("decision_outcomes")}
        drop_cols = [
            name
            for name in (
                "raw_ai_action",
                "final_action",
                "allow_order",
                "block_reason",
                "decision_source",
                "raw_ai_confidence",
                "final_confidence",
                "raw_ai_risk_score",
                "final_risk_score",
                "decision_context_schema_version",
                "ai_provider",
                "ai_model",
            )
            if name in columns
        ]
        if drop_cols:
            with op.batch_alter_table("decision_outcomes") as batch:
                for name in drop_cols:
                    batch.drop_column(name)

    if "order_fills" in tables:
        columns = {c["name"] for c in inspector.get_columns("order_fills")}
        if "fill_source" in columns:
            with op.batch_alter_table("order_fills") as batch:
                batch.drop_column("fill_source")

    if "market_observations" in tables:
        op.drop_index("ix_market_observations_request_id", table_name="market_observations")
        op.drop_index("ix_market_observations_observed_at", table_name="market_observations")
        op.drop_index("ix_market_observations_symbol", table_name="market_observations")
        op.drop_table("market_observations")

    if "measurement_repair_jobs" in tables:
        op.drop_index(
            "ix_measurement_repair_jobs_next_attempt_at",
            table_name="measurement_repair_jobs",
        )
        op.drop_index(
            "ix_measurement_repair_jobs_status", table_name="measurement_repair_jobs"
        )
        op.drop_index(
            "ix_measurement_repair_jobs_repair_type", table_name="measurement_repair_jobs"
        )
        op.drop_index(
            "ix_measurement_repair_jobs_symbol", table_name="measurement_repair_jobs"
        )
        op.drop_index(
            "ix_measurement_repair_jobs_order_log_id", table_name="measurement_repair_jobs"
        )
        op.drop_index(
            "ix_measurement_repair_jobs_request_id", table_name="measurement_repair_jobs"
        )
        op.drop_table("measurement_repair_jobs")
