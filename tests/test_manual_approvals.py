import asyncio
from datetime import datetime, timedelta, timezone

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import ManualApprovalRequest
from app.models.signal import OrderType, SignalAction, SignalMode, SignalResponse
from app.services.manual_approvals import queue_response, reject_request, approve_request


def _response(request_id="approval-1"):
    return SignalResponse(requestId=request_id, symbol="THYAO", action=SignalAction.BUY, qty=10, orderType=OrderType.LIMIT, price=100, confidenceScore=80, riskScore=10, allowOrder=True, requiresConfirmation=True, reason="manual")


def test_queue_deduplicates_and_rejects():
    async def run():
        await drop_all(); await init_db()
        first = await queue_response(_response(), SignalMode.MANUAL)
        duplicate = await queue_response(_response(), SignalMode.MANUAL)
        assert first and duplicate and first.id == duplicate.id
        rejected = await reject_request(first.id, "admin", "no")
        assert rejected.status == "REJECTED" and rejected.admin_note == "no"
    asyncio.run(run())


def test_expired_approval_is_not_sent():
    async def run():
        await drop_all(); await init_db()
        async with async_session_factory() as session:
            row=ManualApprovalRequest(request_id="expired", symbol="THYAO", action="BUY", qty=1, price=100, order_type="LIMIT", expires_at=datetime.now(timezone.utc)-timedelta(minutes=1))
            session.add(row); await session.commit(); await session.refresh(row)
            row_id=row.id
        result=await approve_request(row_id,"admin")
        assert result.status == "EXPIRED"
    asyncio.run(run())
