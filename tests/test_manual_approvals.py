import asyncio
from datetime import datetime, timedelta, timezone

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import ManualApprovalRequest, SystemConfig
from app.models.signal import OrderType, SignalAction, SignalMode, SignalResponse
from app.services.manual_approvals import (
    queue_response,
    reject_request,
    approve_request,
)
import app.services.manual_approvals as approvals_service
import pytest


def _response(request_id="approval-1"):
    return SignalResponse(
        requestId=request_id,
        symbol="THYAO",
        action=SignalAction.BUY,
        qty=10,
        orderType=OrderType.LIMIT,
        price=100,
        confidenceScore=80,
        riskScore=10,
        allowOrder=True,
        requiresConfirmation=True,
        reason="manual",
    )


def test_queue_deduplicates_and_rejects():
    async def run():
        await drop_all()
        await init_db()
        first = await queue_response(_response(), SignalMode.MANUAL)
        duplicate = await queue_response(_response(), SignalMode.MANUAL)
        assert first and duplicate and first.id == duplicate.id
        rejected = await reject_request(first.id, "admin", "no")
        assert rejected.status == "REJECTED" and rejected.admin_note == "no"

    asyncio.run(run())


def test_expired_approval_is_not_sent():
    async def run():
        await drop_all()
        await init_db()
        async with async_session_factory() as session:
            row = ManualApprovalRequest(
                request_id="expired",
                symbol="THYAO",
                action="BUY",
                qty=1,
                price=100,
                order_type="LIMIT",
                expires_at=datetime.now(timezone.utc) - timedelta(minutes=1),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            row_id = row.id
        result = await approve_request(row_id, "admin")
        assert result.status == "EXPIRED"

    asyncio.run(run())


class FakeGateway:
    def __init__(self):
        self.sent = []

    async def health(self):
        return {"positionsLoaded": True}

    async def send_order(self, **kwargs):
        self.sent.append(kwargs)
        return {"accepted": True, "status": "SENT_PENDING", "reason": "ok"}


async def _approval_with_config(**overrides):
    values = {
        "killSwitchEnabled": "false",
        "tradingMode": "DEMO_LIVE",
        "botMode": "DEMO_LIVE",
        "botEnableDemoOrders": "true",
        "botDemoAccountConfirmed": "true",
    }
    values.update(overrides)
    async with async_session_factory() as session:
        for key, value in values.items():
            session.add(SystemConfig(key=key, value=value, value_type="string"))
        row = ManualApprovalRequest(
            request_id="gated",
            symbol="THYAO",
            action="BUY",
            qty=1,
            price=100,
            order_type="LIMIT",
            expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


@pytest.mark.parametrize(
    ("setting_enabled", "override", "expected_reason"),
    [
        (False, {}, "MANUAL_APPROVAL_ALLOW_ORDERS"),
        (True, {"killSwitchEnabled": "true"}, "kill switch"),
        (True, {"tradingMode": "PAPER", "botMode": "PAPER"}, "not DEMO_LIVE"),
        (True, {"botEnableDemoOrders": "false"}, "not enabled"),
        (True, {"botDemoAccountConfirmed": "false"}, "not confirmed"),
        (True, {"tradingMode": "REAL_LIVE"}, "forbidden"),
    ],
)
def test_approval_gates_do_not_call_gateway(
    monkeypatch, setting_enabled, override, expected_reason
):
    async def run():
        await drop_all()
        await init_db()
        gateway = FakeGateway()
        monkeypatch.setattr(
            approvals_service.settings, "manual_approval_allow_orders", setting_enabled
        )
        monkeypatch.setattr(approvals_service, "gateway_client", gateway)
        row_id = await _approval_with_config(**override)
        result = await approve_request(row_id, "admin")
        assert result.status == "REJECTED"
        assert expected_reason in result.admin_note
        assert gateway.sent == []

    asyncio.run(run())


def test_approval_success_sends_only_demo_live_limit(monkeypatch):
    async def run():
        await drop_all()
        await init_db()
        gateway = FakeGateway()
        monkeypatch.setattr(
            approvals_service.settings, "manual_approval_allow_orders", True
        )
        monkeypatch.setattr(approvals_service, "gateway_client", gateway)
        row_id = await _approval_with_config()
        result = await approve_request(row_id, "admin")
        assert result.status == "SENT_PENDING"
        assert len(gateway.sent) == 1
        assert gateway.sent[0]["mode"] == "DEMO_LIVE"
        assert "order_type" not in gateway.sent[0]

    asyncio.run(run())


def test_sending_approval_cannot_be_rejected():
    async def run():
        await drop_all()
        await init_db()
        async with async_session_factory() as session:
            row = ManualApprovalRequest(
                request_id="sending",
                symbol="THYAO",
                action="BUY",
                qty=1,
                price=100,
                order_type="LIMIT",
                status="SENDING",
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            row_id = row.id
        result = await reject_request(row_id, "admin", "too late")
        assert result.status == "SENDING"
        assert result.rejected_by is None

    asyncio.run(run())
