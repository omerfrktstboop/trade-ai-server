"""account_events tablosu + v2 systemMode seed'i (Faz 4)

Hiçbir satır silinmez — eski mod anahtarları geçiş dönemi boyunca yerinde
kalır (paralel kur, en son sil ilkesi).

Revision ID: 20260716_11
Revises: 20260716_10
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260716_11"
down_revision = "20260716_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "account_events" not in tables:
        op.create_table(
            "account_events",
            sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column("event_type", sa.String(32), nullable=False),
            sa.Column("account_ref", sa.String(64), nullable=True),
            sa.Column("account_session_ref", sa.String(64), nullable=True),
            sa.Column("account_type", sa.String(16), nullable=True),
            sa.Column("previous_ref", sa.String(64), nullable=True),
            sa.Column("source", sa.String(16), nullable=False),
            sa.Column("detail", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
            ),
        )
        op.create_index(
            "ix_account_events_event_type", "account_events", ["event_type"]
        )

    if "system_configs" in tables:
        # Güvenli seed: satır yoksa OBSERVE_ONLY / disarmed yaz; varsa dokunma.
        for key, value, value_type, description in (
            (
                "systemMode",
                "OBSERVE_ONLY",
                "system_mode",
                "v2 çalışma modu (OBSERVE_ONLY|AUTO_TRADE).",
            ),
            (
                "realAccountArmed",
                "false",
                "bool",
                "REAL hesap emir yolu arming durumu.",
            ),
        ):
            existing = bind.execute(
                sa.text("SELECT 1 FROM system_configs WHERE key = :key"),
                {"key": key},
            ).fetchone()
            if existing is None:
                bind.execute(
                    sa.text(
                        "INSERT INTO system_configs "
                        "(key, value, value_type, description, is_sensitive) "
                        "VALUES (:key, :value, :value_type, :description, :sensitive)"
                    ),
                    {
                        "key": key,
                        "value": value,
                        "value_type": value_type,
                        "description": description,
                        "sensitive": False,
                    },
                )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if "account_events" in tables:
        op.drop_index("ix_account_events_event_type", table_name="account_events")
        op.drop_table("account_events")
    if "system_configs" in tables:
        bind.execute(
            sa.text(
                "DELETE FROM system_configs "
                "WHERE key IN ('systemMode', 'realAccountArmed')"
            )
        )
