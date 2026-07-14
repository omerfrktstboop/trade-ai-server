from pathlib import Path


def test_gateway_exposes_reconciliation_and_cancel_endpoints():
    source = (Path(__file__).parents[1] / "matriks" / "TradeAiGateway.cs").read_text(
        encoding="utf-8"
    )
    assert 'request.Path == "/orders/active"' in source
    assert 'request.Path == "/order/cancel"' in source
    assert "SendCancelOrder(orderId)" in source
    assert "GetRealOrders" in source


def test_cancel_endpoint_does_not_send_a_new_order():
    source = (Path(__file__).parents[1] / "matriks" / "TradeAiGateway.cs").read_text(
        encoding="utf-8"
    )
    handler = source.split("private async Task HandleCancelOrderAsync", 1)[1]
    handler = handler.split("private List<GatewayOrderSnapshot>", 1)[0]
    assert "SendCancelOrder" in handler
    assert "SendLimitOrder" not in handler
    assert "SendMarketOrder" not in handler


def test_account_endpoint_marks_provider_timestamp_and_reliability():
    source = (Path(__file__).parents[1] / "matriks" / "TradeAiGateway.cs").read_text(
        encoding="utf-8"
    )
    handler = source.split("private async Task HandleAccountAsync", 1)[1]
    handler = handler.split("private async Task HandleRealPositionsAsync", 1)[0]
    assert 'sourceProvider = "MATRIKS_IQ"' in handler
    assert 'DateTime.UtcNow.ToString("o")' in handler
    assert "accountDataReliable = user != null" in handler
    assert "accountDataReliable = false" in handler


def test_empty_gateway_buy_watchlist_is_fail_closed():
    source = (Path(__file__).parents[1] / "matriks" / "TradeAiGateway.cs").read_text(
        encoding="utf-8"
    )
    handler = source.split("private async Task HandleOrderAsync", 1)[1]
    handler = handler.split("private string CheckModeGates", 1)[0]
    assert 'side == "BUY" && !orderConfig.BuyAllowedSymbols.Contains(symbol)' in handler
    assert "BuyAllowedSymbols.Length > 0" not in handler
    assert "not in the active trade watchlist" in handler
