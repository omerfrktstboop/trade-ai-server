from pathlib import Path

from app.services.order_state_machine import transition


SOURCE = (Path(__file__).parents[1] / "matriks" / "TradeAiGateway.cs").read_text(encoding="utf-8")


def test_gateway_sellable_is_clamped_to_bot_owned_and_account_free():
    method = SOURCE.split("private decimal GetSellableQty", 1)[1].split("private OrderExecutionResult", 1)[0]
    assert "Math.Min(GetBotOwnedQty(symbol), accountFree)" in method
    assert "MaxPositionSyncAgeSeconds" in method
    assert "_positionSnapshotConfidence" in method


def test_incomplete_empty_snapshot_does_not_clear_cache():
    method = SOURCE.split("private void LoadRealPositionsSnapshot", 1)[1].split("private void UpdatePositionCache", 1)[0]
    guard = method.index("!PositionReceiveComplated && positions.Count == 0")
    publish = method.index("_accountNetQtyBySymbol =")
    assert guard < publish


def test_stale_fill_cannot_reduce_cumulative_ownership():
    assert transition("PARTIALLY_FILLED", "PARTIALLY_FILLED", current_filled=40, incoming_filled=40)[0]
    assert not transition("PARTIALLY_FILLED", "PARTIALLY_FILLED", current_filled=40, incoming_filled=20)[0]
