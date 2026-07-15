from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import AccountNormalizationAudit, OrderCashReservation, OrderLog
from app.services.account_context import MatriksAccountContextAdapter
from app.services.cash_reservation import (
    calculate_backend_reserved_cash,
    reserve_sized_buy,
    sync_cash_reservation,
)
from app.services.effective_risk_config import (
    EffectiveRiskConfigResolver,
    EnvironmentRiskLimits,
    SystemRiskConfig,
)
from app.services.position_sizing import TradeSizingContext
from app.services.trade_profile import get_static_default_profile


def _limits():
    return EffectiveRiskConfigResolver().resolve(
        environment_limits=EnvironmentRiskLimits(),
        system_config=SystemRiskConfig(),
        trade_profile=get_static_default_profile(),
    )


def _raw_account(cash="400"):
    return {
        "ok": True,
        "sourceProvider": "MATRIKS_IQ",
        "accountDataAgeSeconds": "1",
        "accountDataReliable": True,
        "account": {"TotalEquity": "100000", "OrderableCash": cash},
    }


def _trade(symbol):
    return TradeSizingContext(
        symbol=symbol,
        entry_price=Decimal("100"),
        stop_loss=Decimal("96"),
        target_price=Decimal("110"),
        confidence=Decimal("90"),
        current_price=Decimal("100"),
    )


async def _reset():
    await drop_all()
    await init_db()


async def test_partial_send_unknown_and_final_states_update_exact_remaining_cash():
    await _reset()
    async with async_session_factory() as session:
        row = OrderLog(
            request_id="lifecycle-1",
            symbol="THYAO",
            action="BUY",
            qty=10,
            order_qty=10,
            filled_qty=4,
            limit_price=100,
            rounded_limit_price=100,
            status="PARTIALLY_FILLED",
            state="PARTIALLY_FILLED",
        )
        session.add(row)
        await session.flush()
        reservation = await sync_cash_reservation(session, row)
        assert reservation is not None
        assert reservation.remaining_qty == 6
        assert reservation.reserved_amount_tl == Decimal("600")

        row.status = row.state = "SEND_UNKNOWN"
        await sync_cash_reservation(session, row)
        assert await calculate_backend_reserved_cash(session) == Decimal("600")

        for final in ("REJECTED", "CANCELED", "FILLED"):
            row.status = row.state = final
            reservation = await sync_cash_reservation(session, row)
            assert reservation is not None
            assert reservation.remaining_qty == 0
            assert reservation.reserved_amount_tl == Decimal("0")
            # Re-open only to exercise each final transition deterministically.
            row.status = row.state = "SEND_UNKNOWN"
            await sync_cash_reservation(session, row)


async def test_transaction_rollback_leaves_no_reservation():
    await _reset()
    async with async_session_factory() as session:
        row = OrderLog(
            request_id="rollback-1",
            symbol="THYAO",
            action="BUY",
            qty=1,
            order_qty=1,
            filled_qty=0,
            limit_price=100,
            rounded_limit_price=100,
            status="RESERVED",
            state="RESERVED",
        )
        session.add(row)
        await session.flush()
        await sync_cash_reservation(session, row)
        await session.rollback()
    async with async_session_factory() as session:
        assert (await session.execute(select(OrderLog))).scalars().all() == []
        assert (
            await session.execute(select(OrderCashReservation))
        ).scalars().all() == []


async def test_two_concurrent_buys_cannot_use_the_same_cash():
    await _reset()

    async def reserve(request_id, symbol):
        async with async_session_factory() as session:
            return await reserve_sized_buy(
                session,
                request_id=request_id,
                symbol=symbol,
                original_decision_qty=1,
                limit_price=Decimal("100"),
                mode="DEMO_LIVE",
                raw_account=_raw_account(),
                raw_positions=[],
                raw_open_orders=[],
                market_prices={symbol: Decimal("100")},
                trade=_trade(symbol),
                limits=_limits(),
                adapter=MatriksAccountContextAdapter(
                    reservation_handling="BACKEND_DEDUCTED",
                    max_account_data_age_seconds=Decimal("60"),
                ),
            )

    first, second = await asyncio.gather(
        reserve("concurrent-cash-1", "THYAO"),
        reserve("concurrent-cash-2", "AKBNK"),
    )
    assert sorted([first.allowed, second.allowed]) == [False, True]
    async with async_session_factory() as session:
        rows = (await session.execute(select(OrderCashReservation))).scalars().all()
        assert len(rows) == 1
        assert rows[0].reserved_amount_tl == Decimal("100")
        assert (
            len(
                (await session.execute(select(AccountNormalizationAudit)))
                .scalars()
                .all()
            )
            == 2
        )


async def test_same_request_id_cannot_create_two_cash_reservations():
    await _reset()

    async def reserve_once():
        async with async_session_factory() as session:
            return await reserve_sized_buy(
                session,
                request_id="same-request",
                symbol="THYAO",
                original_decision_qty=1,
                limit_price=Decimal("100"),
                mode="DEMO_LIVE",
                raw_account=_raw_account("1000"),
                raw_positions=[],
                raw_open_orders=[],
                market_prices={"THYAO": Decimal("100")},
                trade=_trade("THYAO"),
                limits=_limits(),
                adapter=MatriksAccountContextAdapter(
                    reservation_handling="BACKEND_DEDUCTED"
                ),
            )

    results = await asyncio.gather(reserve_once(), reserve_once())
    assert sum(result.allowed for result in results) == 1
    async with async_session_factory() as session:
        count = len(
            (await session.execute(select(OrderCashReservation))).scalars().all()
        )
        assert count == 1
