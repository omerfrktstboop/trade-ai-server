"""trade_profiles.max_decision_age_seconds_for_buy — admin-configurable
order-time decision staleness threshold (was a hardcoded 20s constant in
order_preflight.py)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260722_16"
down_revision = "20260720_15"
branch_labels = None
depends_on = None

_COLUMN_NAME = "max_decision_age_seconds_for_buy"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "trade_profiles" not in set(inspector.get_table_names()):
        return
    existing = {column["name"] for column in inspector.get_columns("trade_profiles")}
    if _COLUMN_NAME not in existing:
        op.add_column(
            "trade_profiles",
            sa.Column(
                _COLUMN_NAME, sa.Float(), nullable=False, server_default="60"
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "trade_profiles" not in set(inspector.get_table_names()):
        return
    existing = {column["name"] for column in inspector.get_columns("trade_profiles")}
    if _COLUMN_NAME in existing:
        with op.batch_alter_table("trade_profiles") as batch:
            batch.drop_column(_COLUMN_NAME)
