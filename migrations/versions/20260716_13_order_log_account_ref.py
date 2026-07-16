"""order_logs.account_ref — emir gönderim anında sabitlenen hesap referansı

Callback fill'i bu sabit değeri kullanır (Fix #1).

Revision ID: 20260716_13
Revises: 20260716_12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260716_13"
down_revision = "20260716_12"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "order_logs" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("order_logs")}
    if "account_ref" not in columns:
        op.add_column(
            "order_logs", sa.Column("account_ref", sa.String(64), nullable=True)
        )
        op.create_index("ix_order_logs_account_ref", "order_logs", ["account_ref"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "order_logs" not in set(inspector.get_table_names()):
        return
    columns = {c["name"] for c in inspector.get_columns("order_logs")}
    if "account_ref" in columns:
        op.drop_index("ix_order_logs_account_ref", table_name="order_logs")
        op.drop_column("order_logs", "account_ref")
