"""order_fills.account_ref (DEMO/REAL PnL ayrımı) + arming oturum anahtarları

Revision ID: 20260716_12
Revises: 20260716_11
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260716_12"
down_revision = "20260716_11"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "order_fills" in tables:
        columns = {c["name"] for c in inspector.get_columns("order_fills")}
        if "account_ref" not in columns:
            op.add_column(
                "order_fills",
                sa.Column("account_ref", sa.String(64), nullable=True),
            )
            op.create_index(
                "ix_order_fills_account_ref", "order_fills", ["account_ref"]
            )

    if "system_configs" in tables:
        for key in ("armedAccountSessionRef", "armedAccountType"):
            existing = bind.execute(
                sa.text("SELECT 1 FROM system_configs WHERE key = :key"),
                {"key": key},
            ).fetchone()
            if existing is None:
                bind.execute(
                    sa.text(
                        "INSERT INTO system_configs "
                        "(key, value, value_type, description, is_sensitive) "
                        "VALUES (:key, '', 'string', :description, :sensitive)"
                    ),
                    {
                        "key": key,
                        "description": f"v2 arming metadata: {key}",
                        "sensitive": False,
                    },
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "order_fills" in tables:
        columns = {c["name"] for c in inspector.get_columns("order_fills")}
        if "account_ref" in columns:
            op.drop_index("ix_order_fills_account_ref", table_name="order_fills")
            op.drop_column("order_fills", "account_ref")

    if "system_configs" in tables:
        bind.execute(
            sa.text(
                "DELETE FROM system_configs "
                "WHERE key IN ('armedAccountSessionRef', 'armedAccountType')"
            )
        )
