"""Recorded-decision replay. This module never imports or calls the gateway."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import AiDecision, MarketSnapshot, RiskDecision as RiskDecisionModel
from app.models.signal import SignalRequest
from app.services.admin_config import build_runtime_risk_config
from app.services.evaluator import dict_to_risk_decision
from app.services.risk_engine import RiskEngine
from app.services.trade_profile import get_profile


async def list_replay_candidates(
    symbol: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: int = 100,
) -> list[str]:
    async with async_session_factory() as session:
        stmt = (
            select(MarketSnapshot.request_id)
            .order_by(MarketSnapshot.created_at.desc())
            .limit(limit)
        )
        if symbol:
            stmt = stmt.where(MarketSnapshot.symbol == symbol.upper())
        if since:
            stmt = stmt.where(MarketSnapshot.created_at >= since)
        if until:
            stmt = stmt.where(MarketSnapshot.created_at <= until)
        return list((await session.execute(stmt)).scalars().all())


async def replay_request(
    request_id: str, profile_code: str | None = None, mode: str | None = None
) -> dict[str, Any] | None:
    async with async_session_factory() as session:
        snapshot = (
            (
                await session.execute(
                    select(MarketSnapshot).where(
                        MarketSnapshot.request_id == request_id
                    )
                )
            )
            .scalars()
            .first()
        )
        ai = (
            (
                await session.execute(
                    select(AiDecision).where(AiDecision.request_id == request_id)
                )
            )
            .scalars()
            .first()
        )
        original = (
            (
                await session.execute(
                    select(RiskDecisionModel).where(
                        RiskDecisionModel.request_id == request_id
                    )
                )
            )
            .scalars()
            .first()
        )
        if not snapshot or not ai or not ai.raw_response:
            return None
        config = await build_runtime_risk_config(session)
        if profile_code:
            profile = await get_profile(session, profile_code)
            if profile is None:
                raise ValueError(f"Unknown trade profile: {profile_code}")
            config = config.model_copy(
                update={
                    "max_position_value_per_symbol": profile.max_position_value_per_symbol,
                    "max_daily_trade_count": profile.max_orders_per_day,
                    "min_confidence_for_buy": profile.min_confidence_for_buy,
                    "min_confidence_for_sell": profile.min_confidence_for_sell,
                    "max_natr_for_buy": profile.max_natr_for_buy,
                    "max_depth_queue_drop_pct_for_buy": profile.max_depth_queue_drop_pct_for_buy,
                    "demo_live_mode_allowed": profile.allow_demo_live,
                    "real_live_mode_allowed": False,
                }
            )
        request_data = dict(ai.raw_request or {})
        request_data.update(
            {
                "requestId": request_id,
                "symbol": snapshot.symbol,
                "timeframe": snapshot.timeframe,
                "lastPrice": snapshot.close,
                "open": snapshot.open,
                "high": snapshot.high,
                "low": snapshot.low,
                "volume": snapshot.volume,
                "rsi": snapshot.rsi,
                "ema20": snapshot.ema20,
                "ema50": snapshot.ema50,
                "macd": snapshot.macd,
                "macdSignal": snapshot.macd_signal,
                "botPositionQty": snapshot.position_qty,
                "totalAccountQty": snapshot.total_account_qty,
                "lockedLongTermQty": snapshot.locked_long_term_qty,
            }
        )
        request = SignalRequest.model_validate(request_data)
        replay = RiskEngine(config).evaluate(
            request,
            dict_to_risk_decision(ai.raw_response, request),
            request.macro_market_regime,
        )
        return {
            "requestId": request_id,
            "symbol": snapshot.symbol,
            "originalAction": original.action if original else ai.action,
            "originalAllowOrder": original.allow_order if original else False,
            "originalReason": original.reason if original else ai.reason,
            "replayAction": replay.action.value,
            "replayAllowOrder": replay.allow_order,
            "replayReason": replay.reason,
            "profileCode": profile_code,
            "mode": mode or snapshot.mode or "OBSERVE_ONLY",
        }


async def replay_batch(
    profile_code: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    symbols: list[str] | None = None,
    limit: int = 100,
    mode: str | None = None,
) -> dict[str, Any]:
    candidates = await list_replay_candidates(since=since, until=until, limit=limit)
    results = []
    for request_id in candidates:
        result = await replay_request(request_id, profile_code, mode)
        if result and (not symbols or result["symbol"] in {s.upper() for s in symbols}):
            results.append(result)
    return {
        "totalEvaluated": len(results),
        "replayAllowedOrderCount": sum(r["replayAllowOrder"] for r in results),
        "originalAllowedOrderCount": sum(r["originalAllowOrder"] for r in results),
        "changedCount": sum(
            (r["originalAction"], r["originalAllowOrder"])
            != (r["replayAction"], r["replayAllowOrder"])
            for r in results
        ),
        "results": results,
    }
