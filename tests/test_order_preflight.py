from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.order_preflight import validate_order_preflight
from tests.fake_gateway import make_snapshot_payload


def _validate(payload=None, created=None, health=None):
    return validate_order_preflight(
        payload=payload or make_snapshot_payload("THYAO"),
        positions={
            "confidence": "HIGH",
            "snapshotAgeSeconds": 1,
            "accountRef": "f" * 64,
            "accountSessionRef": "5" * 64,
        },
        health=health
        or {
            "configStale": False,
            "positionsLoaded": True,
            "accountRef": "f" * 64,
            "accountSessionRef": "5" * 64,
        },
        side="BUY",
        qty=1,
        limit_price=71.5,
        decision_created_utc=created or datetime.now(timezone.utc),
        max_spread_pct=0.5,
    )


def test_stale_quote_is_rejected():
    assert (
        "quote" in _validate(make_snapshot_payload("THYAO", quoteAgeSeconds=16)).lower()
    )


def test_crossed_book_is_rejected():
    payload = make_snapshot_payload("THYAO", bidPrice=72, askPrice=71)
    assert "crossed" in _validate(payload).lower()


def test_stale_depth_is_rejected():
    reason = _validate(make_snapshot_payload("THYAO", depthAgeSeconds=11))
    assert "stale" in reason.lower()


def test_zero_size_depth_is_rejected():
    reason = _validate(make_snapshot_payload("THYAO", askVolume=0))
    assert "order book" in reason.lower()


def test_closed_session_is_rejected():
    reason = _validate(make_snapshot_payload("THYAO", sessionOpen=False))
    assert "session" in reason.lower()


def test_order_time_price_drift_is_rejected():
    reason = validate_order_preflight(
        payload=make_snapshot_payload("THYAO"),
        positions={"confidence": "HIGH", "snapshotAgeSeconds": 1},
        health={"configStale": False},
        side="BUY",
        qty=1,
        limit_price=80,
        decision_created_utc=datetime.now(timezone.utc),
        max_spread_pct=0.5,
    )
    assert "drift" in reason.lower()


def test_stale_position_and_config_are_rejected():
    payload = make_snapshot_payload("THYAO")
    position_reason = validate_order_preflight(
        payload=payload,
        positions={"confidence": "HIGH", "snapshotAgeSeconds": 61},
        health={"configStale": False},
        side="BUY",
        qty=1,
        limit_price=71.5,
        decision_created_utc=datetime.now(timezone.utc),
        max_spread_pct=0.5,
    )
    config_reason = validate_order_preflight(
        payload=payload,
        positions={
            "confidence": "HIGH",
            "snapshotAgeSeconds": 1,
            "accountRef": "f" * 64,
            "accountSessionRef": "5" * 64,
        },
        health={
            "configStale": True,
            "accountRef": "f" * 64,
            "accountSessionRef": "5" * 64,
        },
        side="BUY",
        qty=1,
        limit_price=71.5,
        decision_created_utc=datetime.now(timezone.utc),
        max_spread_pct=0.5,
    )
    assert "position" in position_reason.lower()
    assert "config" in config_reason.lower()


def test_old_decision_is_rejected():
    assert (
        "decision"
        in _validate(created=datetime.now(timezone.utc) - timedelta(seconds=21)).lower()
    )


def test_unloaded_positions_are_rejected():
    reason = _validate(health={"configStale": False, "positionsLoaded": False})
    assert reason is not None
    assert "positions" in reason.lower()


def test_non_finite_order_is_rejected():
    reason = validate_order_preflight(
        payload=make_snapshot_payload("THYAO"),
        positions={"confidence": "HIGH", "snapshotAgeSeconds": 1},
        health={"configStale": False},
        side="BUY",
        qty=float("nan"),
        limit_price=71.5,
        decision_created_utc=datetime.now(timezone.utc),
        max_spread_pct=0.5,
    )
    assert "invalid" in reason.lower()


def test_persistent_top5_liquidity_drop_is_rechecked_before_order():
    payload = make_snapshot_payload(
        "THYAO",
        depthBidTop5DropMetricReady=True,
        depthBidTop5DropPct=62.0,
        depthBidTop5DropRecentPcts=[58.0, 62.0],
    )

    reason = _validate(payload)

    assert "persistent Top5 bid liquidity drop" in reason


def test_transient_top5_liquidity_drop_passes_order_preflight():
    payload = make_snapshot_payload(
        "THYAO",
        depthBidTop5DropMetricReady=True,
        depthBidTop5DropPct=62.0,
        depthBidTop5DropRecentPcts=[10.0, 62.0],
    )

    assert _validate(payload) is None


def test_partial_top5_liquidity_payload_fails_closed():
    payload = make_snapshot_payload(
        "THYAO",
        depthBidTop5DropMetricReady=True,
        depthBidTop5DropPct=None,
        depthBidTop5DropRecentPcts=[60.0, 60.0],
        depthQueueDropPct=90.0,
    )

    reason = _validate(payload)

    assert "metric is invalid" in reason


def test_missing_top5_readiness_with_rolling_values_fails_closed():
    payload = make_snapshot_payload(
        "THYAO",
        depthBidTop5DropMetricReady=None,
        depthBidTop5DropPct=60.0,
        depthBidTop5DropRecentPcts=[60.0, 60.0],
        depthQueueDropPct=10.0,
    )

    reason = _validate(payload)

    assert "readiness is unavailable" in reason


def test_non_finite_top5_liquidity_metric_fails_closed():
    payload = make_snapshot_payload(
        "THYAO",
        depthBidTop5DropMetricReady=True,
        depthBidTop5DropPct=float("nan"),
        depthBidTop5DropRecentPcts=[60.0, 60.0],
    )

    reason = _validate(payload)

    assert "metric is invalid" in reason


def test_legacy_gateway_queue_drop_remains_fail_closed():
    payload = make_snapshot_payload("THYAO", depthQueueDropPct=90.0)
    for key in (
        "depthBidTop5Size",
        "depthBidTop5ReferenceSize",
        "depthBidTop5DropPct",
        "depthBidTop5DropMetricReady",
        "depthBidTop5DropSampleCount",
        "depthBidTop5DropRecentPcts",
    ):
        payload.pop(key, None)
        payload["technicalFeatures"].pop(key, None)

    reason = _validate(payload)

    assert "bid queue dropped 90.0%" in reason


def test_gateway_has_independent_freshness_and_finite_guards():
    source = (Path(__file__).parents[1] / "matriks" / "TradeAiGateway.cs").read_text(
        encoding="utf-8"
    )
    assert "double.IsNaN(order.Qty)" in source
    assert "MaxQuoteAgeSecondsForOrder" in source
    assert "crossed order book" in source
    assert "BUY persistent Top5 bid liquidity drop" in source
    assert "SampleDepthLiquidityBaselines();" in source
    assert "configuredMaxDepthQueueDropPctForBuy.HasValue" in source
    assert "orderConfig.MaxDepthQueueDropPctForBuy" in source
    history = source.split("private void UpdateBarHistory", 1)[1].split(
        "private List<decimal> GetCloseHistory", 1
    )[0]
    assert "list[list.Count - 1] = bar.Close" in history
    assert "string.Equals(previous, barKey" in history
