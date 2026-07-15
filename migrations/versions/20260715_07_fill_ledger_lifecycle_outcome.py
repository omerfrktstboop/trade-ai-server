"""order fill ledger, position lifecycle, position stop events, decision outcomes

Revision ID: 20260715_07
Revises: 20260715_06
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260715_07"
down_revision = "20260715_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "order_fills" not in tables:
        op.create_table(
            "order_fills",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("order_log_id", sa.Integer(), sa.ForeignKey("order_logs.id"), nullable=False),
            sa.Column("request_id", sa.String(64), nullable=False),
            sa.Column("order_id", sa.String(64)),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("action", sa.String(8), nullable=False),
            sa.Column("fill_qty", sa.Numeric(28, 10), nullable=False),
            sa.Column("fill_price", sa.Numeric(28, 10), nullable=False),
            sa.Column("limit_price", sa.Numeric(28, 10)),
            sa.Column("gross_value_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("commission_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("exchange_fee_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("other_fee_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("total_cost_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("slippage_tl", sa.Numeric(28, 10)),
            sa.Column("slippage_pct", sa.Numeric(28, 10)),
            sa.Column("fill_event_key", sa.String(128), nullable=False),
            sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint("fill_event_key", name="uq_order_fills_fill_event_key"),
        )
        op.create_index("ix_order_fills_order_log_id", "order_fills", ["order_log_id"])
        op.create_index("ix_order_fills_request_id", "order_fills", ["request_id"])
        op.create_index("ix_order_fills_symbol", "order_fills", ["symbol"])

    if "position_lifecycles" not in tables:
        op.create_table(
            "position_lifecycles",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("status", sa.String(16), nullable=False, server_default="OPEN"),
            sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("closed_at", sa.DateTime(timezone=True)),
            sa.Column("entry_request_id", sa.String(64)),
            sa.Column("entry_order_id", sa.String(64)),
            sa.Column("current_qty", sa.Numeric(28, 10), nullable=False, server_default="0"),
            sa.Column("average_entry_price", sa.Numeric(28, 10)),
            sa.Column("gross_buy_value_tl", sa.Numeric(28, 10), nullable=False, server_default="0"),
            sa.Column("gross_sell_value_tl", sa.Numeric(28, 10), nullable=False, server_default="0"),
            sa.Column("total_buy_cost_tl", sa.Numeric(28, 10), nullable=False, server_default="0"),
            sa.Column("total_sell_cost_tl", sa.Numeric(28, 10), nullable=False, server_default="0"),
            sa.Column("gross_realized_pnl_tl", sa.Numeric(28, 10), nullable=False, server_default="0"),
            sa.Column("net_realized_pnl_tl", sa.Numeric(28, 10), nullable=False, server_default="0"),
            sa.Column("initial_stop_loss", sa.Numeric(28, 10)),
            sa.Column("active_stop_loss", sa.Numeric(28, 10)),
            sa.Column("initial_target_price", sa.Numeric(28, 10)),
            sa.Column("active_target_price", sa.Numeric(28, 10)),
            sa.Column("strategy_version", sa.String(64)),
            sa.Column("prompt_version", sa.String(64)),
            sa.Column("config_hash", sa.String(128)),
            sa.Column("profile_code", sa.String(64)),
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
        )
        op.create_index("ix_position_lifecycles_symbol", "position_lifecycles", ["symbol"])
        op.create_index(
            "ix_position_lifecycles_symbol_status",
            "position_lifecycles",
            ["symbol", "status"],
        )

    if "position_stop_events" not in tables:
        op.create_table(
            "position_stop_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                "position_lifecycle_id",
                sa.Integer(),
                sa.ForeignKey("position_lifecycles.id"),
                nullable=False,
            ),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("old_stop", sa.Numeric(28, 10)),
            sa.Column("new_stop", sa.Numeric(28, 10)),
            sa.Column("event_type", sa.String(32), nullable=False),
            sa.Column("source_request_id", sa.String(64)),
            sa.Column("source_order_id", sa.String(64)),
            sa.Column("reason", sa.Text()),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_position_stop_events_position_lifecycle_id",
            "position_stop_events",
            ["position_lifecycle_id"],
        )
        op.create_index("ix_position_stop_events_symbol", "position_stop_events", ["symbol"])

    if "decision_outcomes" not in tables:
        op.create_table(
            "decision_outcomes",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("request_id", sa.String(64), nullable=False),
            sa.Column("symbol", sa.String(16), nullable=False),
            sa.Column("evaluation_purpose", sa.String(64)),
            sa.Column("decision_action", sa.String(8), nullable=False),
            sa.Column("decision_price", sa.Numeric(28, 10)),
            sa.Column("decision_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("strategy_version", sa.String(64)),
            sa.Column("prompt_version", sa.String(64)),
            sa.Column("profile_code", sa.String(64)),
            sa.Column("config_hash", sa.String(128)),
            sa.Column("discovery_sources", sa.JSON()),
            sa.Column("market_regime", sa.String(32)),
            sa.Column("trend_pre_score", sa.Numeric(28, 10)),
            sa.Column("research_score", sa.Numeric(28, 10)),
            sa.Column("confidence_score", sa.Numeric(28, 10)),
            sa.Column("risk_score", sa.Numeric(28, 10)),
            sa.Column("entry_price", sa.Numeric(28, 10)),
            sa.Column("stop_loss", sa.Numeric(28, 10)),
            sa.Column("target_price", sa.Numeric(28, 10)),
            sa.Column("future_return_5m", sa.Numeric(28, 10)),
            sa.Column("future_return_15m", sa.Numeric(28, 10)),
            sa.Column("future_return_30m", sa.Numeric(28, 10)),
            sa.Column("future_return_60m", sa.Numeric(28, 10)),
            sa.Column("future_return_eod", sa.Numeric(28, 10)),
            sa.Column("mfe_pct", sa.Numeric(28, 10)),
            sa.Column("mae_pct", sa.Numeric(28, 10)),
            sa.Column("target_hit_at", sa.DateTime(timezone=True)),
            sa.Column("stop_hit_at", sa.DateTime(timezone=True)),
            sa.Column("target_hit_before_stop", sa.Boolean()),
            sa.Column(
                "outcome_status", sa.String(16), nullable=False, server_default="PENDING"
            ),
            sa.Column("unavailable_reason", sa.Text()),
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
            sa.UniqueConstraint("request_id", name="uq_decision_outcomes_request_id"),
        )
        op.create_index("ix_decision_outcomes_request_id", "decision_outcomes", ["request_id"])
        op.create_index("ix_decision_outcomes_symbol", "decision_outcomes", ["symbol"])
        op.create_index("ix_decision_outcomes_decision_at", "decision_outcomes", ["decision_at"])
        op.create_index(
            "ix_decision_outcomes_outcome_status", "decision_outcomes", ["outcome_status"]
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "decision_outcomes" in tables:
        op.drop_index("ix_decision_outcomes_outcome_status", table_name="decision_outcomes")
        op.drop_index("ix_decision_outcomes_decision_at", table_name="decision_outcomes")
        op.drop_index("ix_decision_outcomes_symbol", table_name="decision_outcomes")
        op.drop_index("ix_decision_outcomes_request_id", table_name="decision_outcomes")
        op.drop_table("decision_outcomes")

    if "position_stop_events" in tables:
        op.drop_index("ix_position_stop_events_symbol", table_name="position_stop_events")
        op.drop_index(
            "ix_position_stop_events_position_lifecycle_id",
            table_name="position_stop_events",
        )
        op.drop_table("position_stop_events")

    if "position_lifecycles" in tables:
        op.drop_index(
            "ix_position_lifecycles_symbol_status", table_name="position_lifecycles"
        )
        op.drop_index("ix_position_lifecycles_symbol", table_name="position_lifecycles")
        op.drop_table("position_lifecycles")

    if "order_fills" in tables:
        op.drop_index("ix_order_fills_symbol", table_name="order_fills")
        op.drop_index("ix_order_fills_request_id", table_name="order_fills")
        op.drop_index("ix_order_fills_order_log_id", table_name="order_fills")
        op.drop_table("order_fills")
