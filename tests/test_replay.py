import asyncio

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import AiDecision, MarketSnapshot, RiskDecision
from app.services.replay import replay_batch, replay_request


def test_replay_uses_recorded_data_without_gateway():
    async def run():
        await drop_all()
        await init_db()
        async with async_session_factory() as session:
            session.add(MarketSnapshot(request_id="replay-1", symbol="THYAO", timeframe="1h", open=99, high=101, low=98, close=100, volume=1000, mode="PAPER"))
            session.add(AiDecision(request_id="replay-1", symbol="THYAO", raw_response={"action": "BUY", "confidence": 1, "qty": 10, "reason": "test"}))
            session.add(RiskDecision(request_id="replay-1", symbol="THYAO", action="BUY", confidence=1, risk_score=0, allow_order=False, reason="old", qty=10, order_type="LIMIT", mode="PAPER"))
            await session.commit()
        one = await replay_request("replay-1", mode="PAPER")
        batch = await replay_batch(mode="PAPER")
        assert one is not None and one["requestId"] == "replay-1"
        assert batch["totalEvaluated"] == 1
        await drop_all()
        await init_db()
    asyncio.run(run())
