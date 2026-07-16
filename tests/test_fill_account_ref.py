"""Fill hesap referansı sabitleme testleri (Fix #1).

Callback fill'i, OrderLog'a emir gönderilirken yazılan sabit account_ref'i
kullanmalı — callback anındaki canlı hesabı DEĞİL. Böylece emirden sonra
hesap değişse bile fill doğru hesaba yazılır.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import OrderFill, OrderLog
from app.services.order_lifecycle import apply_callback


@pytest.fixture(autouse=True)
def _db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    yield


async def _seed_order_log_with_account(request_id: str, account_ref: str | None):
    async with async_session_factory() as session:
        session.add(
            OrderLog(
                request_id=request_id,
                symbol="THYAO",
                action="BUY",
                qty=10,
                price=100.0,
                order_qty=10,
                limit_price=100.0,
                rounded_limit_price=100.0,
                order_type="LIMIT",
                status="SENT_PENDING",
                state="SENT_PENDING",
                filled_qty=0.0,
                last_fill_qty=0.0,
                mode="DEMO_LIVE",
                account_ref=account_ref,
            )
        )
        await session.commit()


async def _fill_of(request_id: str) -> OrderFill | None:
    async with async_session_factory() as session:
        return (
            await session.execute(
                select(OrderFill).where(OrderFill.request_id == request_id)
            )
        ).scalar_one_or_none()


async def test_fill_uses_orderlog_account_ref_not_callback_account():
    await _seed_order_log_with_account("req-fixed", account_ref="demo-ref")
    async with async_session_factory() as session:
        await apply_callback(
            session,
            request_id="req-fixed",
            symbol="THYAO",
            action="BUY",
            status="FILLED",
            order_qty=10,
            filled_qty=10,
            last_fill_qty=10,
            avg_price=100.0,
            limit_price=100.0,
            order_id="ord-1",
            message="filled",
        )
    fill = await _fill_of("req-fixed")
    assert fill is not None
    # Callback sırasında canlı hesap ne olursa olsun, fill OrderLog'un
    # gönderim-anı account_ref'ini taşır.
    assert fill.account_ref == "demo-ref"


async def test_fill_account_ref_none_when_orderlog_unstamped():
    await _seed_order_log_with_account("req-none", account_ref=None)
    async with async_session_factory() as session:
        await apply_callback(
            session,
            request_id="req-none",
            symbol="THYAO",
            action="BUY",
            status="FILLED",
            order_qty=10,
            filled_qty=10,
            last_fill_qty=10,
            avg_price=100.0,
            limit_price=100.0,
            order_id="ord-2",
            message="filled",
        )
    fill = await _fill_of("req-none")
    assert fill is not None
    assert fill.account_ref is None
