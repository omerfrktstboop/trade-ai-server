from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.services.order_preflight import validate_order_preflight
from tests.fake_gateway import make_snapshot_payload


def _validate(payload=None, created=None):
    return validate_order_preflight(
        payload=payload or make_snapshot_payload("THYAO"),
        positions={"confidence": "HIGH", "snapshotAgeSeconds": 1},
        health={"configStale": False},
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
        positions={"confidence": "HIGH", "snapshotAgeSeconds": 1},
        health={"configStale": True},
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


def test_gateway_has_independent_freshness_and_finite_guards():
    source = (Path(__file__).parents[1] / "matriks" / "TradeAiGateway.cs").read_text(
        encoding="utf-8"
    )
    assert "double.IsNaN(order.Qty)" in source
    assert "MaxQuoteAgeSecondsForOrder" in source
    assert "crossed order book" in source
    history = source.split("private void UpdateCloseHistory", 1)[1].split(
        "private List<decimal> GetCloseHistory", 1
    )[0]
    assert "list[list.Count - 1] = lastPrice" in history
