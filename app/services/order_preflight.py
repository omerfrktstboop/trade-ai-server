"""Order-time market freshness and price validation."""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

MAX_DECISION_AGE_SECONDS = 20.0
MAX_QUOTE_AGE_SECONDS = 15.0
MAX_DEPTH_AGE_SECONDS = 10.0
MAX_POSITION_AGE_SECONDS = 60.0
MAX_PRICE_DRIFT_PCT = 0.75


def validate_order_preflight(
    *,
    payload: dict[str, Any],
    positions: dict[str, Any],
    health: dict[str, Any],
    side: str,
    qty: float,
    limit_price: float,
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
    numbers = (qty, limit_price)
    if (
        not all(math.isfinite(float(v)) for v in numbers)
        or qty <= 0
        or limit_price <= 0
        or float(qty) != int(qty)
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
    bid, ask = (
        float(payload.get("bidPrice") or payload.get("bestBid") or 0),
        float(payload.get("askPrice") or 0),
    )
    bid_size, ask_size = (
        float(payload.get("bidVolume") or 0),
        float(payload.get("askVolume") or 0),
    )
    if (
        payload.get("depthReliable") is not True
        or depth_age is None
        or float(depth_age) > MAX_DEPTH_AGE_SECONDS
        or bid <= 0
        or ask <= 0
        or bid_size <= 0
        or ask_size <= 0
        or bid >= ask
    ):
        return "order book is invalid, crossed, or stale"
    spread_pct = (ask - bid) / ((ask + bid) / 2) * 100
    if side.upper() == "BUY" and spread_pct > max_spread_pct:
        return "spread exceeds active profile limit"
    reference = ask if side.upper() == "BUY" else bid
    if abs(limit_price - reference) / reference * 100 > MAX_PRICE_DRIFT_PCT:
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
