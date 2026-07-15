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
    reference = ask if normalized_side == "BUY" else bid
    if abs(parsed_limit - reference) / reference * Decimal("100") > MAX_PRICE_DRIFT_PCT:
        return "limit price drift exceeds order-time threshold"

    if positions.get("confidence") not in {"HIGH", "MEDIUM"} or not _valid_age(
        positions.get("snapshotAgeSeconds"), MAX_POSITION_AGE_SECONDS
    ):
        return "position snapshot is stale or unreliable"
    if health.get("configStale") is not False:
        return "gateway config is stale"
    if health.get("positionsLoaded") is not True:
        return "gateway positions are not loaded"
    return None
