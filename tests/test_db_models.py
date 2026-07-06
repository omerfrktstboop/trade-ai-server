"""Unit tests for SQLAlchemy ORM models — table creation + basic CRUD."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.db import AiDecision, BotPosition, LockedPosition, MarketSnapshot
from app.models.db import NewsCache, OrderLog, RiskDecision


# ── Test fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
async def engine():
    """In-memory SQLite engine — fresh per test function."""
    e = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with e.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield e
    await e.dispose()


@pytest.fixture
async def session(engine):
    """Async session backed by the in-memory engine."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with factory() as s:
        yield s


# ── Table existence ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_all_tables_created(engine):
    """Verify every expected table exists after create_all."""
    async with engine.connect() as conn:
        for name in [
            "market_snapshots",
            "ai_decisions",
            "risk_decisions",
            "order_logs",
            "bot_positions",
            "locked_positions",
            "news_cache",
        ]:
            result = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type='table' AND name=:n"),
                {"n": name},
            )
            assert result.scalar() == name, f"Table {name} not found"


# ── Insert + read per model ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_market_snapshot_insert_read(session: AsyncSession):
    snap = MarketSnapshot(
        request_id="req-001",
        symbol="BTCUSDT",
        timeframe="1h",
        open=67200.0,
        high=67800.0,
        low=67000.0,
        close=67500.0,
        volume=1234.5,
        rsi=65.2,
        ema20=67400.0,
        mode="PAPER",
    )
    session.add(snap)
    await session.commit()

    row = await session.get(MarketSnapshot, snap.id)
    assert row is not None
    assert row.symbol == "BTCUSDT"
    assert row.rsi == 65.2


@pytest.mark.asyncio
async def test_ai_decision_insert_read(session: AsyncSession):
    d = AiDecision(
        request_id="req-001",
        symbol="BTCUSDT",
        provider="deepseek",
        model="deepseek-chat",
        raw_request={"symbol": "BTCUSDT"},
        raw_response={"action": "BUY", "confidence": 85},
        action="BUY",
        confidence=85.0,
        qty=1.0,
        reason="Strong RSI buy signal",
        response_time_ms=420,
    )
    session.add(d)
    await session.commit()

    row = await session.get(AiDecision, d.id)
    assert row is not None
    assert row.action == "BUY"
    assert row.confidence == 85.0
    # JSONB stored as dict (SQLite stores as text, but SQLAlchemy deserializes)
    assert row.raw_response["action"] == "BUY"


@pytest.mark.asyncio
async def test_risk_decision_insert_read(session: AsyncSession):
    d = RiskDecision(
        request_id="req-001",
        symbol="BTCUSDT",
        action="BUY",
        confidence=85.0,
        risk_score=12.0,
        allow_order=True,
        reason="All checks passed",
        entry_min=67300.0,
        entry_max=67700.0,
        stop_loss=66000.0,
        target_price=70000.0,
        order_type="LIMIT",
        qty=1.0,
        mode="LIVE",
    )
    session.add(d)
    await session.commit()

    row = await session.get(RiskDecision, d.id)
    assert row is not None
    assert row.allow_order is True
    assert row.entry_min == 67300.0
    assert row.target_price == 70000.0


@pytest.mark.asyncio
async def test_order_log_insert_read(session: AsyncSession):
    o = OrderLog(
        request_id="req-001",
        symbol="BTCUSDT",
        action="BUY",
        qty=1.0,
        price=67500.0,
        order_id="12345",
        status="FILLED",
        mode="LIVE",
    )
    session.add(o)
    await session.commit()

    row = await session.get(OrderLog, o.id)
    assert row is not None
    assert row.status == "FILLED"
    assert row.order_id == "12345"


@pytest.mark.asyncio
async def test_bot_position_insert_read(session: AsyncSession):
    pos = BotPosition(symbol="BTCUSDT", qty=2.5, avg_price=66800.0, total_value=167000.0)
    session.add(pos)
    await session.commit()

    row = await session.get(BotPosition, pos.id)
    assert row is not None
    assert row.qty == 2.5
    assert row.avg_price == 66800.0


@pytest.mark.asyncio
async def test_locked_position_insert_read(session: AsyncSession):
    lp = LockedPosition(symbol="ASELS", qty=100.0, lock_type="LONG_TERM")
    session.add(lp)
    await session.commit()

    row = await session.get(LockedPosition, lp.id)
    assert row is not None
    assert row.symbol == "ASELS"
    assert row.lock_type == "LONG_TERM"


@pytest.mark.asyncio
async def test_news_cache_insert_read(session: AsyncSession):
    n = NewsCache(
        symbol="BTCUSDT",
        title="BTC breaks 70K",
        content="Bitcoin reached new highs today.",
        source="CoinDesk",
        url="https://example.com/news/1",
    )
    session.add(n)
    await session.commit()

    row = await session.get(NewsCache, n.id)
    assert row is not None
    assert row.title == "BTC breaks 70K"
    assert row.source == "CoinDesk"
