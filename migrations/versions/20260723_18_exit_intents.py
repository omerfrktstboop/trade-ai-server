"""exit_intents — pozisyon çıkışlarının kalıcı niyet + durum kaydı (Plan Faz 2.2)."""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260723_18"
down_revision = "20260723_17"
branch_labels = None
depends_on = None

_TABLE = "exit_intents"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("position_lifecycle_id", sa.Integer(), nullable=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("exit_reason", sa.String(length=24), nullable=False),
        sa.Column("trigger_price", sa.Numeric(28, 10), nullable=True),
        sa.Column("trigger_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_version", sa.String(length=32), nullable=False),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("order_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="ACCEPTED",
        ),
        sa.Column(
            "cancel_reprice_generation",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
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
    op.create_index("ix_exit_intents_symbol", _TABLE, ["symbol"])
    op.create_index("ix_exit_intents_symbol_status", _TABLE, ["symbol", "status"])
    op.create_index("ix_exit_intents_created_at", _TABLE, ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    op.drop_index("ix_exit_intents_created_at", table_name=_TABLE)
    op.drop_index("ix_exit_intents_symbol_status", table_name=_TABLE)
    op.drop_index("ix_exit_intents_symbol", table_name=_TABLE)
    op.drop_table(_TABLE)
