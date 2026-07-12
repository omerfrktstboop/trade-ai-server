from pathlib import Path


SOURCE = (Path(__file__).parents[1] / "matriks" / "TradeAiGateway.cs").read_text(
    encoding="utf-8"
)


def _method(name: str, next_name: str) -> str:
    return SOURCE.split(name, 1)[1].split(next_name, 1)[0]


def test_runtime_mode_is_authoritative_and_mismatch_rejected():
    handler = _method(
        "private async Task HandleOrderAsync", "private string CheckModeGates"
    )
    assert "CheckModeGates(RuntimeMode)" in handler
    assert "request mode does not match RuntimeMode" in handler


def test_final_values_are_checked_and_fractional_qty_rejected():
    handler = _method(
        "private async Task HandleOrderAsync", "private string CheckModeGates"
    )
    assert "finalQty * roundedPrice" in handler
    assert "qty has fractional component" in handler


def test_market_data_reads_do_not_mutate_close_history():
    build = _method(
        "private MarketDataPayload BuildMarketData", "private void InitializeIndicators"
    )
    assert "UpdateCloseHistory" not in build


def test_pending_idempotency_reconciliation_and_kap_contracts():
    assert "_pendingOrdersByRequestId.TryAdd" in SOURCE
    assert "_pendingOrdersBySymbolSide.TryAdd" in SOURCE
    assert "IdempotencyTtl" in SOURCE and "CleanupIdempotencyCache" in SOURCE
    assert "PositionSyncIntervalSeconds" in SOURCE
    assert '[JsonProperty("publishedAt")]' in SOURCE
    kap = _method(
        "private async Task HandleKapAsync", "private static bool IsKapLikeNews"
    )
    assert "AddHours(-lookbackHours)" in kap


def test_position_snapshot_is_published_by_reference_swap():
    load = _method(
        "private void LoadRealPositionsSnapshot", "private void UpdatePositionCache"
    )
    assert "_accountNetQtyBySymbol.Clear()" not in load
    assert "new ConcurrentDictionary<string, decimal>(netSnapshot)" in load


def test_bar_history_requires_a_real_bar_timestamp():
    update = _method(
        "private void UpdateOhlcvSnapshotFromBarData",
        "private string ResolveBarEventSymbol",
    )
    assert "TryResolveBarTimestamp" in update
    assert "if (hasBarTimestamp)" in update


def test_duplicate_order_returns_cached_result():
    handler = _method(
        "private async Task HandleOrderAsync", "private string CheckModeGates"
    )
    assert "cachedResult.Status" in handler
    assert "duplicate = true" in handler
