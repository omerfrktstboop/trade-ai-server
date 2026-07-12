"""Seed and replay an offline trading scenario without calling the gateway."""

from __future__ import annotations

import argparse
import asyncio
import json
from uuid import uuid4

from app.config import settings
from app.db.init_db import init_db
from app.db.session import async_session_factory
from app.models.db import AiDecision, MarketSnapshot, RiskDecision
from app.services.replay import replay_request


async def seed_dummy_scenario(symbol: str = "THYAO") -> tuple[str, dict]:
    request_id = f"SIM-{symbol.upper()}-{uuid4().hex[:12]}"
    ai_response = {
        "action": "BUY",
        "confidence": 88,
        "risk_score": 22,
        "qty": 10,
        "reason": "Offline scenario: oversold RSI and positive KAP context",
        "entryRange": {"min": 99.0, "max": 100.0},
        "stopLoss": 95.0,
        "targetPrice": 112.0,
    }
    raw_request = {
        "requestId": request_id,
        "symbol": symbol.upper(),
        "timeframe": "1h",
        "lastPrice": 100.0,
        "open": 106.0,
        "high": 107.0,
        "low": 98.0,
        "volume": 25_000_000,
        "rsi": 15.0,
        "ema20": 104.0,
        "ema50": 108.0,
        "macd": -2.2,
        "macdSignal": -1.4,
        "newsContext": {
            "headline": "Yeni yatırım ve kapasite artışı KAP açıklaması",
            "sentiment": "POSITIVE",
        },
        "mode": "PAPER",
    }
    async with async_session_factory() as session:
        session.add(
            MarketSnapshot(
                request_id=request_id,
                symbol=symbol.upper(),
                timeframe="1h",
                open=106.0,
                high=107.0,
                low=98.0,
                close=100.0,
                volume=25_000_000,
                rsi=15.0,
                ema20=104.0,
                ema50=108.0,
                macd=-2.2,
                macd_signal=-1.4,
                mode="PAPER",
            )
        )
        session.add(
            AiDecision(
                request_id=request_id,
                symbol=symbol.upper(),
                provider="offline-dummy",
                model="deterministic-simulation",
                raw_request=raw_request,
                raw_response=ai_response,
                action="BUY",
                confidence=88,
                qty=10,
                reason=ai_response["reason"],
            )
        )
        session.add(
            RiskDecision(
                request_id=request_id,
                symbol=symbol.upper(),
                action="BUY",
                confidence=88,
                risk_score=22,
                allow_order=False,
                reason="Offline seed before replay",
                entry_min=99,
                entry_max=100,
                stop_loss=95,
                target_price=112,
                qty=10,
                order_type="LIMIT",
                mode="PAPER",
            )
        )
        await session.commit()
    return request_id, ai_response


async def run_simulation(symbol: str, profile: str | None, mode: str) -> dict:
    if settings.is_development:
        await init_db()
    request_id, ai_response = await seed_dummy_scenario(symbol)
    replay = await replay_request(request_id, profile_code=profile, mode=mode)
    return {
        "requestId": request_id,
        "scenario": "oversold-price-drop-positive-kap",
        "recordedAiDecision": ai_response,
        "riskReplay": replay,
        "gatewayCalled": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="THYAO")
    parser.add_argument("--profile", default=None)
    parser.add_argument(
        "--mode", choices=("PAPER", "MANUAL", "DEMO_LIVE"), default="PAPER"
    )
    args = parser.parse_args()
    result = asyncio.run(run_simulation(args.symbol, args.profile, args.mode))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
