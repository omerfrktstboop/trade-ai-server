"""ai_call_claims — kalıcı, bar-farkında AI çağrı kilidi (Plan Faz 1.2).

Aynı (sembol, bar, setup parmak izi) için restart sonrası dahil tekrar LLM
çağrısını önler. Benzersizlik kısıtı atomik talep içindir.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260723_17"
down_revision = "20260722_16"
branch_labels = None
depends_on = None

_TABLE = "ai_call_claims"


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("symbol", sa.String(length=16), nullable=False),
        sa.Column("bar_key", sa.String(length=64), nullable=False),
        sa.Column("setup_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("evaluation_purpose", sa.String(length=32), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "symbol",
            "bar_key",
            "setup_fingerprint",
            name="uq_ai_call_claim_symbol_bar_setup",
        ),
    )
    op.create_index("ix_ai_call_claims_symbol", _TABLE, ["symbol"])
    op.create_index("ix_ai_call_claims_created_at", _TABLE, ["created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    op.drop_index("ix_ai_call_claims_created_at", table_name=_TABLE)
    op.drop_index("ix_ai_call_claims_symbol", table_name=_TABLE)
    op.drop_table(_TABLE)
