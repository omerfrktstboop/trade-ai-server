import asyncio

import pytest
from sqlalchemy import func, select

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import OrderLog
from app.services.order_ledger import mark_send_result, reserve_order
from tests.conftest import _is_isolated_test_database


async def _reserve(request_id="phase8-1", **overrides):
    values = {
        "request_id": request_id,
        "symbol": "THYAO",
        "side": "BUY",
        "qty": 2,
        "limit_price": 100.126,
        "mode": "DEMO_LIVE",
    }
    values.update(overrides)
    async with async_session_factory() as session:
        return await reserve_order(session, **values)


@pytest.fixture(autouse=True)
async def _clean_ledger():
    await drop_all()
    await init_db()


async def test_duplicate_request_is_atomic_and_returns_existing_result():
    first, first_may_send, _ = await _reserve()
    second, second_may_send, rejection = await _reserve()
    assert first_may_send is True
    assert second_may_send is False
    assert rejection is None
    assert first.id == second.id
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count(OrderLog.id))) == 1


async def test_concurrent_duplicate_reservation_creates_one_row():
    results = await asyncio.gather(_reserve(), _reserve())
    assert sorted(result[1] for result in results) == [False, True]
    async with async_session_factory() as session:
        assert await session.scalar(select(func.count(OrderLog.id))) == 1


async def test_fingerprint_mismatch_never_creates_new_order():
    await _reserve()
    _, may_send, rejection = await _reserve(qty=3)
    assert may_send is False
    assert rejection == "requestId fingerprint mismatch"


async def test_send_unknown_is_persistent_and_never_resends():
    row, may_send, _ = await _reserve()
    assert may_send is True
    async with async_session_factory() as session:
        attached = await session.get(OrderLog, row.id)
        await mark_send_result(
            session,
            attached,
            status="SENT_PENDING",
            message="socket closed",
            uncertain=True,
        )
    restored, may_send, _ = await _reserve()
    assert may_send is False
    assert restored.status == "SEND_UNKNOWN"
    assert restored.state == "SEND_UNKNOWN"


async def test_pending_symbol_side_blocks_a_different_request():
    await _reserve()
    existing, may_send, rejection = await _reserve(request_id="phase8-2")
    assert may_send is False
    assert rejection == "pending symbol+side order exists"
    assert existing.request_id == "phase8-1"


async def test_fractional_qty_and_non_finite_financials_are_rejected():
    with pytest.raises(ValueError, match="positive integer"):
        await _reserve(qty=1.5)
    with pytest.raises(ValueError, match="finite and positive"):
        await _reserve(limit_price=float("nan"))


async def test_rounded_price_and_final_notional_use_canonical_fields():
    row, may_send, _ = await _reserve()
    assert may_send is True
    assert row.rounded_limit_price == 100.13
    assert row.order_qty * row.rounded_limit_price == pytest.approx(200.26)


def test_test_database_guard_rejects_production_urls():
    assert not _is_isolated_test_database(
        "postgresql+asyncpg://trade:secret@production.example/trade_ai"
    )
    assert not _is_isolated_test_database("sqlite+aiosqlite:///./production.db")
    assert _is_isolated_test_database("sqlite+aiosqlite:///./test.db")
    assert _is_isolated_test_database(
        "postgresql+asyncpg://trade:secret@127.0.0.1/trade_ai_test"
    )
