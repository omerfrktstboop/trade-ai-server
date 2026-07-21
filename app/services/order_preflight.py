"""Order-time market freshness and price validation."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_DECISION_AGE_SECONDS = 20.0
MAX_QUOTE_AGE_SECONDS = 15.0
MAX_DEPTH_AGE_SECONDS = 10.0
MAX_POSITION_AGE_SECONDS = 60.0
MAX_PRICE_DRIFT_PCT = Decimal("0.75")


def parse_finite_decimal(value: Any) -> Decimal | None:
    """Return a finite Decimal only; never coerce booleans or bad input."""
    if isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError, OverflowError):
        return None
    return result if result.is_finite() else None


def _valid_age(value: Any, maximum: float) -> bool:
    age = parse_finite_decimal(value)
    return age is not None and Decimal("0") <= age <= Decimal(str(maximum))


def bid_liquidity_block_reason(
    *,
    metric_ready: Any,
    current_top5_drop_pct: Any,
    recent_top5_drop_pcts: Any,
    legacy_drop_pct: Any,
    maximum_drop_pct: Any,
) -> str | None:
    """Validate rolling Top5 liquidity, retaining the old gateway hard gate."""
    maximum = parse_finite_decimal(maximum_drop_pct)
    if maximum is None or not Decimal("0") <= maximum <= Decimal("100"):
        return "BUY blocked: invalid bid liquidity profile limit"

    rolling_values_present = (
        current_top5_drop_pct is not None or recent_top5_drop_pcts is not None
    )
    if metric_ready is None:
        if rolling_values_present:
            return "BUY blocked: rolling Top5 bid liquidity readiness is unavailable"
        if legacy_drop_pct is None:
            return None
        legacy = parse_finite_decimal(legacy_drop_pct)
        if legacy is None or not Decimal("0") <= legacy <= Decimal("100"):
            return "BUY blocked: legacy bid queue metric is invalid"
        if legacy > maximum:
            return f"BUY blocked: bid queue dropped {legacy:.1f}% (max {maximum:.1f}%)"
        return None

    if metric_ready is not True:
        return "BUY blocked: rolling Top5 bid liquidity baseline is warming"

    current = parse_finite_decimal(current_top5_drop_pct)
    if current is None or not Decimal("0") <= current <= Decimal("100"):
        return "BUY blocked: rolling Top5 bid liquidity metric is invalid"
    if not isinstance(recent_top5_drop_pcts, (list, tuple)):
        return "BUY blocked: rolling Top5 bid liquidity history is unavailable"
    recent = [parse_finite_decimal(item) for item in recent_top5_drop_pcts]
    if any(item is None or not Decimal("0") <= item <= Decimal("100") for item in recent):
        return "BUY blocked: rolling Top5 bid liquidity history is invalid"
    if len(recent) < 2:
        return "BUY blocked: rolling Top5 bid liquidity baseline is warming"
    if current > maximum and all(item > maximum for item in recent[-2:]):
        return (
            f"BUY blocked: persistent Top5 bid liquidity drop {current:.1f}% "
            f"(max {maximum:.1f}%)"
        )
    return None


def _payload_field(payload: dict[str, Any], key: str) -> Any:
    if key in payload:
        return payload.get(key)
    technical = payload.get("technicalFeatures")
    return technical.get(key) if isinstance(technical, dict) else None


def validate_order_preflight(
    *,
    payload: dict[str, Any],
    positions: dict[str, Any],
    health: dict[str, Any],
    side: str,
    qty: Any,
    limit_price: Any,
    decision_created_utc: datetime,
    max_spread_pct: float,
    max_depth_queue_drop_pct: float = 35.0,
) -> str | None:
    now = datetime.now(timezone.utc)
    created = (
        decision_created_utc
        if decision_created_utc.tzinfo
        else decision_created_utc.replace(tzinfo=timezone.utc)
    )
    if (now - created).total_seconds() > MAX_DECISION_AGE_SECONDS:
        return "decision is stale"
    if payload.get("sessionOpen") is not True:
        return "trading session is closed or unknown"

    parsed_qty = parse_finite_decimal(qty)
    parsed_limit = parse_finite_decimal(limit_price)
    if (
        isinstance(qty, bool)
        or not isinstance(qty, int)
        or parsed_qty is None
        or parsed_qty <= 0
        or parsed_qty != parsed_qty.to_integral_value()
        or parsed_limit is None
        or parsed_limit <= 0
    ):
        return "invalid non-finite, non-positive, or fractional order value"

    if (
        payload.get("quoteFresh") is False
        or payload.get("quoteReliable") is not True
        or not _valid_age(payload.get("quoteAgeSeconds"), MAX_QUOTE_AGE_SECONDS)
    ):
        return "quote is unavailable or stale"

    bid = parse_finite_decimal(payload.get("bidPrice") or payload.get("bestBid") or 0)
    ask = parse_finite_decimal(payload.get("askPrice") or 0)
    bid_size = parse_finite_decimal(payload.get("bidVolume") or 0)
    ask_size = parse_finite_decimal(payload.get("askVolume") or 0)
    if (
        payload.get("depthReliable") is not True
        or not _valid_age(payload.get("depthAgeSeconds"), MAX_DEPTH_AGE_SECONDS)
        or bid is None
        or ask is None
        or bid_size is None
        or ask_size is None
        or bid <= 0
        or ask <= 0
        or bid_size <= 0
        or ask_size <= 0
        or bid >= ask
    ):
        return "order book is invalid, crossed, or stale"

    normalized_side = side.upper() if isinstance(side, str) else ""
    if normalized_side not in {"BUY", "SELL"}:
        return "invalid order side"
    parsed_max_spread = parse_finite_decimal(max_spread_pct)
    if parsed_max_spread is None or parsed_max_spread < 0:
        return "invalid active profile spread limit"

    spread_pct = (ask - bid) / ((ask + bid) / Decimal("2")) * Decimal("100")
    if normalized_side == "BUY" and spread_pct > parsed_max_spread:
        return "spread exceeds active profile limit"
    if normalized_side == "BUY":
        liquidity_reason = bid_liquidity_block_reason(
            metric_ready=_payload_field(
                payload, "depthBidTop5DropMetricReady"
            ),
            current_top5_drop_pct=_payload_field(
                payload, "depthBidTop5DropPct"
            ),
            recent_top5_drop_pcts=_payload_field(
                payload, "depthBidTop5DropRecentPcts"
            ),
            legacy_drop_pct=_payload_field(payload, "depthQueueDropPct"),
            maximum_drop_pct=max_depth_queue_drop_pct,
        )
        if liquidity_reason:
            return liquidity_reason
    reference = ask if normalized_side == "BUY" else bid
    if abs(parsed_limit - reference) / reference * Decimal("100") > MAX_PRICE_DRIFT_PCT:
        return "limit price drift exceeds order-time threshold"

    if positions.get("confidence") not in {"HIGH", "MEDIUM"} or not _valid_age(
        positions.get("snapshotAgeSeconds"), MAX_POSITION_AGE_SECONDS
    ):
        return "position snapshot is stale or unreliable"
    position_ref = str(positions.get("accountRef") or "").strip()
    health_ref = str(health.get("accountRef") or "").strip()
    position_session = str(positions.get("accountSessionRef") or "").strip()
    health_session = str(health.get("accountSessionRef") or "").strip()
    if (
        len(position_ref) != 64
        or len(health_ref) != 64
        or position_ref != health_ref
        or len(position_session) != 64
        or len(health_session) != 64
        or position_session != health_session
    ):
        return "positions snapshot and health account identity mismatch"
    if health.get("configStale") is not False:
        return "gateway config is stale"
    if health.get("positionsLoaded") is not True:
        return "gateway positions are not loaded"
    return None
