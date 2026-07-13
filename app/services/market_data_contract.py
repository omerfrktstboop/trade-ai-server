"""Normalize the versioned Matriks market-data payload without hiding semantics.

The legacy gateway exposed one ambiguous ``volume`` field and the evaluator
silently labelled snapshots without a timeframe as ``1h``.  New gateways send
separate session-turnover and bar-volume fields.  This module keeps old
payloads readable while refusing to invent a bar period or reinterpret a
known TL turnover value as bar volume.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


_PERIOD_ALIASES = {
    "MIN": "MIN1",
    "MIN1": "MIN1",
    "1M": "MIN1",
    "MIN5": "MIN5",
    "5M": "MIN5",
    "MIN15": "MIN15",
    "15M": "MIN15",
    "MIN30": "MIN30",
    "30M": "MIN30",
    "MIN60": "MIN60",
    "HOUR": "MIN60",
    "1H": "MIN60",
    "DAY": "DAY1",
    "DAY1": "DAY1",
    "1D": "DAY1",
}


def canonical_period(value: Any) -> str | None:
    """Return a comparison-safe period name, preserving unknown values."""

    if value is None:
        return None
    normalized = str(value).strip().upper().replace("_", "")
    if not normalized:
        return None
    return _PERIOD_ALIASES.get(normalized, normalized)


def normalize_snapshot_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return a semantic, backward-compatible copy of a gateway payload.

    ``volume`` remains as a compatibility alias, but always means *bar volume*
    after normalization.  A legacy payload carrying only an ambiguous volume
    is retained and explicitly marked ``LEGACY_AMBIGUOUS``; a value explicitly
    identified as session turnover is never copied into bar volume.
    """

    normalized = deepcopy(payload)

    requested = normalized.get("requestedTimeframe")
    actual = normalized.get("actualBarPeriod")
    legacy_timeframe = normalized.get("timeframe")
    if requested is None:
        requested = legacy_timeframe or actual
    if actual is None and normalized.get("barPeriodSource"):
        actual = legacy_timeframe

    normalized["requestedTimeframe"] = requested
    normalized["actualBarPeriod"] = actual
    normalized["timeframe"] = actual or legacy_timeframe or "UNKNOWN"
    requested_period = canonical_period(requested)
    actual_period = canonical_period(actual)
    normalized["timeframeMismatch"] = bool(
        requested_period and actual_period and requested_period != actual_period
    )

    total_vol_semantic = str(normalized.get("totalVolSemantic") or "").upper()
    volume_semantic = str(normalized.get("volumeSemantic") or "").upper()
    explicit_turnover = (
        normalized.get("sessionTurnoverTl") is not None
        or total_vol_semantic in {"SESSION_TURNOVER_TL", "CUMULATIVE_SESSION_TURNOVER_TL"}
        or volume_semantic in {"SESSION_TURNOVER_TL", "CUMULATIVE_SESSION_TURNOVER_TL"}
    )

    bar_volume = normalized.get("barVolume")
    if bar_volume is None and "volume" in normalized and not explicit_turnover:
        bar_volume = normalized.get("volume")
        normalized.setdefault("barVolumeSource", "LEGACY_VOLUME_FIELD")
        normalized.setdefault("barVolumeUnit", "UNKNOWN")
        normalized.setdefault("barVolumeReliable", False)
        normalized.setdefault("volumeSemantic", "LEGACY_AMBIGUOUS")

    normalized["barVolume"] = bar_volume
    # SignalRequest.volume and old AI consumers continue to work, but they can
    # no longer receive a value known to be cumulative TL turnover.
    normalized["volume"] = bar_volume if bar_volume is not None else 0

    if normalized.get("sessionTurnoverTl") is None and normalized.get("totalVol") is not None:
        normalized["sessionTurnoverTl"] = normalized.get("totalVol")
    if normalized.get("symbolTrendRegime") is None and normalized.get("marketRegime"):
        normalized["symbolTrendRegime"] = normalized.get("marketRegime")
    if normalized.get("lastTradeUtc") is None and normalized.get("quoteEventUtc"):
        normalized["lastTradeUtc"] = normalized.get("quoteEventUtc")
        normalized.setdefault("quoteTimestampSource", "LEGACY_QUOTE_EVENT")
    if normalized.get("depthEventUtc") is None:
        normalized.setdefault("depthEventTimestampAvailable", False)
        normalized.setdefault("depthTimestampSource", "READ_TIME_ONLY")
    normalized.setdefault("schemaVersion", "technical-features-v2")
    normalized.setdefault("marketDataContractVersion", "legacy-normalized-v1")
    return normalized


def normalize_gateway_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Normalize the nested payload of one gateway snapshot response."""

    result = dict(snapshot)
    payload = result.get("payload")
    if isinstance(payload, dict):
        result["payload"] = normalize_snapshot_payload(payload)
    return result
