"""Versiyonlu exit politikası + ExitIntent kayıt yardımcıları (Plan Faz 2.2).

Plan bölüm 6'daki hızlı-çıkış hipotezini R (risk katı) cinsinden tek bir
versiyonlanmış politika olarak toplar. R = (fiyat − entry) / (entry − stop);
tüm eşikler bu birimdedir. Politika versiyonu ExitIntent'e yazılır ki sonuç
ölçümü (Faz 3) hangi parametre setiyle üretildiğini bilebilsin.

Bu modül karar VERMEZ ve emir GÖNDERMEZ; yalnızca politikayı ve ExitIntent
CRUD'unu sağlar. Karar mantığı ``position_exit_monitor``'dadır.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import ExitIntent


@dataclass(frozen=True)
class ExitPolicy:
    """R-bazlı deterministik çıkış politikası (plan bölüm 6 başlangıç hipotezi)."""

    version: str = "exit-policy-v1"
    hard_target_r: float = 1.7
    breakeven_activation_r: float = 0.8
    trailing_activation_r: float = 1.3
    trailing_distance_r: float = 0.6
    stagnation_minutes: float = 15.0
    # Durgunluk çıkışı yalnızca MFE bu R'nin altında kaldıysa tetiklenir.
    stagnation_mfe_r_ceiling: float = 0.3
    max_holding_minutes: float = 60.0
    entry_order_timeout_seconds: float = 60.0
    # Acil çıkış (stop/zaman) emri dolmazsa bu süre sonra cancel/reprice.
    urgent_reprice_seconds: float = 15.0
    # Pasif kâr-al emri dolmazsa bu süre sonra cancel/reprice.
    passive_reprice_seconds: float = 90.0


DEFAULT_EXIT_POLICY = ExitPolicy()


def get_active_exit_policy() -> ExitPolicy:
    """Aktif exit politikasını döndür.

    Şimdilik dondurulmuş varsayılan (v1). İleride config/TradeProfile'dan
    çözülecek; ExitIntent politika versiyonunu sakladığı için sürüm geçişleri
    ölçümde ayrıştırılabilir kalır.
    """
    return DEFAULT_EXIT_POLICY


async def record_exit_intent(
    session: AsyncSession,
    *,
    symbol: str,
    exit_reason: str,
    trigger_price: Decimal | None,
    policy_version: str,
    position_lifecycle_id: int | None = None,
    request_id: str | None = None,
    order_id: str | None = None,
    status: str = "ACCEPTED",
) -> ExitIntent:
    """Bir çıkış niyeti kaydı oluştur (çağıran commit'ler)."""
    intent = ExitIntent(
        symbol=symbol.upper(),
        exit_reason=exit_reason,
        trigger_price=trigger_price,
        trigger_at=datetime.now(timezone.utc),
        policy_version=policy_version,
        position_lifecycle_id=position_lifecycle_id,
        request_id=request_id,
        order_id=order_id,
        status=status,
    )
    session.add(intent)
    await session.flush()
    return intent


async def open_exit_intents(
    session: AsyncSession, symbol: str | None = None
) -> list[ExitIntent]:
    """Henüz kapanmamış (ACCEPTED/PARTIAL) çıkış niyetleri."""
    stmt = select(ExitIntent).where(ExitIntent.status.in_(("ACCEPTED", "PARTIAL")))
    if symbol is not None:
        stmt = stmt.where(ExitIntent.symbol == symbol.upper())
    return list((await session.execute(stmt)).scalars().all())


async def update_exit_intent_status(
    session: AsyncSession,
    intent: ExitIntent,
    *,
    status: str,
    order_id: str | None = None,
    bump_generation: bool = False,
) -> None:
    """Çıkış niyetinin durumunu güncelle (çağıran commit'ler)."""
    intent.status = status
    if order_id is not None:
        intent.order_id = order_id
    if bump_generation:
        intent.cancel_reprice_generation += 1
    await session.flush()
