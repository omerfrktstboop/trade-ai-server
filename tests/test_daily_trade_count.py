"""Tests for DB-backed daily trade count resolution."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import OrderLog, RiskDecision
from app.models.signal import SignalMode, SignalRequest
from app.services.evaluator import (
    _has_explicit_daily_trade_count,
    with_resolved_daily_trade_count as _with_resolved_daily_trade_count,
)
from app.services.daily_trade_count import TRADING_TIMEZONE, get_today_trade_counts


@pytest.fixture
async def session():
    """In-memory SQLite session for service-level tests."""
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s

    await engine.dispose()


@pytest.fixture(autouse=True)
async def _reset_app_db():
    """Keep app-level DB tests isolated from the local dev database."""
    await drop_all()
    await init_db()


def _request(**kwargs) -> SignalRequest:
    defaults = {
        "requestId": "req-daily",
        "symbol": "THYAO",
        "timeframe": "1h",
        "lastPrice": 100.0,
        "open": 99.0,
        "high": 101.0,
        "low": 98.0,
        "volume": 1000.0,
        "mode": SignalMode.LIVE,
    }
    defaults.update(kwargs)
    return SignalRequest(**defaults)


def _risk_decision(
    request_id: str,
    symbol: str,
    created_at: datetime,
    *,
    allow_order: bool = True,
    action: str = "BUY",
) -> RiskDecision:
    return RiskDecision(
        request_id=request_id,
        symbol=symbol,
        action=action,
        confidence=90.0,
        risk_score=5.0,
        allow_order=allow_order,
        reason="test",
        order_type="LIMIT",
        qty=1.0,
        mode="LIVE",
        created_at=created_at,
    )


class TestDailyTradeCountService:
    async def test_counts_today_orders_and_allowed_risk_decisions(
        self, session: AsyncSession
    ):
        now = datetime(2026, 7, 7, 14, 30, tzinfo=TRADING_TIMEZONE)
        yesterday = now - timedelta(days=1)

        session.add_all(
            [
                OrderLog(
                    request_id="order-thyao",
                    symbol="THYAO",
                    action="BUY",
                    qty=1.0,
                    price=100.0,
                    status="FILLED",
                    created_at=now,
                ),
                OrderLog(
                    request_id="order-akbnk",
                    symbol="AKBNK",
                    action="SELL",
                    qty=1.0,
                    price=50.0,
                    status="PENDING",
                    created_at=now,
                ),
                OrderLog(
                    request_id="order-rejected",
                    symbol="THYAO",
                    action="BUY",
                    qty=1.0,
                    price=100.0,
                    status="REJECTED",
                    created_at=now,
                ),
                OrderLog(
                    request_id="order-yesterday",
                    symbol="THYAO",
                    action="BUY",
                    qty=1.0,
                    price=100.0,
                    status="FILLED",
                    created_at=yesterday,
                ),
                _risk_decision("risk-thyao-1", "THYAO", now),
                _risk_decision("risk-thyao-2", "THYAO", now),
                _risk_decision("risk-sise", "SISE", now),
                _risk_decision(
                    "risk-blocked",
                    "THYAO",
                    now,
                    allow_order=False,
                ),
                _risk_decision("risk-wait", "THYAO", now, action="WAIT"),
                _risk_decision("risk-old", "THYAO", yesterday),
            ]
        )
        await session.commit()

        counts = await get_today_trade_counts(session, "thyao", now=now)

        assert counts.symbol == "THYAO"
        assert counts.symbol_count == 3
        assert counts.bot_count == 5
        assert counts.effective_count == 5

    async def test_does_not_double_count_same_request_across_tables(
        self, session: AsyncSession
    ):
        now = datetime(2026, 7, 7, 14, 30, tzinfo=TRADING_TIMEZONE)
        session.add_all(
            [
                OrderLog(
                    request_id="same-request",
                    symbol="THYAO",
                    action="BUY",
                    qty=1.0,
                    price=100.0,
                    status="FILLED",
                    created_at=now,
                ),
                _risk_decision("same-request", "THYAO", now),
            ]
        )
        await session.commit()

        counts = await get_today_trade_counts(session, "THYAO", now=now)

        assert counts.symbol_count == 1
        assert counts.bot_count == 1
        assert counts.effective_count == 1


class TestSignalDailyTradeCountResolution:
    def test_detects_explicit_daily_trade_count(self):
        assert _has_explicit_daily_trade_count(_request()) is False
        assert _has_explicit_daily_trade_count(_request(dailyTradeCount=0)) is True
        assert _has_explicit_daily_trade_count(_request(daily_trade_count=0)) is True

    async def test_uses_db_count_when_request_omits_daily_trade_count(self):
        now = datetime.now(TRADING_TIMEZONE)
        async with async_session_factory() as session:
            session.add_all(
                [
                    _risk_decision("risk-1", "THYAO", now),
                    _risk_decision("risk-2", "AKBNK", now),
                    _risk_decision("risk-3", "SISE", now),
                ]
            )
            await session.commit()

        resolved = await _with_resolved_daily_trade_count(_request())

        assert resolved.daily_trade_count == 3

    async def test_keeps_request_count_when_present(self):
        now = datetime.now(TRADING_TIMEZONE)
        async with async_session_factory() as session:
            session.add(_risk_decision("risk-1", "THYAO", now))
            await session.commit()

        resolved = await _with_resolved_daily_trade_count(
            _request(dailyTradeCount=0)
        )

        assert resolved.daily_trade_count == 0
