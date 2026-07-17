"""v2 systemMode emir kapısı testleri.

Emir dispatch'i TEK anahtardan gelir: systemMode=AUTO_TRADE. OBSERVE_ONLY
(default) tek başına tüm emirleri keser. Eski scannerAllowOrders/DEMO_LIVE
mod kapıları kaldırıldı.
"""

from __future__ import annotations

import asyncio

import pytest

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import RiskDecision, SystemConfig, TradeWatchlistSymbol
from app.services.admin_config import (
    RISKY_CONFIRMATION,
    get_system_mode,
    is_auto_trade,
    set_admin_config_value,
)
from app.services.scanner import SymbolScanner
from tests.fake_gateway import FakeGateway
from tests.test_scanner import make_gateway_client, make_result


@pytest.fixture(autouse=True)
def _db(monkeypatch):
    asyncio.run(drop_all())
    asyncio.run(init_db())

    async def seed():
        async with async_session_factory() as session:
            session.add(
                SystemConfig(
                    key="accountReservationHandling",
                    value="BACKEND_DEDUCTED",
                    value_type="reservation_handling",
                    description="test",
                )
            )
            # Cutoff testin koştuğu saate bağlı olmasın (17:30 sonrası koşular).
            session.add(
                SystemConfig(
                    key="disableTradingAfter",
                    value="23:59",
                    value_type="time",
                    description="test",
                )
            )
            session.add(
                TradeWatchlistSymbol(
                    symbol="THYAO",
                    is_active=True,
                    source="MANUAL_OVERRIDE",
                    manual_override=True,
                )
            )
            session.add(
                RiskDecision(
                    request_id="THYAO-20260709-120000-scan",
                    symbol="THYAO",
                    action="BUY",
                    confidence=90.0,
                    risk_score=10.0,
                    allow_order=True,
                    order_type="LIMIT",
                    qty=1,
                    mode="DEMO_LIVE",
                )
            )
            await session.commit()

    asyncio.run(seed())

    async def fake_persist(scanner_self, response, status, reason):
        return None

    monkeypatch.setattr(SymbolScanner, "_persist_order_outcome", fake_persist)
    yield


async def _set_system_mode(value: str) -> None:
    async with async_session_factory() as session:
        await set_admin_config_value(
            session,
            "systemMode",
            value,
            changed_by="test",
            confirmation=RISKY_CONFIRMATION if value == "AUTO_TRADE" else None,
        )


def _scanner(fake: FakeGateway) -> SymbolScanner:
    return SymbolScanner(gateway=make_gateway_client(fake))


async def test_default_system_mode_is_observe_only():
    async with async_session_factory() as session:
        assert await get_system_mode(session) == "OBSERVE_ONLY"
        assert await is_auto_trade(session) is False


async def test_observe_only_blocks_dispatch(monkeypatch):
    """OBSERVE_ONLY (default): diğer tüm kapılar açık (eligible + audit +
    kill switch kapalı) olsa bile systemMode tek başına emri keser."""
    fake = FakeGateway()
    fake.positions = []

    await _scanner(fake)._maybe_send_order(make_result())

    assert fake.orders == []


async def test_auto_trade_allows_dispatch(monkeypatch):
    await _set_system_mode("AUTO_TRADE")
    fake = FakeGateway()
    fake.positions = []

    await _scanner(fake)._maybe_send_order(make_result())

    assert len(fake.orders) == 1


async def test_switching_to_auto_trade_requires_confirmation():
    async with async_session_factory() as session:
        with pytest.raises(ValueError, match="confirmation"):
            await set_admin_config_value(
                session, "systemMode", "AUTO_TRADE", changed_by="test"
            )


async def test_invalid_system_mode_value_rejected():
    async with async_session_factory() as session:
        with pytest.raises(ValueError, match="OBSERVE_ONLY or AUTO_TRADE"):
            await set_admin_config_value(
                session, "systemMode", "REAL_LIVE", changed_by="test"
            )


async def test_startup_disarm_failure_hard_blocks_dispatch(monkeypatch):
    """Fix #2: startup disarm başarısızsa süreç-global sert blok devrededir;
    diğer tüm kapılar açık olsa bile hiçbir emir gönderilmez (fail-closed)."""
    from app.core import runtime_flags

    await _set_system_mode("AUTO_TRADE")
    runtime_flags.block_dispatch("test: startup disarm failed")
    try:
        fake = FakeGateway()
        fake.positions = []
        await _scanner(fake)._maybe_send_order(make_result())
        assert fake.orders == []
    finally:
        runtime_flags.clear_dispatch_block()


async def test_missing_decision_audit_blocks_dispatch(monkeypatch):
    """Audit-yoksa-emir-yok (ilke #6): risk_decisions satırı olmayan bir
    karar, diğer tüm kapılar açıkken bile gönderilemez."""
    await _set_system_mode("AUTO_TRADE")
    async with async_session_factory() as session:
        row = (
            await session.execute(
                RiskDecision.__table__.delete().where(
                    RiskDecision.request_id == "THYAO-20260709-120000-scan"
                )
            )
        )
        assert row is not None
        await session.commit()
    fake = FakeGateway()
    fake.positions = []

    await _scanner(fake)._maybe_send_order(make_result())

    assert fake.orders == []
