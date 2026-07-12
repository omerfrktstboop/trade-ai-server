from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update

from app.config import settings
from app.db.session import async_session_factory
from app.models.db import ManualApprovalRequest, OrderLog
from app.models.signal import OrderType, SignalAction, SignalMode
from app.services.matriks_gateway import GatewayError, GatewayUnavailable, gateway_client
from app.services.admin_config import get_admin_config_value


async def queue_response(response, mode: SignalMode, source: str = "SCANNER") -> ManualApprovalRequest | None:
    if response.action not in (SignalAction.BUY, SignalAction.SELL) or response.order_type != OrderType.LIMIT or response.qty <= 0 or not response.price or response.price <= 0:
        return None
    async with async_session_factory() as session:
        existing = (await session.execute(select(ManualApprovalRequest).where(ManualApprovalRequest.request_id == response.request_id))).scalar_one_or_none()
        if existing:
            return existing
        pending = (await session.execute(select(ManualApprovalRequest).where(ManualApprovalRequest.symbol == response.symbol, ManualApprovalRequest.action == response.action, ManualApprovalRequest.status == "PENDING_APPROVAL"))).scalar_one_or_none()
        if pending:
            return pending
        row = ManualApprovalRequest(request_id=response.request_id, symbol=response.symbol, action=response.action.value, qty=response.qty, price=response.price, order_type="LIMIT", confidence=response.confidence_score, risk_score=response.risk_score, reason=response.reason, source=source, status="PENDING_APPROVAL", expires_at=datetime.now(timezone.utc) + timedelta(minutes=10), raw_response_json=response.model_dump(mode="json"))
        session.add(row); await session.commit(); await session.refresh(row)
        return row


async def approve_request(request_id: int, approved_by: str, note: str | None = None) -> ManualApprovalRequest:
    async with async_session_factory() as session:
        row = await session.get(ManualApprovalRequest, request_id)
        if row is None:
            raise ValueError("Approval not found")
        now = datetime.now(timezone.utc)
        expires = row.expires_at.replace(tzinfo=timezone.utc) if row.expires_at.tzinfo is None else row.expires_at
        if row.status != "PENDING_APPROVAL":
            return row
        if expires <= now:
            row.status = "EXPIRED"
            await session.commit()
            return row

        block_reason = await _approval_block_reason(session, row)
        if block_reason:
            row.status = "REJECTED"
            row.rejected_by = "SYSTEM_POLICY"
            row.admin_note = block_reason
            await session.commit()
            return row
        claimed = await session.execute(update(ManualApprovalRequest).where(
            ManualApprovalRequest.id == request_id,
            ManualApprovalRequest.status == "PENDING_APPROVAL",
        ).values(status="SENDING", approved_by=approved_by))
        if claimed.rowcount != 1:
            await session.rollback()
            return (await session.get(ManualApprovalRequest, request_id))
        await session.commit()

    try:
        health = await gateway_client.health()
        if not health.get("positionsLoaded"):
            raise GatewayUnavailable("positions not loaded")
        outcome = await gateway_client.send_order(request_id=row.request_id, symbol=row.symbol, side=row.action, qty=row.qty, limit_price=row.price, mode=SignalMode.DEMO_LIVE.value)
        final_status = "SENT_PENDING" if outcome.get("accepted") else "SEND_FAILED"
        message = note or str(outcome.get("reason", ""))
    except (GatewayUnavailable, GatewayError) as exc:
        final_status = "SEND_UNKNOWN"
        message = str(exc)

    async with async_session_factory() as session:
        row = await session.get(ManualApprovalRequest, request_id)
        if row.status != "SENDING":
            return row
        row.status = final_status; row.admin_note = message
        existing = (await session.execute(select(OrderLog).where(OrderLog.request_id == row.request_id))).scalar_one_or_none()
        if existing is None:
            session.add(OrderLog(request_id=row.request_id, symbol=row.symbol, action=row.action, qty=row.qty, price=row.price, status=final_status, matrix_message=message, mode="DEMO_LIVE"))
        await session.commit(); return row


async def _approval_block_reason(session, row: ManualApprovalRequest) -> str | None:
    if not settings.manual_approval_allow_orders:
        return "Manual approval orders disabled by MANUAL_APPROVAL_ALLOW_ORDERS"
    if row.order_type != OrderType.LIMIT.value:
        return "Manual approval blocked: only LIMIT orders are allowed"

    values = {
        key: await get_admin_config_value(session, key)
        for key in (
            "killSwitchEnabled",
            "tradingMode",
            "botMode",
            "botEnableDemoOrders",
            "botDemoAccountConfirmed",
        )
    }
    if values["killSwitchEnabled"].lower() == "true":
        return "Manual approval blocked: kill switch enabled"

    modes = {values["tradingMode"].upper(), values["botMode"].upper()}
    if modes.intersection({SignalMode.LIVE.value, SignalMode.REAL_LIVE.value}):
        return "Manual approval blocked: LIVE/REAL_LIVE mode is forbidden"
    if SignalMode.DEMO_LIVE.value not in modes:
        return "Manual approval blocked: botMode is not DEMO_LIVE"
    if values["botEnableDemoOrders"].lower() != "true":
        return "Manual approval blocked: demo orders are not enabled"
    if values["botDemoAccountConfirmed"].lower() != "true":
        return "Manual approval blocked: demo account not confirmed"
    return None


async def reject_request(request_id: int, rejected_by: str, note: str | None = None) -> ManualApprovalRequest:
    async with async_session_factory() as session:
        row = await session.get(ManualApprovalRequest, request_id)
        if row is None: raise ValueError("Approval not found")
        result = await session.execute(update(ManualApprovalRequest).where(
            ManualApprovalRequest.id == request_id,
            ManualApprovalRequest.status == "PENDING_APPROVAL",
        ).values(status="REJECTED", rejected_by=rejected_by, admin_note=note))
        if result.rowcount == 1:
            await session.commit()
        else:
            await session.rollback()
        return await session.get(ManualApprovalRequest, request_id)
