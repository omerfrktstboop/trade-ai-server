"""broker account normalization and durable cash reservations"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260713_03"
down_revision = "20260713_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "trade_profiles" in tables:
        profile_columns = {
            column["name"] for column in inspector.get_columns("trade_profiles")
        }
        if "allow_margin_buying" not in profile_columns:
            op.add_column(
                "trade_profiles",
                sa.Column(
                    "allow_margin_buying",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                ),
            )

    if "order_cash_reservations" not in tables:
        op.create_table(
            "order_cash_reservations",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("request_id", sa.String(64), nullable=False),
            sa.Column("symbol", sa.String(32), nullable=False),
            sa.Column("side", sa.String(8), nullable=False),
            sa.Column("reserved_qty", sa.Integer(), nullable=False),
            sa.Column("remaining_qty", sa.Integer(), nullable=False),
            sa.Column("limit_price", sa.Numeric(28, 10), nullable=False),
            sa.Column("reserved_amount_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("status", sa.String(32), nullable=False),
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
            sa.Column("released_at", sa.DateTime(timezone=True)),
            sa.UniqueConstraint(
                "request_id", name="uq_order_cash_reservations_request_id"
            ),
            sa.CheckConstraint(
                "reserved_qty >= 0", name="ck_cash_reservation_qty_nonnegative"
            ),
            sa.CheckConstraint(
                "remaining_qty >= 0 AND remaining_qty <= reserved_qty",
                name="ck_cash_reservation_remaining_qty",
            ),
            sa.CheckConstraint(
                "limit_price > 0", name="ck_cash_reservation_price_positive"
            ),
            sa.CheckConstraint(
                "reserved_amount_tl >= 0",
                name="ck_cash_reservation_amount_nonnegative",
            ),
        )
        op.create_index(
            "ix_order_cash_reservations_request_id",
            "order_cash_reservations",
            ["request_id"],
        )
        op.create_index(
            "ix_order_cash_reservations_symbol",
            "order_cash_reservations",
            ["symbol"],
        )
        op.create_index(
            "ix_order_cash_reservations_status",
            "order_cash_reservations",
            ["status"],
        )

    if "account_reservation_scopes" not in tables:
        op.create_table(
            "account_reservation_scopes",
            sa.Column("scope_key", sa.String(32), primary_key=True),
            sa.Column("lock_version", sa.Integer(), nullable=False, default=0),
        )

    if "account_normalization_audits" not in tables:
        op.create_table(
            "account_normalization_audits",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("request_id", sa.String(64)),
            sa.Column("symbol", sa.String(32)),
            sa.Column("source_provider", sa.String(64), nullable=False),
            sa.Column("source_fields", sa.JSON(), nullable=False),
            sa.Column("normalization_policy", sa.String(128), nullable=False),
            sa.Column("reservation_handling", sa.String(32), nullable=False),
            sa.Column("account_data_reliable", sa.Boolean(), nullable=False),
            sa.Column("unreliable_reasons", sa.JSON(), nullable=False),
            sa.Column("account_data_age_seconds", sa.Numeric(20, 8)),
            sa.Column("margin_buying_enabled", sa.Boolean(), nullable=False),
            sa.Column("broker_reported_buying_power_tl", sa.Numeric(28, 10)),
            sa.Column("backend_reserved_cash_tl", sa.Numeric(28, 10), nullable=False),
            sa.Column("effective_available_cash_tl", sa.Numeric(28, 10)),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_account_normalization_audits_request_id",
            "account_normalization_audits",
            ["request_id"],
        )
        op.create_index(
            "ix_account_normalization_audits_symbol",
            "account_normalization_audits",
            ["symbol"],
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "account_normalization_audits" in tables:
        op.drop_index(
            "ix_account_normalization_audits_symbol",
            table_name="account_normalization_audits",
        )
        op.drop_index(
            "ix_account_normalization_audits_request_id",
            table_name="account_normalization_audits",
        )
        op.drop_table("account_normalization_audits")
    if "order_cash_reservations" in tables:
        op.drop_index(
            "ix_order_cash_reservations_status",
            table_name="order_cash_reservations",
        )
        op.drop_index(
            "ix_order_cash_reservations_symbol",
            table_name="order_cash_reservations",
        )
        op.drop_index(
            "ix_order_cash_reservations_request_id",
            table_name="order_cash_reservations",
        )
        op.drop_table("order_cash_reservations")
    if "account_reservation_scopes" in tables:
        op.drop_table("account_reservation_scopes")
    if "trade_profiles" in tables:
        columns = {column["name"] for column in inspector.get_columns("trade_profiles")}
        if "allow_margin_buying" in columns:
            with op.batch_alter_table("trade_profiles") as batch:
                batch.drop_column("allow_margin_buying")
