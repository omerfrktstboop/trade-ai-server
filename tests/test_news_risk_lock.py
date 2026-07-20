from __future__ import annotations

import app.services.news_risk_lock as news_lock
from app.models.signal import OrderType, SignalAction, SignalResponse


def _response(action=SignalAction.BUY, *, allowed=False):
    return SignalResponse(
        requestId="news-risk",
        symbol="THYAO",
        action=action,
        qty=10,
        orderType=OrderType.LIMIT,
        price=100,
        confidenceScore=80,
        riskScore=10,
        allowOrder=allowed,
        reason="proposal",
    )


async def test_negative_news_blocks_actionable_buy(monkeypatch):
    async def risky(_symbol):
        return "tedbir", "SPK tedbir kararı"

    monkeypatch.setattr(news_lock, "active_news_risk", risky)
    response = await news_lock.apply_news_risk_lock(
        _response(allowed=True), "THYAO"
    )
    assert response.action == SignalAction.WAIT
    assert response.allow_order is False
    assert response.order_type == OrderType.NONE
    assert response.qty == 0
    assert response.price is None
    assert "tedbir - SPK tedbir kararı" in response.reason


async def test_negative_news_does_not_block_sell(monkeypatch):
    async def risky(_symbol):
        return "tedbir", "headline"

    monkeypatch.setattr(news_lock, "active_news_risk", risky)
    response = await news_lock.apply_news_risk_lock(
        _response(SignalAction.SELL, allowed=True), "THYAO"
    )
    assert response.action == SignalAction.SELL
    assert response.allow_order is True


async def test_news_lookup_error_fails_open(monkeypatch):
    async def broken(_symbol):
        raise RuntimeError("news unavailable")

    monkeypatch.setattr(news_lock, "active_news_risk", broken)
    response = await news_lock.apply_news_risk_lock(_response(allowed=True), "THYAO")
    assert response.action == SignalAction.BUY
    assert response.allow_order is True
