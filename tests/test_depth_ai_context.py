from pathlib import Path

from app.core.risk_config import RiskConfig
from app.models.signal import SignalAction, SignalMode
from app.services.evaluator import build_payload, snapshot_to_signal_request
from app.services.risk_engine import RiskEngine


def _payload():
    return {
        "lastPrice": 100, "open": 99, "high": 101, "low": 98, "volume": 1000,
        "depthAnalysis": {
            "levelsUsed": 25, "spreadPct": 0.08, "bidAskRatioTop5": 0.7,
            "bidAskRatioTop10": 0.45, "bidAskRatioTop25": 0.5,
            "imbalanceTop5": -0.2, "imbalanceTop10": -0.38, "imbalanceTop25": -0.33,
            "buyPressureScore": 25, "sellPressureScore": 85,
            "orderBookSignal": "STRONG_SELL_PRESSURE", "depthReliable": True,
            "largestAskWall": {"distancePct": 0.1},
        },
    }


def test_snapshot_depth_maps_to_signal_and_ai_context():
    req = snapshot_to_signal_request("THYAO", _payload(), request_id="d1", mode=SignalMode.PAPER)
    assert req.depth_levels_used == 25
    assert req.depth_bid_ask_ratio_top10 == 0.45
    assert req.depth_largest_ask_wall_distance_pct == 0.1
    context = build_payload(req)["depthContext"]
    assert context["orderBookSignal"] == "STRONG_SELL_PRESSURE"
    assert context["bidAskRatioTop25"] == 0.5


def test_old_snapshot_and_missing_depth_remain_compatible():
    req = snapshot_to_signal_request("THYAO", {"lastPrice": 1, "open": 1, "high": 1, "low": 1, "volume": 0}, request_id="old", mode=SignalMode.PAPER)
    assert req.depth_order_book_signal is None
    assert "depthContext" not in build_payload(req)


def test_reliable_strong_sell_depth_blocks_buy_but_not_sell():
    req = snapshot_to_signal_request("THYAO", _payload(), request_id="d2", mode=SignalMode.DEMO_LIVE)
    engine = RiskEngine(RiskConfig(allowed_symbols="THYAO", block_buy_on_strong_sell_pressure=True))
    assert "depth" in engine._technical_feature_block_reason(req, SignalAction.BUY).lower()
    assert engine._technical_feature_block_reason(req, SignalAction.SELL) is None


def test_gateway_depth_contract_is_shared_and_capped():
    source = (Path(__file__).parents[1] / "matriks" / "TradeAiGateway.cs").read_text(encoding="utf-8")
    assert "Math.Min(25, requestedLevels)" in source
    assert 'payload["depthAnalysis"] = depthAnalysis' in source
    assert "ReadDepthSnapshot(symbol, 25)" in source
    assert "depthLevels = new { bids = depth.Bids, asks = depth.Asks }" in source
    build = source.split("private MarketDataPayload BuildMarketData", 1)[1].split("private void InitializeIndicators", 1)[0]
    assert 'payload["depthLevels"]' not in build
    assert "(r.TotalBidSize - r.TotalAskSize) / total" in source
    assert "WeightedDepthPrice" in source
    assert "median * 3m" in source and "sizes.Average() * 2.5m" in source
