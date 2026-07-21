"""AI capital budget and durable portfolio rotation plans.

Revision ID: 20260720_15
Revises: 20260717_14
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260720_15"
down_revision = "20260717_14"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "rotation_plans",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("account_ref", sa.String(length=64), nullable=False),
        sa.Column("source_symbol", sa.String(length=16), nullable=False),
        sa.Column("target_symbol", sa.String(length=16), nullable=False),
        sa.Column("source_qty", sa.Integer(), nullable=False),
        sa.Column("target_qty", sa.Integer(), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("source_opportunity_score", sa.Float(), nullable=False),
        sa.Column("target_opportunity_score", sa.Float(), nullable=False),
        sa.Column("source_expected_return_pct", sa.Float(), nullable=False),
        sa.Column("target_expected_return_pct", sa.Float(), nullable=False),
        sa.Column("source_assessment_request_id", sa.String(length=64), nullable=False),
        sa.Column("target_assessment_request_id", sa.String(length=64), nullable=False),
        sa.Column("source_position_generation", sa.Integer(), nullable=True),
        sa.Column("source_fill_position_generation", sa.Integer(), nullable=True),
        sa.Column("sell_request_id", sa.String(length=64), nullable=True),
        sa.Column("buy_request_id", sa.String(length=64), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column("not_before", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("buy_request_id", name="uq_rotation_plans_buy_request_id"),
        sa.UniqueConstraint("sell_request_id", name="uq_rotation_plans_sell_request_id"),
    )
    op.create_index(
        "ix_rotation_plans_account_ref", "rotation_plans", ["account_ref"]
    )
    op.create_index(
        "ix_rotation_plans_source_symbol", "rotation_plans", ["source_symbol"]
    )
    op.create_index(
        "ix_rotation_plans_target_symbol", "rotation_plans", ["target_symbol"]
    )
    op.create_index("ix_rotation_plans_state", "rotation_plans", ["state"])
    op.create_index(
        "ix_rotation_plans_account_state",
        "rotation_plans",
        ["account_ref", "state"],
    )


def downgrade() -> None:
    op.drop_index("ix_rotation_plans_account_state", table_name="rotation_plans")
    op.drop_index("ix_rotation_plans_state", table_name="rotation_plans")
    op.drop_index("ix_rotation_plans_target_symbol", table_name="rotation_plans")
    op.drop_index("ix_rotation_plans_source_symbol", table_name="rotation_plans")
    op.drop_index("ix_rotation_plans_account_ref", table_name="rotation_plans")
    op.drop_table("rotation_plans")
