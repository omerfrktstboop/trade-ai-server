"""Günlük parasal K/Z muhasebesi + zarar limiti testleri (v2 Faz 5).

Kurallar (ilke #7): kısmi fill'ler fill bazında; komisyonlar düşülür;
önceki günden taşınan pozisyonun gün içi oynaması limite girmez ama bugün
satılırsa realized tam sayılır; add-on lotlar bugünkü ağırlıklı maliyetle
unrealized'a girer; kısmi satış yalnızca satılan miktarı realize eder.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import OrderFill, OrderLog, PositionLifecycle, SystemConfig
from app.models.signal import (
    EntryRange,
    OrderType,
    SignalAction,
    SignalResponse,
)
from app.services.daily_pnl import (
    apply_daily_loss_limit,
    get_daily_pnl,
    is_daily_loss_limit_breached,
)

NOW = datetime.now(timezone.utc)
YESTERDAY = NOW - timedelta(days=2)


@pytest.fixture(autouse=True)
def _db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    yield


async def _seed_order_log(request_id: str, symbol: str, action: str) -> int:
    async with async_session_factory() as session:
        row = OrderLog(
            request_id=request_id,
            symbol=symbol,
            action=action,
            qty=0,
            price=0,
            status="FILLED",
            mode="DEMO_LIVE",
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _seed_fill(
    request_id: str,
    symbol: str,
    action: str,
    *,
    qty: float,
    price: float,
    fees: float = 0.0,
    filled_at: datetime | None = None,
    account_ref: str | None = None,
) -> None:
    order_log_id = await _seed_order_log(request_id, symbol, action)
    async with async_session_factory() as session:
        session.add(
            OrderFill(
                order_log_id=order_log_id,
                request_id=request_id,
                symbol=symbol,
                action=action,
                account_ref=account_ref,
                fill_qty=Decimal(str(qty)),
                fill_price=Decimal(str(price)),
                gross_value_tl=Decimal(str(qty * price)),
                commission_tl=Decimal(str(fees)),
                exchange_fee_tl=Decimal("0"),
                other_fee_tl=Decimal("0"),
                total_cost_tl=Decimal(str(fees)),
                fill_event_key=f"{request_id}-{qty}-{price}",
                filled_at=filled_at or NOW,
            )
        )
        await session.commit()


async def _seed_lifecycle(
    symbol: str,
    *,
    opened_at: datetime,
    avg_entry: float | None,
    qty: float,
    status: str = "OPEN",
    closed_at: datetime | None = None,
) -> None:
    async with async_session_factory() as session:
        session.add(
            PositionLifecycle(
                symbol=symbol,
                status=status,
                opened_at=opened_at,
                closed_at=closed_at,
                current_qty=Decimal(str(qty)),
                average_entry_price=(
                    Decimal(str(avg_entry)) if avg_entry is not None else None
                ),
            )
        )
        await session.commit()


async def _set_limit(value: str) -> None:
    async with async_session_factory() as session:
        session.add(
            SystemConfig(
                key="dailyMaxLossTl",
                value=value,
                value_type="decimal",
                description="test",
            )
        )
        await session.commit()


# ── Realized muhasebe ───────────────────────────────────────────────────────


async def test_carried_position_sold_today_realizes_full_pnl():
    """Dünden taşınan lot bugün satılırsa gerçek ortalama maliyete göre
    realized TAM sayılır."""
    await _seed_lifecycle("THYAO", opened_at=YESTERDAY, avg_entry=100.0, qty=10)
    await _seed_fill("sell-1", "THYAO", "SELL", qty=10, price=95.0, fees=5.0)

    async with async_session_factory() as session:
        pnl = await get_daily_pnl(session, price_lookup={})
    # (95-100)*10 - 5 = -55
    assert pnl.realized_tl == Decimal("-55")
    # Taşınan pozisyonun kalan oynaması unrealized'a girmez.
    assert pnl.unrealized_today_tl == Decimal("0")


async def test_partial_fills_accumulate_per_fill():
    await _seed_lifecycle("THYAO", opened_at=YESTERDAY, avg_entry=100.0, qty=10)
    await _seed_fill("sell-p1", "THYAO", "SELL", qty=4, price=98.0, fees=1.0)
    await _seed_fill("sell-p2", "THYAO", "SELL", qty=6, price=97.0, fees=1.5)

    async with async_session_factory() as session:
        pnl = await get_daily_pnl(session, price_lookup={})
    # (98-100)*4 + (97-100)*6 - 2.5 = -8 - 18 - 2.5 = -28.5
    assert pnl.realized_tl == Decimal("-28.5")


async def test_commissions_reduce_realized_even_for_buys():
    await _seed_lifecycle("THYAO", opened_at=NOW, avg_entry=100.0, qty=10)
    await _seed_fill("buy-1", "THYAO", "BUY", qty=10, price=100.0, fees=7.5)

    async with async_session_factory() as session:
        pnl = await get_daily_pnl(session, price_lookup={"THYAO": Decimal("100")})
    assert pnl.realized_tl == Decimal("-7.5")
    assert pnl.unrealized_today_tl == Decimal("0")


async def test_unknown_cost_sell_is_a_data_gap_not_fabricated():
    await _seed_lifecycle("THYAO", opened_at=YESTERDAY, avg_entry=None, qty=10)
    await _seed_fill("sell-g", "THYAO", "SELL", qty=10, price=95.0, fees=2.0)

    async with async_session_factory() as session:
        pnl = await get_daily_pnl(session, price_lookup={})
    assert pnl.realized_tl == Decimal("-2")  # sadece komisyon
    assert any("REALIZED_COST_UNKNOWN" in gap for gap in pnl.data_gaps)


# ── Unrealized muhasebe ─────────────────────────────────────────────────────


async def test_position_opened_today_counts_unrealized():
    await _seed_lifecycle("THYAO", opened_at=NOW, avg_entry=100.0, qty=10)

    async with async_session_factory() as session:
        pnl = await get_daily_pnl(session, price_lookup={"THYAO": Decimal("96")})
    assert pnl.unrealized_today_tl == Decimal("-40")


async def test_carried_position_drift_excluded_from_unrealized():
    await _seed_lifecycle("THYAO", opened_at=YESTERDAY, avg_entry=100.0, qty=10)

    async with async_session_factory() as session:
        pnl = await get_daily_pnl(session, price_lookup={"THYAO": Decimal("80")})
    assert pnl.unrealized_today_tl == Decimal("0")
    assert pnl.total_tl == Decimal("0")


async def test_addon_buy_to_carried_position_counts_at_todays_cost():
    """Taşınan pozisyona bugünkü ek alım: yalnızca add-on lotlar bugünkü
    ağırlıklı maliyetle unrealized'a girer."""
    await _seed_lifecycle("THYAO", opened_at=YESTERDAY, avg_entry=100.0, qty=15)
    await _seed_fill("buy-a1", "THYAO", "BUY", qty=3, price=110.0)
    await _seed_fill("buy-a2", "THYAO", "BUY", qty=2, price=115.0)

    async with async_session_factory() as session:
        pnl = await get_daily_pnl(session, price_lookup={"THYAO": Decimal("108")})
    # w_avg = (3*110 + 2*115)/5 = 112; (108-112)*5 = -20
    assert pnl.unrealized_today_tl == Decimal("-20")


async def test_partial_sell_leaves_remaining_cost_basis_untouched():
    """Bugün açılan pozisyonun bir kısmı satılır: satılan kısım realized,
    kalan lotlar aynı ortalama maliyetle unrealized."""
    await _seed_lifecycle("THYAO", opened_at=NOW, avg_entry=100.0, qty=6)
    await _seed_fill("sell-part", "THYAO", "SELL", qty=4, price=97.0, fees=1.0)

    async with async_session_factory() as session:
        pnl = await get_daily_pnl(session, price_lookup={"THYAO": Decimal("97")})
    # realized: (97-100)*4 - 1 = -13; unrealized: (97-100)*6... hayır — kalan
    # qty lifecycle.current_qty=6 tohumlandı (satış sonrası kalan).
    assert pnl.realized_tl == Decimal("-13")
    assert pnl.unrealized_today_tl == Decimal("-18")


async def test_missing_price_is_data_gap_not_zero():
    await _seed_lifecycle("THYAO", opened_at=NOW, avg_entry=100.0, qty=10)

    async with async_session_factory() as session:
        pnl = await get_daily_pnl(session, price_lookup={})
    assert any("UNREALIZED_PRICE_UNAVAILABLE" in gap for gap in pnl.data_gaps)


# ── Fix #3: kronolojik replay (güncel lifecycle ortalamasına GÜVENME) ───────


async def test_addon_buy_then_sell_uses_cost_at_sale_time_not_current_avg():
    """Bugün: 10@100 al, 10@120 al (ort=110), sonra 10@130 sat. Doğru realized
    kronolojik replay'e göre (130-110)*10=+200. Lifecycle güncel ortalaması
    (satıştan sonra kalan lotların ortalaması) kullanılsaydı yanlış çıkardı."""
    # Lifecycle güncel ortalaması kasıtlı olarak yanıltıcı (95) tohumlanır;
    # replay bunu kullanmamalı.
    await _seed_lifecycle("THYAO", opened_at=NOW, avg_entry=95.0, qty=10)
    await _seed_fill("buy-r1", "THYAO", "BUY", qty=10, price=100.0,
                     filled_at=NOW - timedelta(minutes=30))
    await _seed_fill("buy-r2", "THYAO", "BUY", qty=10, price=120.0,
                     filled_at=NOW - timedelta(minutes=20))
    await _seed_fill("sell-r1", "THYAO", "SELL", qty=10, price=130.0,
                     filled_at=NOW - timedelta(minutes=10))

    async with async_session_factory() as session:
        pnl = await get_daily_pnl(session, price_lookup={"THYAO": Decimal("120")})
    assert pnl.realized_tl == Decimal("200")  # (130-110)*10


async def test_two_partial_sells_at_different_prices_replay_correctly():
    await _seed_lifecycle("THYAO", opened_at=NOW, avg_entry=100.0, qty=0)
    await _seed_fill("buy-s", "THYAO", "BUY", qty=10, price=100.0,
                     filled_at=NOW - timedelta(minutes=30))
    await _seed_fill("sell-s1", "THYAO", "SELL", qty=4, price=110.0,
                     filled_at=NOW - timedelta(minutes=20))
    await _seed_fill("sell-s2", "THYAO", "SELL", qty=6, price=90.0,
                     filled_at=NOW - timedelta(minutes=10))

    async with async_session_factory() as session:
        pnl = await get_daily_pnl(session, price_lookup={})
    # (110-100)*4 + (90-100)*6 = 40 - 60 = -20
    assert pnl.realized_tl == Decimal("-20")


# ── Fix #4: hesap bazında ayrım ──────────────────────────────────────────────


async def test_daily_pnl_split_by_account_ref():
    await _seed_lifecycle("THYAO", opened_at=NOW, avg_entry=100.0, qty=0)
    # DEMO hesabında zarar, REAL hesabında kâr.
    await _seed_fill("buy-d", "THYAO", "BUY", qty=10, price=100.0,
                     filled_at=NOW - timedelta(minutes=30), account_ref="demo")
    await _seed_fill("sell-d", "THYAO", "SELL", qty=10, price=90.0,
                     filled_at=NOW - timedelta(minutes=20), account_ref="demo")
    await _seed_fill("buy-r", "THYAO", "BUY", qty=10, price=100.0,
                     filled_at=NOW - timedelta(minutes=15), account_ref="real")
    await _seed_fill("sell-r", "THYAO", "SELL", qty=10, price=115.0,
                     filled_at=NOW - timedelta(minutes=10), account_ref="real")

    async with async_session_factory() as session:
        demo = await get_daily_pnl(session, price_lookup={}, account_ref="demo")
        real = await get_daily_pnl(session, price_lookup={}, account_ref="real")
    assert demo.realized_tl == Decimal("-100")  # DEMO REAL'i etkilemez
    assert real.realized_tl == Decimal("150")


# ── Limit uygulaması ────────────────────────────────────────────────────────


async def test_limit_disabled_when_zero():
    await _seed_lifecycle("THYAO", opened_at=YESTERDAY, avg_entry=100.0, qty=10)
    await _seed_fill("sell-z", "THYAO", "SELL", qty=10, price=50.0)
    async with async_session_factory() as session:
        breached, reason = await is_daily_loss_limit_breached(
            session, price_lookup={}
        )
    assert breached is False and reason is None


async def test_realized_breach_is_fail_closed_despite_gaps():
    await _set_limit("100")
    await _seed_lifecycle("THYAO", opened_at=YESTERDAY, avg_entry=100.0, qty=20)
    await _seed_fill("sell-b", "THYAO", "SELL", qty=20, price=90.0)  # -200
    # Ek olarak fiyatı okunamayan bugünkü pozisyon → data gap var.
    await _seed_lifecycle("AKBNK", opened_at=NOW, avg_entry=50.0, qty=10)

    async with async_session_factory() as session:
        breached, reason = await is_daily_loss_limit_breached(
            session, price_lookup={}
        )
    assert breached is True
    assert "realized" in reason


async def test_unrealized_gap_fails_open_when_realized_ok():
    await _set_limit("100")
    await _seed_lifecycle("THYAO", opened_at=NOW, avg_entry=100.0, qty=10)
    async with async_session_factory() as session:
        breached, _ = await is_daily_loss_limit_breached(session, price_lookup={})
    assert breached is False


async def test_total_breach_blocks():
    await _set_limit("100")
    await _seed_lifecycle("THYAO", opened_at=NOW, avg_entry=100.0, qty=50)
    async with async_session_factory() as session:
        breached, reason = await is_daily_loss_limit_breached(
            session, price_lookup={"THYAO": Decimal("97")}
        )
    assert breached is True  # (97-100)*50 = -150
    assert "unrealizedToday" in reason


def _buy_response() -> SignalResponse:
    return SignalResponse(
        requestId="buy-veto-1",
        symbol="THYAO",
        action=SignalAction.BUY,
        qty=10,
        orderType=OrderType.LIMIT,
        price=100.0,
        confidenceScore=90.0,
        riskScore=10.0,
        allowOrder=True,
        requiresConfirmation=False,
        reason="test buy",
        entryRange=EntryRange(min=99.0, max=100.0),
        stopLoss=Decimal("95"),
        targetPrice=Decimal("110"),
    )


async def test_apply_veto_blocks_buy_on_breach():
    await _set_limit("100")
    await _seed_lifecycle("THYAO", opened_at=YESTERDAY, avg_entry=100.0, qty=20)
    await _seed_fill("sell-v", "THYAO", "SELL", qty=20, price=90.0)  # -200

    response = await apply_daily_loss_limit(_buy_response())
    assert response.action == SignalAction.WAIT
    assert response.allow_order is False
    assert "Daily loss limit" in response.reason


async def test_apply_veto_never_touches_sell():
    await _set_limit("100")
    await _seed_lifecycle("THYAO", opened_at=YESTERDAY, avg_entry=100.0, qty=20)
    await _seed_fill("sell-v2", "THYAO", "SELL", qty=20, price=90.0)

    sell = _buy_response()
    sell.action = SignalAction.SELL
    result = await apply_daily_loss_limit(sell)
    assert result.action == SignalAction.SELL
    assert result.allow_order is True
