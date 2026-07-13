"""deterministic position sizing risk config and audit"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260713_02"
down_revision = "20260712_01"
branch_labels = None
depends_on = None

_PROFILE_COLUMNS = {
    "version": sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    "risk_per_trade_pct": sa.Column(
        "risk_per_trade_pct", sa.Numeric(20, 8), nullable=False, server_default="0.50"
    ),
    "max_cash_utilization_pct": sa.Column(
        "max_cash_utilization_pct",
        sa.Numeric(20, 8),
        nullable=False,
        server_default="25",
    ),
    "max_account_exposure_pct": sa.Column(
        "max_account_exposure_pct",
        sa.Numeric(20, 8),
        nullable=False,
        server_default="50",
    ),
    "min_order_value_tl": sa.Column(
        "min_order_value_tl", sa.Numeric(20, 8), nullable=False, server_default="1"
    ),
    "min_stop_distance_pct": sa.Column(
        "min_stop_distance_pct",
        sa.Numeric(20, 8),
        nullable=False,
        server_default="0.10",
    ),
    "max_stop_distance_pct": sa.Column(
        "max_stop_distance_pct", sa.Numeric(20, 8), nullable=False, server_default="10"
    ),
    "minimum_stop_slippage_pct": sa.Column(
        "minimum_stop_slippage_pct",
        sa.Numeric(20, 8),
        nullable=False,
        server_default="0.05",
    ),
    "maximum_stop_slippage_pct": sa.Column(
        "maximum_stop_slippage_pct",
        sa.Numeric(20, 8),
        nullable=False,
        server_default="1",
    ),
    "profile_stop_slippage_pct": sa.Column(
        "profile_stop_slippage_pct",
        sa.Numeric(20, 8),
        nullable=False,
        server_default="0.20",
    ),
    "max_account_data_age_seconds": sa.Column(
        "max_account_data_age_seconds",
        sa.Numeric(20, 8),
        nullable=False,
        server_default="60",
    ),
}


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "trade_profiles" in tables:
        existing = {
            column["name"] for column in inspector.get_columns("trade_profiles")
        }
        for name, column in _PROFILE_COLUMNS.items():
            if name not in existing:
                op.add_column("trade_profiles", column)
        if "code" in existing:
            bind.execute(
                sa.text(
                    """
                    UPDATE trade_profiles SET
                        risk_per_trade_pct = CASE code
                            WHEN 'CONSERVATIVE' THEN 0.25
                            WHEN 'NORMAL' THEN 0.50
                            WHEN 'AGGRESSIVE' THEN 0.75
                            WHEN 'HIGH_RISK' THEN 1.00
                            ELSE risk_per_trade_pct END,
                        max_cash_utilization_pct = CASE code
                            WHEN 'CONSERVATIVE' THEN 15
                            WHEN 'NORMAL' THEN 25
                            WHEN 'AGGRESSIVE' THEN 35
                            WHEN 'HIGH_RISK' THEN 50
                            ELSE max_cash_utilization_pct END,
                        max_account_exposure_pct = CASE code
                            WHEN 'CONSERVATIVE' THEN 30
                            WHEN 'NORMAL' THEN 50
                            WHEN 'AGGRESSIVE' THEN 65
                            WHEN 'HIGH_RISK' THEN 75
                            ELSE max_account_exposure_pct END
                    """
                )
            )
        # PostgreSQL must retain exact Decimal values for the three pre-existing
        # sizing columns. SQLite gets exact NUMERIC columns on fresh create_all.
        if bind.dialect.name == "postgresql":
            with op.batch_alter_table("trade_profiles") as batch:
                for name in (
                    "max_order_value_tl",
                    "max_position_value_per_symbol",
                ):
                    if name in existing:
                        batch.alter_column(
                            name,
                            existing_type=sa.Float(),
                            type_=sa.Numeric(20, 8),
                            postgresql_using=f"{name}::numeric(20,8)",
                        )
                if "max_qty_per_order" in existing:
                    batch.alter_column(
                        "max_qty_per_order",
                        existing_type=sa.Float(),
                        type_=sa.Integer(),
                        postgresql_using="floor(max_qty_per_order)::integer",
                    )
        else:
            with op.batch_alter_table("trade_profiles") as batch:
                for name in (
                    "max_order_value_tl",
                    "max_position_value_per_symbol",
                ):
                    if name in existing:
                        batch.alter_column(
                            name,
                            existing_type=sa.Float(),
                            type_=sa.Numeric(20, 8),
                        )
                if "max_qty_per_order" in existing:
                    batch.alter_column(
                        "max_qty_per_order",
                        existing_type=sa.Float(),
                        type_=sa.Integer(),
                    )

    if "position_sizing_audits" not in tables:
        op.create_table(
            "position_sizing_audits",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("request_id", sa.String(64), nullable=False),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("trade_profile_id", sa.Integer()),
            sa.Column("trade_profile_version", sa.Integer(), nullable=False),
            sa.Column("system_config_version", sa.String(128), nullable=False),
            sa.Column("environment_config_fingerprint", sa.String(64), nullable=False),
            sa.Column("account_equity_tl", sa.Numeric(28, 10)),
            sa.Column("effective_available_cash_tl", sa.Numeric(28, 10)),
            sa.Column("risk_per_trade_pct", sa.Numeric(20, 10), nullable=False),
            sa.Column("risk_budget_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("entry_price", sa.Numeric(28, 10)),
            sa.Column("stop_loss", sa.Numeric(28, 10)),
            sa.Column("raw_stop_distance_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("slippage_buffer_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("effective_stop_distance_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("qty_by_risk", sa.Integer()),
            sa.Column("qty_by_cash", sa.Integer()),
            sa.Column("qty_by_account_exposure", sa.Integer()),
            sa.Column("qty_by_symbol_position", sa.Integer()),
            sa.Column("qty_by_order_value", sa.Integer()),
            sa.Column("qty_by_profile_max", sa.Integer()),
            sa.Column("final_qty", sa.Integer(), nullable=False),
            sa.Column("order_value_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("estimated_loss_at_stop_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("binding_limits", sa.JSON(), nullable=False),
            sa.Column("allowed", sa.Boolean(), nullable=False),
            sa.Column("reason", sa.Text(), nullable=False),
            sa.Column("effective_risk_config", sa.JSON(), nullable=False),
            sa.Column("calculation_details", sa.JSON(), nullable=False),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_position_sizing_audits_request_id",
            "position_sizing_audits",
            ["request_id"],
        )
        op.create_index(
            "ix_position_sizing_audits_symbol",
            "position_sizing_audits",
            ["symbol"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "position_sizing_audits" in tables:
        op.drop_index(
            "ix_position_sizing_audits_symbol", table_name="position_sizing_audits"
        )
        op.drop_index(
            "ix_position_sizing_audits_request_id",
            table_name="position_sizing_audits",
        )
        op.drop_table("position_sizing_audits")
    if "trade_profiles" in tables:
        columns = {column["name"] for column in inspector.get_columns("trade_profiles")}
        if bind.dialect.name == "postgresql":
            with op.batch_alter_table("trade_profiles") as batch:
                for name in (
                    "max_order_value_tl",
                    "max_position_value_per_symbol",
                ):
                    if name in columns:
                        batch.alter_column(
                            name,
                            existing_type=sa.Numeric(20, 8),
                            type_=sa.Float(),
                            postgresql_using=f"{name}::double precision",
                        )
                if "max_qty_per_order" in columns:
                    batch.alter_column(
                        "max_qty_per_order",
                        existing_type=sa.Integer(),
                        type_=sa.Float(),
                        postgresql_using="max_qty_per_order::double precision",
                    )
        else:
            with op.batch_alter_table("trade_profiles") as batch:
                for name in (
                    "max_order_value_tl",
                    "max_position_value_per_symbol",
                ):
                    if name in columns:
                        batch.alter_column(
                            name,
                            existing_type=sa.Numeric(20, 8),
                            type_=sa.Float(),
                        )
                if "max_qty_per_order" in columns:
                    batch.alter_column(
                        "max_qty_per_order",
                        existing_type=sa.Integer(),
                        type_=sa.Float(),
                    )
        with op.batch_alter_table("trade_profiles") as batch:
            for name in reversed(tuple(_PROFILE_COLUMNS)):
                if name in columns:
                    batch.drop_column(name)
