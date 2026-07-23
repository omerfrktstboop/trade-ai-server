"""Flat-hesap reconciliation'ı: gateway artık tutulmayan bir sembolü
bildirdiğinde ona ait açık ``PositionLifecycle``'ı kapatır.

``position_sync`` gateway snapshot'ını doğruladıktan (confidence HIGH/MEDIUM +
geçerli 64 karakterlik ``accountRef``) sonra ``bot_positions`` tablosunda
snapshot'ta olmayan satırları siliyordu; fakat ``position_lifecycles``
dokunulmadan kalıyordu. Sonuç: kullanıcı bir pozisyonu **bot dışında** (manuel)
tasfiye ettiğinde bot tarafında SELL fill oluşmadığı için lifecycle sonsuza
kadar ``OPEN`` kalıyor, hesap flat olsa bile.

Bu modül o asimetriyi kapatır. Kapanış "flat'e eşitleme"dir: çıkış fiyatları
bot tarafından ölçülemediği için gerçekleşen P&L doğrulanmış sayılmaz
(``pnl_verified=False``) ve satır ``RECONCILED`` / ``RECONCILIATION`` olarak
işaretlenir; ``performance_report`` strateji metriklerine bu tür lifecycle'ları
varsayılan olarak dahil etmez.

Güvenlik: fonksiyon **sahiplik kümesini kendisi çekmez** — çağıran, gateway'in
doğrulanmış snapshot'ından türettiği ``owned_symbols`` kümesini geçmek
zorundadır. Böylece kapanış yalnızca ``position_sync``'in ``bot_positions``
satırını silmeye güvendiği tam koşulda (aynı doğrulanmış snapshot) tetiklenir;
geçici/şüpheli bir snapshot asla lifecycle kapatamaz.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import PositionLifecycle

logger = logging.getLogger(__name__)

# data_quality yalnızca "kötüye" gidebilir: BACKFILL_UNAVAILABLE ya da zaten
# MANUAL_REVIEW olan bir lifecycle reconciliation ile RECONCILED'a
# "yükseltilmemeli". Sadece bu iki temiz durumdan RECONCILED'a geçilir.
_RECONCILABLE_QUALITY = frozenset({"VERIFIED", "PARTIAL"})


async def reconcile_open_lifecycles_to_ownership(
    session: AsyncSession,
    owned_symbols: Collection[str],
    *,
    reason: str,
) -> list[PositionLifecycle]:
    """Sahiplikte olmayan her OPEN lifecycle'ı flat'e eşitleyip kapat.

    Args:
        session: Aktif async oturum. Bu fonksiyon **commit yapmaz** — çağıran
            tek transaction içinde ``bot_positions`` temizliğiyle birlikte
            commit'ler (atomiklik).
        owned_symbols: Gateway'in doğrulanmış snapshot'ından türetilen, hâlâ
            tutulan semboller. Boş küme = hesap tamamen flat.
        reason: ``backfill_reason``'a yazılacak insan-okur açıklama.

    Returns:
        Kapatılan lifecycle nesneleri (audit/log için). Kapatılacak bir şey
        yoksa boş liste.
    """
    owned = {str(s).strip().upper() for s in owned_symbols}
    open_lifecycles = (
        (
            await session.execute(
                select(PositionLifecycle).where(PositionLifecycle.status == "OPEN")
            )
        )
        .scalars()
        .all()
    )

    closed: list[PositionLifecycle] = []
    now = datetime.now(timezone.utc)
    for lifecycle in open_lifecycles:
        if str(lifecycle.symbol).strip().upper() in owned:
            continue

        lifecycle.status = "CLOSED"
        lifecycle.closed_at = now
        lifecycle.current_qty = Decimal("0")
        # Çıkış bot dışında gerçekleştiği için gerçekleşen P&L bot fill'lerinden
        # tam ölçülemez; strateji metriklerine girmemeli.
        lifecycle.pnl_verified = False
        lifecycle.is_backfilled = True
        lifecycle.backfill_reason = reason
        lifecycle.measurement_source = "RECONCILIATION"
        if lifecycle.data_quality in _RECONCILABLE_QUALITY:
            lifecycle.data_quality = "RECONCILED"

        closed.append(lifecycle)
        logger.warning(
            "LIFECYCLE_RECONCILED_TO_FLAT symbol=%s lifecycleId=%s "
            "recordedQty=%s reason=%s",
            lifecycle.symbol,
            lifecycle.id,
            lifecycle.current_qty,
            reason,
        )

    if closed:
        await session.flush()
    return closed
