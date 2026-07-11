from __future__ import annotations
from app.db.session import async_session_factory
from app.models.db import BotPosition, PositionManagementDecision

_ACTIONS = {"HOLD", "TAKE_PROFIT", "STOP_LOSS", "REDUCE_POSITION", "EXIT_FULL", "TRAIL_STOP_UPDATE"}

async def record_position_management(request, raw: dict, response) -> None:
    if request.bot_position_qty <= 0: return
    async with async_session_factory() as session:
        position = await session.get(BotPosition, request.symbol)
        avg = position.avg_price if position else None
        pnl = ((request.last_price - avg) / avg * 100) if avg else None
        action = str(raw.get("positionAction") or ("EXIT_FULL" if response.action.value == "SELL" else "HOLD")).upper()
        action = action if action in _ACTIONS else "HOLD"
        qty = min(request.bot_position_qty, max(0.0, float(raw.get("positionQty") or response.qty or 0)))
        if action == "EXIT_FULL": qty = request.bot_position_qty
        session.add(PositionManagementDecision(request_id=request.request_id, symbol=request.symbol, bot_qty=request.bot_position_qty, avg_cost=avg, last_price=request.last_price, unrealized_pnl_pct=pnl, action=action, suggested_sell_qty=qty, suggested_limit_price=response.price, stop_loss=response.stop_loss, take_profit=response.target_price, confidence=response.confidence_score, reason=response.reason, status="SUGGESTED"))
        await session.commit()
