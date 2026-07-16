"""Günlük parasal K/Z ve zarar limiti (v2 Faz 5, ilke #7).

Muhasebe kuralları:

- **Realized (bugün):** bugünkü SELL ``order_fills`` satırları üzerinden,
  satılan lotların fill anındaki lifecycle ortalama maliyetine göre
  ``(fill_price − avg_entry) × qty``; bugünkü TÜM fill'lerin komisyon +
  borsa + diğer ücretleri düşülür. Kısmi fill'ler zaten fill başına ayrı
  satırdır. Önceki günden taşınan lot bugün satılırsa realized TAM sayılır
  (gerçek ortalama maliyete göre).
- **Unrealized (sadece bugünkü lotlar):** bugün AÇILAN lifecycle'lar tam
  pozisyonlarıyla; önceki günden taşınan lifecycle'larda yalnızca bugünkü
  ek alım (add-on) lotları (bugünkü BUY fill'lerinin ağırlıklı ortalaması,
  kalan pozisyonla sınırlı). Taşınan lotların gün içi değer oynaması limite
  DAHİL DEĞİLDİR.

Fail yönü: realized tek başına limiti aşıyorsa fail-closed (veri boşluğu
olsa bile BUY bloklanır); unrealized verisi eksikse fail-open + yüksek sesli
log. SELL ve stop-loss guard bu limitten HİÇBİR ZAMAN etkilenmez — zararlı
günde çıkış her zaman mümkündür.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.db import OrderFill, PositionLifecycle
from app.models.signal import SignalAction, SignalResponse
from app.services.admin_config import get_admin_config_value

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailyPnl:
    realized_tl: Decimal
    unrealized_today_tl: Decimal
    total_tl: Decimal
    #: Hesaplanamayan parçaların gerekçeleri (fiyat yok, maliyet yok, ...).
    data_gaps: tuple[str, ...] = ()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _day_start_utc(session: AsyncSession) -> datetime:
    """İşlem gününün başlangıcı (admin timezone'unda yerel gece yarısı)."""
    try:
        tz = ZoneInfo(await get_admin_config_value(session, "timezone"))
    except Exception:
        tz = ZoneInfo("Europe/Istanbul")
    local_now = datetime.now(tz)
    return datetime.combine(local_now.date(), time.min, tzinfo=tz).astimezone(
        timezone.utc
    )


async def _lifecycle_covering(
    session: AsyncSession, symbol: str, at: datetime
) -> PositionLifecycle | None:
    """Verilen an için geçerli lifecycle (satışın maliyet bazı)."""
    stmt = (
        select(PositionLifecycle)
        .where(
            PositionLifecycle.symbol == symbol,
            PositionLifecycle.opened_at <= at,
        )
        .order_by(PositionLifecycle.opened_at.desc())
        .limit(1)
    )
    row = (await session.execute(stmt)).scalars().first()
    if row is None:
        return None
    if row.closed_at is not None and _aware(row.closed_at) < at:
        return None
    return row


async def get_daily_pnl(
    session: AsyncSession,
    gateway=None,
    *,
    price_lookup: dict[str, Decimal] | None = None,
) -> DailyPnl:
    """Bugünün realized + (bugünkü lotların) unrealized K/Z'si.

    ``price_lookup`` verilirse canlı fiyatlar oradan okunur (test/veri
    enjeksiyonu); yoksa ``gateway.get_snapshot`` kullanılır; o da yoksa
    unrealized tarafı data-gap olarak raporlanır.
    """
    day_start = await _day_start_utc(session)
    gaps: list[str] = []

    fills_today = list(
        (
            await session.execute(
                select(OrderFill)
                .where(OrderFill.filled_at >= day_start)
                .order_by(OrderFill.filled_at.asc(), OrderFill.id.asc())
            )
        )
        .scalars()
        .all()
    )

    realized = Decimal("0")
    fees = Decimal("0")
    for fill in fills_today:
        fees += (
            (fill.commission_tl or 0)
            + (fill.exchange_fee_tl or 0)
            + (fill.other_fee_tl or 0)
        )
        if fill.action != "SELL":
            continue
        lifecycle = await _lifecycle_covering(
            session, fill.symbol, _aware(fill.filled_at)
        )
        avg_entry = lifecycle.average_entry_price if lifecycle is not None else None
        if avg_entry is None:
            gaps.append(f"REALIZED_COST_UNKNOWN:{fill.symbol}:{fill.request_id}")
            continue
        realized += (fill.fill_price - avg_entry) * fill.fill_qty
    realized -= fees

    # ── Unrealized: sadece bugün açılan/eklenen lotlar ─────────────────────
    open_rows = list(
        (
            await session.execute(
                select(PositionLifecycle).where(PositionLifecycle.status == "OPEN")
            )
        )
        .scalars()
        .all()
    )

    async def _price_for(symbol: str) -> Decimal | None:
        if price_lookup is not None and symbol in price_lookup:
            return price_lookup[symbol]
        if gateway is None:
            return None
        try:
            snapshot = await gateway.get_snapshot(symbol)
            payload = snapshot.get("payload") or {}
            raw = payload.get("lastPrice")
            if payload.get("quoteReliable") is not True or not raw:
                return None
            return Decimal(str(raw))
        except Exception:
            return None

    unrealized = Decimal("0")
    for row in open_rows:
        symbol = row.symbol.strip().upper()
        qty = Decimal(str(row.current_qty or 0))
        if qty <= 0:
            continue
        opened_today = _aware(row.opened_at) >= day_start
        if opened_today:
            if row.average_entry_price is None:
                gaps.append(f"UNREALIZED_COST_UNKNOWN:{symbol}")
                continue
            price = await _price_for(symbol)
            if price is None:
                gaps.append(f"UNREALIZED_PRICE_UNAVAILABLE:{symbol}")
                continue
            unrealized += (price - row.average_entry_price) * qty
            continue

        # Taşınan pozisyon: yalnızca bugünkü add-on BUY lotları sayılır.
        todays_buys = [
            fill
            for fill in fills_today
            if fill.symbol == symbol and fill.action == "BUY"
        ]
        if not todays_buys:
            continue
        buy_qty = sum((fill.fill_qty for fill in todays_buys), Decimal("0"))
        if buy_qty <= 0:
            continue
        weighted_cost = sum(
            (fill.fill_qty * fill.fill_price for fill in todays_buys), Decimal("0")
        )
        w_avg = weighted_cost / buy_qty
        counted_qty = min(buy_qty, qty)
        price = await _price_for(symbol)
        if price is None:
            gaps.append(f"UNREALIZED_PRICE_UNAVAILABLE:{symbol}")
            continue
        unrealized += (price - w_avg) * counted_qty

    return DailyPnl(
        realized_tl=realized,
        unrealized_today_tl=unrealized,
        total_tl=realized + unrealized,
        data_gaps=tuple(gaps),
    )


async def is_daily_loss_limit_breached(
    session: AsyncSession,
    gateway=None,
    *,
    price_lookup: dict[str, Decimal] | None = None,
) -> tuple[bool, str | None]:
    """(aşıldı mı, gerekçe). Limit 0/geçersiz → devre dışı (False)."""
    try:
        raw_limit = await get_admin_config_value(session, "dailyMaxLossTl")
        limit = Decimal(str(raw_limit))
    except (InvalidOperation, ValueError, TypeError):
        logger.warning("dailyMaxLossTl unreadable — limit disabled")
        return False, None
    if limit <= 0:
        return False, None

    pnl = await get_daily_pnl(session, gateway, price_lookup=price_lookup)

    # Realized tek başına aşıyorsa fail-closed — veri boşlukları önemsiz.
    if pnl.realized_tl <= -limit:
        return True, (
            f"daily realized loss {pnl.realized_tl} TL breaches "
            f"dailyMaxLossTl={limit}"
        )
    if pnl.data_gaps:
        logger.warning(
            "DAILY_PNL_DATA_GAP — unrealized incomplete, fail-open gaps=%s "
            "realized=%s",
            ";".join(pnl.data_gaps),
            pnl.realized_tl,
        )
        return False, None
    if pnl.total_tl <= -limit:
        return True, (
            f"daily loss {pnl.total_tl} TL (realized={pnl.realized_tl}, "
            f"unrealizedToday={pnl.unrealized_today_tl}) breaches "
            f"dailyMaxLossTl={limit}"
        )
    return False, None


async def apply_daily_loss_limit(
    response: SignalResponse, *, gateway=None
) -> SignalResponse:
    """Pipeline sonrası BUY vetosu (news_risk_lock deseniyle aynı).

    Sadece emre dönüşebilecek BUY'ları keser; SELL/WAIT ve stop-loss guard
    yolu hiçbir koşulda etkilenmez. İç hata → fail-open (log'la, karari
    değiştirme) — realized-tabanlı fail-closed yön zaten
    ``is_daily_loss_limit_breached`` içindedir.
    """
    if response.action != SignalAction.BUY:
        return response
    if not (response.allow_order or response.requires_confirmation):
        return response
    try:
        async with async_session_factory() as session:
            breached, reason = await is_daily_loss_limit_breached(session, gateway)
    except Exception:
        logger.exception("Daily loss limit check failed — fail-open")
        return response
    if not breached:
        return response
    logger.warning(
        "DAILY_LOSS_LIMIT_BLOCKED_BUY symbol=%s requestId=%s reason=%s",
        response.symbol,
        response.request_id,
        reason,
    )
    response.action = SignalAction.WAIT
    response.allow_order = False
    response.requires_confirmation = False
    response.qty = 0
    response.reason = f"Daily loss limit: {reason} | {response.reason}"
    return response
