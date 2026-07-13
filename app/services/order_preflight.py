"""Order-time market freshness and price validation."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

MAX_DECISION_AGE_SECONDS = 20.0
MAX_QUOTE_AGE_SECONDS = 15.0
MAX_DEPTH_AGE_SECONDS = 10.0
MAX_POSITION_AGE_SECONDS = 60.0
MAX_PRICE_DRIFT_PCT = Decimal("0.75")


def _decimal(value: Any) -> Decimal | None:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def validate_order_preflight(
    *,
    payload: dict[str, Any],
    positions: dict[str, Any],
    health: dict[str, Any],
    side: str,
    qty: int,
    limit_price: Decimal,
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
    parsed_limit = _decimal(limit_price)
    if (
        isinstance(qty, bool)
        or not isinstance(qty, int)
        or qty <= 0
        or parsed_limit is None
        or parsed_limit <= 0
    ):
        return "invalid non-finite, non-positive, or fractional order value"
    quote_age, depth_age = (
        payload.get("quoteAgeSeconds"),
        payload.get("depthAgeSeconds"),
    )
    if (
        payload.get("quoteFresh") is False
        or payload.get("quoteReliable") is not True
        or quote_age is None
        or not math.isfinite(float(quote_age))
        or float(quote_age) > MAX_QUOTE_AGE_SECONDS
    ):
        return "quote is unavailable or stale"
    bid = _decimal(payload.get("bidPrice") or payload.get("bestBid") or 0)
    ask = _decimal(payload.get("askPrice") or 0)
    bid_size = _decimal(payload.get("bidVolume") or 0)
    ask_size = _decimal(payload.get("askVolume") or 0)
    if (
        payload.get("depthReliable") is not True
        or depth_age is None
        or float(depth_age) > MAX_DEPTH_AGE_SECONDS
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
    spread_pct = (ask - bid) / ((ask + bid) / Decimal("2")) * Decimal("100")
    if side.upper() == "BUY" and spread_pct > Decimal(str(max_spread_pct)):
        return "spread exceeds active profile limit"
    reference = ask if side.upper() == "BUY" else bid
    if abs(parsed_limit - reference) / reference * Decimal("100") > MAX_PRICE_DRIFT_PCT:
        return "limit price drift exceeds order-time threshold"
    position_age = positions.get("snapshotAgeSeconds")
    if (
        positions.get("confidence") not in {"HIGH", "MEDIUM"}
        or position_age is None
        or float(position_age) > MAX_POSITION_AGE_SECONDS
    ):
        return "position snapshot is stale or unreliable"
    if health.get("configStale") is not False:
        return "gateway config is stale"
    return None
