import asyncio

from app.db.init_db import drop_all, init_db
from app.db.session import async_session_factory
from app.models.db import OrderLog, RiskDecision
from app.services.performance_report import build_performance_report


def test_report_counts_decisions_and_orders():
    async def run():
        await drop_all(); await init_db()
        async with async_session_factory() as session:
            session.add(RiskDecision(request_id="p1", symbol="THYAO", action="BUY", confidence=80, risk_score=10, allow_order=True, reason="ok", qty=1, order_type="LIMIT", mode="PAPER"))
            session.add(RiskDecision(request_id="p2", symbol="THYAO", action="WAIT", confidence=20, risk_score=20, allow_order=False, reason="Confidence below minimum", qty=0, order_type="NONE", mode="PAPER"))
            session.add(OrderLog(request_id="p1", symbol="THYAO", action="BUY", qty=1, price=100, status="FILLED"))
            await session.commit()
        report = await build_performance_report("30d")
        assert report["totalDecisions"] == 2 and report["filledOrders"] == 1
        assert report["topBlockReason"] == "CONFIDENCE_LOW"
    asyncio.run(run())
