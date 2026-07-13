from __future__ import annotations

import asyncio
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.base import Base
from app.models.db import PositionSizingAudit


def test_sqlite_position_sizing_audit_preserves_decimal_precision():
    async def run() -> None:
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        exact = Decimal("123.1234567890")
        async with factory() as session:
            session.add(
                PositionSizingAudit(
                    request_id="decimal-1",
                    symbol="THYAO",
                    trade_profile_version=1,
                    system_config_version="v1",
                    environment_config_fingerprint="a" * 64,
                    risk_per_trade_pct=Decimal("0.5000000000"),
                    risk_budget_tl=exact,
                    raw_stop_distance_tl=Decimal("5.0000000000"),
                    slippage_buffer_tl=Decimal("0.1234567890"),
                    effective_stop_distance_tl=Decimal("5.1234567890"),
                    final_qty=24,
                    order_value_tl=Decimal("2400.0000000000"),
                    estimated_loss_at_stop_tl=Decimal("122.9629629360"),
                    binding_limits=["risk_budget"],
                    allowed=True,
                    reason="test",
                    effective_risk_config={"risk": "0.5"},
                    calculation_details={"qty_by_risk": 24},
                )
            )
            await session.commit()
        async with factory() as session:
            row = (await session.execute(select(PositionSizingAudit))).scalar_one()
            assert row.risk_budget_tl == exact
            assert row.slippage_buffer_tl == Decimal("0.1234567890")
        await engine.dispose()

    asyncio.run(run())
