"""REAL hesap arming akışı + account watcher testleri (v2 Faz 4)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.config import settings
from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.main import app
from app.models.db import AccountEvent
from app.routers.admin import arming as arming_module
from app.services.account_watcher import AccountWatcher
from app.services.admin_config import (
    get_admin_config_value,
    set_admin_config_value,
    _parse_bool,
)

REF_A = "a" * 64
REF_B = "b" * 64
SESSION_1 = "1" * 64
SESSION_2 = "2" * 64


@pytest.fixture(autouse=True)
def _db():
    asyncio.run(drop_all())
    asyncio.run(init_db())
    yield


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.effective_admin_api_token}"}


class FakeAccountGateway:
    def __init__(self, account_type: str = "REAL", account_ref: str = REF_A) -> None:
        self.payload: dict[str, Any] = {
            "ok": True,
            "accountRef": account_ref,
            "accountSessionRef": SESSION_1,
            "accountIdMasked": "12***",
            "accountType": account_type,
            "account": {"Overall": "100000", "AvailableMargin": "50000"},
        }

    async def get_account(self) -> dict[str, Any]:
        return self.payload


def _health(
    *,
    contract: Any = 3,
    account_ref: str | None = REF_A,
    session_ref: str | None = SESSION_1,
    account_type: str = "DEMO",
) -> dict[str, Any]:
    return {
        "ok": True,
        "gatewayContractVersion": contract,
        "accountRef": account_ref,
        "accountSessionRef": session_ref,
        "accountType": account_type,
    }


async def _events(event_type: str | None = None) -> list[AccountEvent]:
    async with async_session_factory() as session:
        stmt = select(AccountEvent).order_by(AccountEvent.id)
        if event_type:
            stmt = stmt.where(AccountEvent.event_type == event_type)
        return list((await session.execute(stmt)).scalars().all())


async def _armed_state() -> tuple[bool, str]:
    async with async_session_factory() as session:
        armed = _parse_bool(
            await get_admin_config_value(session, "realAccountArmed")
        )
        ref = await get_admin_config_value(session, "armedAccountRef")
    return armed, ref.strip()


# ── Arming endpoint'leri ────────────────────────────────────────────────────


def test_arm_refused_on_demo_account(client, admin_headers, monkeypatch):
    monkeypatch.setattr(
        arming_module, "gateway_client", FakeAccountGateway(account_type="DEMO")
    )
    resp = client.post(
        "/api/admin/arm-real-account",
        headers=admin_headers,
    )
    assert resp.status_code == 409
    assert "DEMO" in resp.json()["detail"]
    armed, ref = asyncio.run(_armed_state())
    assert armed is False and ref == ""


def test_arm_stores_gateway_ref_verbatim_and_writes_event(
    client, admin_headers, monkeypatch
):
    monkeypatch.setattr(arming_module, "gateway_client", FakeAccountGateway())
    resp = client.post(
        "/api/admin/arm-real-account",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "armed"
    assert body["accountRef"] == REF_A  # yeniden hash YOK — birebir saklanır

    armed, ref = asyncio.run(_armed_state())
    assert armed is True
    assert ref == REF_A

    events = asyncio.run(_events("ARMED"))
    assert len(events) == 1
    assert events[0].account_ref == REF_A
    assert events[0].source == "ADMIN"


def test_arm_requires_admin_auth(client, monkeypatch):
    monkeypatch.setattr(arming_module, "gateway_client", FakeAccountGateway())
    resp = client.post(
        "/api/admin/arm-real-account",
    )
    assert resp.status_code == 401


def test_arm_persists_session_ref_and_type(client, admin_headers, monkeypatch):
    """Fix #2: arming, accountSessionRef ve hesap türünü de kalıcı saklar."""
    monkeypatch.setattr(arming_module, "gateway_client", FakeAccountGateway())
    client.post(
        "/api/admin/arm-real-account",
        headers=admin_headers,
    )

    async def _read():
        async with async_session_factory() as session:
            return (
                await get_admin_config_value(session, "armedAccountSessionRef"),
                await get_admin_config_value(session, "armedAccountType"),
            )

    session_ref, acct_type = asyncio.run(_read())
    assert session_ref == SESSION_1
    assert acct_type == "REAL"


def test_disarm_clears_state_and_writes_event(client, admin_headers, monkeypatch):
    monkeypatch.setattr(arming_module, "gateway_client", FakeAccountGateway())
    client.post(
        "/api/admin/arm-real-account",
        headers=admin_headers,
    )
    resp = client.post(
        "/api/admin/disarm-real-account", headers=admin_headers, json={}
    )
    assert resp.status_code == 200
    armed, ref = asyncio.run(_armed_state())
    assert armed is False and ref == ""
    events = asyncio.run(_events("DISARMED"))
    assert len(events) == 1
    assert events[0].previous_ref == REF_A


# ── Account watcher ─────────────────────────────────────────────────────────


async def _arm_directly(ref: str = REF_A, session_ref: str = SESSION_1) -> None:
    async with async_session_factory() as session:
        await set_admin_config_value(
            session,
            "realAccountArmed",
            "true",
            changed_by="test",
        )
        await set_admin_config_value(
            session, "armedAccountRef", ref, changed_by="test"
        )
        await set_admin_config_value(
            session, "armedAccountSessionRef", session_ref, changed_by="test"
        )


async def test_watcher_allows_stable_demo_account():
    watcher = AccountWatcher()
    async with async_session_factory() as session:
        first = await watcher.check(_health(), session)
        second = await watcher.check(_health(), session)
        await session.commit()
    assert first.dispatch_allowed is True
    assert second.dispatch_allowed is True
    assert await _events() == []


async def test_watcher_contract_mismatch_blocks_and_records():
    watcher = AccountWatcher()
    async with async_session_factory() as session:
        result = await watcher.check(_health(contract=1), session)
        await session.commit()
    assert result.dispatch_allowed is False
    assert "contract" in result.reason
    events = await _events("CONTRACT_MISMATCH")
    assert len(events) == 1


async def test_watcher_account_change_disarms_and_blocks():
    await _arm_directly(REF_A)
    watcher = AccountWatcher()
    async with async_session_factory() as session:
        baseline = await watcher.check(
            _health(account_type="REAL"), session
        )
        changed = await watcher.check(
            _health(account_ref=REF_B, account_type="REAL"), session
        )
        await session.commit()
    assert baseline.dispatch_allowed is True
    assert changed.dispatch_allowed is False
    assert "ACCOUNT_CHANGED" in changed.reason

    armed, ref = await _armed_state()
    assert armed is False and ref == ""
    assert len(await _events("ACCOUNT_CHANGED")) == 1
    assert len(await _events("DISARMED")) == 1


async def test_watcher_session_change_produces_session_event():
    await _arm_directly(REF_A)
    watcher = AccountWatcher()
    async with async_session_factory() as session:
        await watcher.check(_health(account_type="REAL"), session)
        changed = await watcher.check(
            _health(session_ref=SESSION_2, account_type="REAL"), session
        )
        await session.commit()
    assert changed.dispatch_allowed is False
    assert len(await _events("SESSION_CHANGED")) == 1
    armed, _ = await _armed_state()
    assert armed is False


async def test_watcher_type_change_produces_type_event():
    watcher = AccountWatcher()
    async with async_session_factory() as session:
        await watcher.check(_health(account_type="DEMO"), session)
        changed = await watcher.check(_health(account_type="REAL"), session)
        await session.commit()
    assert changed.dispatch_allowed is False
    assert len(await _events("TYPE_CHANGED")) == 1


async def test_watcher_armed_session_mismatch_auto_disarms():
    """Fix #2: aynı hesap ama farklı oturum (restart/yeniden login) → disarm.
    Baseline in-memory olmadan bile DB'deki armed session ref ile karşılaştırır."""
    await _arm_directly(REF_A, session_ref=SESSION_1)
    watcher = AccountWatcher()  # taze baseline (restart senaryosu)
    async with async_session_factory() as session:
        result = await watcher.check(
            _health(account_type="REAL", session_ref=SESSION_2), session
        )
        await session.commit()
    assert result.dispatch_allowed is False
    assert "mismatch" in result.reason
    armed, _ = await _armed_state()
    assert armed is False
    assert len(await _events("DISARMED")) == 1


async def test_watcher_armed_ref_mismatch_auto_disarms():
    """Arm edilen hesap ile canlı hesap farklıysa (restart sonrası baseline
    yokken bile) arming düşürülür ve dispatch bloklanır."""
    await _arm_directly(REF_B)  # farklı hesap arm edilmiş
    watcher = AccountWatcher()
    async with async_session_factory() as session:
        result = await watcher.check(_health(account_type="REAL"), session)
        await session.commit()
    assert result.dispatch_allowed is False
    assert "mismatch" in result.reason
    armed, _ = await _armed_state()
    assert armed is False
    assert len(await _events("DISARMED")) == 1


async def test_watcher_missing_identity_blocks_without_events():
    watcher = AccountWatcher()
    async with async_session_factory() as session:
        result = await watcher.check(
            _health(account_ref=None, account_type="UNKNOWN"), session
        )
        await session.commit()
    assert result.dispatch_allowed is False
    assert await _events() == []
