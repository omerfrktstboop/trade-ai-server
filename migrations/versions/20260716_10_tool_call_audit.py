"""tool_call_audits — read-only tool (AI function-calling + MCP) çağrı audit'i

Revision ID: 20260716_10
Revises: 20260716_09
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260716_10"
down_revision = "20260716_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tool_call_audits" in set(inspector.get_table_names()):
        return

    op.create_table(
        "tool_call_audits",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("caller", sa.String(32), nullable=False),
        sa.Column("symbol_scope", sa.String(32), nullable=True),
        sa.Column("args_json", sa.Text(), nullable=True),
        sa.Column("ok", sa.Boolean(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("result_chars", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_tool_call_audits_tool_name", "tool_call_audits", ["tool_name"])
    op.create_index("ix_tool_call_audits_caller", "tool_call_audits", ["caller"])
    op.create_index(
        "ix_tool_call_audits_request_id", "tool_call_audits", ["request_id"]
    )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "tool_call_audits" not in set(inspector.get_table_names()):
        return
    op.drop_index("ix_tool_call_audits_request_id", table_name="tool_call_audits")
    op.drop_index("ix_tool_call_audits_caller", table_name="tool_call_audits")
    op.drop_index("ix_tool_call_audits_tool_name", table_name="tool_call_audits")
    op.drop_table("tool_call_audits")
