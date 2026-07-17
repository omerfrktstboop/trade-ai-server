"""v2 cutover: legacy mod/onay system_config satırlarını sil, systemMode seed

Eski mod sistemi (PAPER/MANUAL/DEMO_LIVE/REAL_LIVE) ve ilgili admin anahtarları
kaldırıldı. Bu migration ölü system_config satırlarını temizler ve systemMode'u
OBSERVE_ONLY'ye (default, fail-closed) sabitler. manual_approval_requests tablosu
varsa düşürülür.

Revision ID: 20260717_14
Revises: 20260716_13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "20260717_14"
down_revision = "20260716_13"
branch_labels = None
depends_on = None

_LEGACY_KEYS = (
    "tradingMode",
    "botMode",
    "botEnableDemoOrders",
    "botEnableRealOrders",
    "botRealLiveModeAllowed",
    "botRealLiveArmed",
    "botRequireDemoAccount",
    "botDemoAccountConfirmed",
    "tradingKillSwitchActive",
    "forceSafeMode",
    "scannerAllowOrders",
    "manualApprovalAllowOrders",
)


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "system_configs" in tables:
        # killSwitchEnabled'ı koru: eski tradingKillSwitchActive/forceSafeMode
        # açıksa yeni tek anahtarı da açık tut (güvenli yön).
        legacy_kill = bind.execute(
            sa.text(
                "SELECT value FROM system_configs "
                "WHERE key IN ('tradingKillSwitchActive', 'forceSafeMode') "
                "AND value = 'true' LIMIT 1"
            )
        ).fetchone()
        if legacy_kill is not None:
            bind.execute(
                sa.text(
                    "UPDATE system_configs SET value = 'true' "
                    "WHERE key = 'killSwitchEnabled'"
                )
            )

        bind.execute(
            sa.text(
                "DELETE FROM system_configs WHERE key IN :keys"
            ).bindparams(sa.bindparam("keys", expanding=True)),
            {"keys": list(_LEGACY_KEYS)},
        )

        # systemMode satırı yoksa OBSERVE_ONLY seed (fail-closed).
        existing = bind.execute(
            sa.text("SELECT 1 FROM system_configs WHERE key = 'systemMode'")
        ).fetchone()
        if existing is None:
            bind.execute(
                sa.text(
                    "INSERT INTO system_configs "
                    "(key, value, value_type, description, is_sensitive) "
                    "VALUES ('systemMode', 'OBSERVE_ONLY', 'system_mode', "
                    "'v2 çalışma modu', :sensitive)"
                ),
                {"sensitive": False},
            )

    if "manual_approval_requests" in tables:
        op.drop_table("manual_approval_requests")


def downgrade() -> None:
    # Cutover geri alınamaz (eski mod değerleri kaybolur). No-op.
    pass
