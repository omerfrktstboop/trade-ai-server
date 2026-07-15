from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from decimal import Decimal

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.services.order_ledger import reserve_order
from app.services.account_context import MatriksAccountContextAdapter
from app.services.cash_reservation import reserve_sized_buy
from app.services.cash_reservation import sync_cash_reservation
from app.models.db import OrderCashReservation, OrderLog
from sqlalchemy import select
from app.services.effective_risk_config import (
    EffectiveRiskConfigResolver,
    EnvironmentRiskLimits,
    SystemRiskConfig,
)
from app.services.position_sizing import TradeSizingContext
from app.services.trade_profile import get_static_default_profile


POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")
pytestmark = pytest.mark.skipif(
    not POSTGRES_URL, reason="requires isolated TEST_POSTGRES_URL"
)


def _alembic(*arguments: str) -> None:
    subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        check=True,
        env={**os.environ, "DATABASE_URL": POSTGRES_URL},
    )


async def _prepare_legacy_schema() -> None:
    engine = create_async_engine(POSTGRES_URL)
    async with engine.begin() as connection:
        await connection.execute(text("DROP SCHEMA public CASCADE"))
        await connection.execute(text("CREATE SCHEMA public"))
        await connection.execute(
            text(
                """
                CREATE TABLE order_logs (
                    id SERIAL PRIMARY KEY,
                    request_id VARCHAR(64) NOT NULL,
                    symbol VARCHAR(16) NOT NULL,
                    action VARCHAR(8) NOT NULL,
                    qty DOUBLE PRECISION,
                    price DOUBLE PRECISION,
                    order_id VARCHAR(64),
                    status VARCHAR(32),
                    mode VARCHAR(16),
                    matrix_message TEXT,
                    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
        )
        await connection.execute(
            text(
                """
                INSERT INTO order_logs
                    (request_id, symbol, action, qty, price, status, matrix_message)
                VALUES
                    ('duplicate-1', 'THYAO', 'BUY', 1, 100, 'SENT_PENDING', 'sent'),
                    ('duplicate-1', 'THYAO', 'BUY', 2, 100, 'FILLED', 'filled')
                """
            )
        )
    await engine.dispose()


async def _verify_upgrade_and_concurrent_upsert() -> None:
    engine = create_async_engine(POSTGRES_URL)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.connect() as connection:
        duplicate_count = await connection.scalar(
            text("SELECT count(*) FROM order_logs WHERE request_id='duplicate-1'")
        )
        merged = (
            await connection.execute(
                text(
                    "SELECT status, state, order_qty FROM order_logs "
                    "WHERE request_id='duplicate-1'"
                )
            )
        ).one()
        assert duplicate_count == 1
        assert merged.status == "FILLED"
        assert merged.state == "FILLED"
        assert merged.order_qty == 2
        await connection.execute(
            text(
                """
                INSERT INTO position_sizing_audits (
                    request_id, symbol, trade_profile_version,
                    system_config_version, environment_config_fingerprint,
                    risk_per_trade_pct, risk_budget_tl, raw_stop_distance_tl,
                    slippage_buffer_tl, effective_stop_distance_tl, final_qty,
                    order_value_tl, estimated_loss_at_stop_tl, binding_limits,
                    allowed, reason, effective_risk_config, calculation_details
                ) VALUES (
                    'decimal-pg', 'THYAO', 1, 'v1', :fingerprint,
                    0.5000000000, 123.1234567890, 5.0000000000,
                    0.1234567890, 5.1234567890, 24,
                    2400.0000000000, 122.9629629360, '[]',
                    true, 'test', '{}', '{}'
                )
                """
            ),
            {"fingerprint": "a" * 64},
        )
        exact = await connection.scalar(
            text(
                "SELECT risk_budget_tl FROM position_sizing_audits "
                "WHERE request_id='decimal-pg'"
            )
        )
        assert exact == Decimal("123.1234567890")
        with pytest.raises(IntegrityError):
            await connection.execute(
                text(
                    "INSERT INTO order_logs "
                    "(request_id, symbol, action) VALUES "
                    "('duplicate-1', 'THYAO', 'BUY')"
                )
            )

    async def reserve():
        async with factory() as session:
            return await reserve_order(
                session,
                request_id="concurrent-1",
                symbol="AKBNK",
                side="BUY",
                qty=1,
                limit_price=50,
                mode="DEMO_LIVE",
            )

    results = await asyncio.gather(reserve(), reserve())
    assert sorted(result[1] for result in results) == [False, True]

    limits = EffectiveRiskConfigResolver().resolve(
        environment_limits=EnvironmentRiskLimits(),
        system_config=SystemRiskConfig(),
        trade_profile=get_static_default_profile(),
    )

    async def reserve_cash(request_id: str, symbol: str):
        async with factory() as session:
            return await reserve_sized_buy(
                session,
                request_id=request_id,
                symbol=symbol,
                original_decision_qty=1,
                limit_price=Decimal("100"),
                mode="DEMO_LIVE",
                raw_account={
                    "sourceProvider": "MATRIKS_IQ",
                    "accountDataAgeSeconds": "1",
                    "accountDataReliable": True,
                    "account": {
                        "TotalEquity": "100000",
                        "OrderableCash": "450",
                    },
                },
                raw_positions=[],
                raw_open_orders=[],
                market_prices={symbol: Decimal("100")},
                trade=TradeSizingContext(
                    symbol=symbol,
                    entry_price=Decimal("100"),
                    stop_loss=Decimal("96"),
                    target_price=Decimal("110"),
                    confidence=Decimal("90"),
                    current_price=Decimal("100"),
                ),
                limits=limits,
                adapter=MatriksAccountContextAdapter(
                    reservation_handling="BACKEND_DEDUCTED"
                ),
            )

    cash_results = await asyncio.gather(
        reserve_cash("postgres-cash-1", "THYAO"),
        reserve_cash("postgres-cash-2", "EREGL"),
    )
    assert sorted(result.allowed for result in cash_results) == [False, True]

    async with factory() as session:
        rollback_row = OrderLog(
            request_id="postgres-rollback-reservation",
            symbol="BIMAS",
            action="BUY",
            qty=1,
            order_qty=1,
            filled_qty=0,
            limit_price=100,
            rounded_limit_price=100,
            status="RESERVED",
            state="RESERVED",
        )
        session.add(rollback_row)
        await session.flush()
        await sync_cash_reservation(session, rollback_row)
        await session.rollback()
    async with factory() as session:
        leaked = (
            await session.execute(
                select(OrderCashReservation).where(
                    OrderCashReservation.request_id == "postgres-rollback-reservation"
                )
            )
        ).scalar_one_or_none()
        assert leaked is None
    await engine.dispose()


async def _verify_rollback() -> None:
    engine = create_async_engine(POSTGRES_URL)
    async with engine.connect() as connection:
        columns = {
            row[0]
            for row in (
                await connection.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_name='order_logs'"
                    )
                )
            )
        }
        assert "request_fingerprint" not in columns
        assert "order_qty" not in columns
        tables = {
            row[0]
            for row in (
                await connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema='public'"
                    )
                )
            )
        }
        assert "position_sizing_audits" not in tables
        assert "order_cash_reservations" not in tables
        assert "account_normalization_audits" not in tables
        assert "account_reservation_scopes" not in tables
    await engine.dispose()


def test_postgresql_upgrade_cleanup_unique_rollback_and_upsert():
    asyncio.run(_prepare_legacy_schema())
    _alembic("upgrade", "head")
    asyncio.run(_verify_upgrade_and_concurrent_upsert())
    _alembic("downgrade", "base")
    asyncio.run(_verify_rollback())
    _alembic("upgrade", "head")
