"""Static checks for the Matriks IQ C# bot integration."""

from __future__ import annotations

import re
from pathlib import Path


BOT_PATH = Path("matriks/TradeAiAgenticBot.cs")


def _bot_source() -> str:
    return BOT_PATH.read_text(encoding="utf-8")


def test_send_limit_order_uses_documented_signature():
    source = _bot_source()

    public_classes = re.findall(r"\bpublic\s+class\s+(\w+)", source)
    assert public_classes == ["TradeAiAgenticBot"]
    assert "public class TradeAiAgenticBot : MatriksAlgo" in source
    assert (
        "SendLimitOrder(symbol, quantity, orderSide, roundedPrice, timeInForce, chartIcon)"
        in source
    )
    assert "TryConvertOrderQuantity(qty" in source
    assert "RoundPriceStepBistViop(symbol, limitPrice)" in source
    assert "ChartIcon.Buy" in source
    assert "ChartIcon.Sell" in source
    assert "new TimeInForce(TimeInForce.Day)" in source
    assert "new TimeInForce(TimeInForce.GoodTillCancel)" in source
    assert "SendLimitOrder(symbol, qty, orderSide, limitPrice)" not in source


def test_order_updates_report_real_broker_status():
    source = _bot_source()

    assert "public override void OnOrderUpdate(IOrder order)" in source
    for field in (
        "order.OrderID",
        "order.Symbol",
        "order.OrderQty",
        "order.FilledQty",
        "order.AvgPx",
        "order.OrdStatus",
    ):
        assert field in source
    for status in (
        "OrdStatus.New",
        "OrdStatus.PartiallyFilled",
        "OrdStatus.Filled",
        "OrdStatus.Canceled",
        "OrdStatus.Rejected",
    ):
        assert status in source
    assert "ReportOrderResultAsync(context, status" in source
    assert "SENT_PENDING" in source


def test_positions_use_real_position_cache():
    source = _bot_source()

    assert "public override void OnRealPositionUpdate(AlgoTraderPosition position)" in source
    assert "GetRealPositions()" in source
    assert "PositionReceiveComplated" in source
    assert "position.QtyAvailable" in source
    assert "position.QtyNet" in source
    assert "UpdateSimulatedPosition" not in source


def test_timer_drives_scanning_instead_of_on_data_update():
    source = _bot_source()

    assert "SetTimerInterval(60)" in source
    assert "public override void OnTimer()" in source
    assert "ScanDueSymbols();" in source

    match = re.search(
        r"public override void OnDataUpdate\(BarDataEventArgs barData\)\s*\{(?P<body>.*?)\n\s*\}",
        source,
        flags=re.S,
    )
    assert match is not None
    assert "ScanDueSymbols" not in match.group("body")
    assert "RefreshCloseHistoryFromMarketData" in match.group("body")


def test_demo_account_gate_logs_trade_user_permissions():
    source = _bot_source()

    assert "GetTradeUser()" in source
    assert "tradeUser.AutoOrder" in source
    assert "tradeUser.TestAutoOrder" in source
    assert "_testAutoOrderEnabled.Value" in source


def test_matriks_payload_includes_ai_server_technical_features():
    source = _bot_source()

    for field in (
        'payload["technicalFeatures"]',
        'features["alphaTrendSignal"]',
        'features["alphaTrendMode"] = "PROXY_EMA_MACD_RSI"',
        'features["indicatorBuyCount"]',
        'features["indicatorSellCount"]',
        'features["indicatorConsensus"]',
        'features["depthQueueDropPct"]',
        "CalculateAtrFromClose(symbol, 14)",
        "CalculateNatrPct(symbol, 14)",
        "_maxBid1SizeBySymbol",
    ):
        assert field in source
