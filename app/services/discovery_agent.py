"""Low-cost market discovery that creates research-only candidates.

Discovery never grants order permission.  It ranks the broad, data-only scan
universe with Matriks movers/snapshot data and persists candidates for the
separate AI research pipeline.  The legacy ``watchlist_symbols`` tables are
still mirrored for backwards-compatible admin reports, but the order scanner
does not consume them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

from sqlalchemy import select, update

from app.db.session import async_session_factory
from app.models.db import (
    ResearchCandidate,
    ResearchCandidateEvent,
    WatchlistQualityScore,
    WatchlistSymbol,
)
from app.services.admin_config import list_admin_configs
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
    gateway_client,
)
from app.services.watchlist_quality import calculate_quality

logger = logging.getLogger(__name__)

_HISTORICAL_BARS_CACHE: dict[tuple[int, str], tuple[float, dict[str, Any]]] = {}
_MIN_RELATIVE_VOLUME_BASELINE_BARS = 20


class DiscoveryScanResult(list[str]):
    """Accepted symbols plus a completion status for scanner observability."""

    def __init__(
        self,
        symbols: list[str] | None = None,
        *,
        status: str,
        universe_count: int = 0,
        candidate_count: int = 0,
        ranking_source: str = "NONE",
        ranking_scope: str = "UNAVAILABLE",
        weekly_gainer_count: int = 0,
        turnover_leader_count: int = 0,
        relative_volume_leader_count: int = 0,
        historical_bar_requested_count: int = 0,
        historical_bar_success_count: int = 0,
        enrichment_count: int = 0,
        filtered_count: int = 0,
        rejection_reason_counts: dict[str, int] | None = None,
        unavailable_signals: dict[str, str] | None = None,
    ) -> None:
        super().__init__(symbols or [])
        self.status = status
        self.universe_count = universe_count
        self.candidate_count = candidate_count
        self.ranking_source = ranking_source
        self.ranking_scope = ranking_scope
        self.weekly_gainer_count = weekly_gainer_count
        self.turnover_leader_count = turnover_leader_count
        self.relative_volume_leader_count = relative_volume_leader_count
        self.historical_bar_requested_count = historical_bar_requested_count
        self.historical_bar_success_count = historical_bar_success_count
        self.enrichment_count = enrichment_count
        self.filtered_count = filtered_count
        self.rejection_reason_counts = rejection_reason_counts or {}
        self.unavailable_signals = unavailable_signals or {}

@dataclass(frozen=True)
class DiscoveryPolicy:
    minimum_trend_score: float = 60.0
    minimum_volume_tl: float = 100_000_000.0
    maximum_change_pct: float = 9.3
    maximum_spread_pct: float = 0.50
    maximum_ask_bid_ratio: float = 3.0
    maximum_quote_age_seconds: float = 120.0
    maximum_weekly_change_pct: float = 18.0
    candidate_ttl_hours: int = 24
    max_candidates: int = 10
    scan_universe_symbols: frozenset[str] = frozenset()
    historical_bars_ttl_seconds: int = 300
    max_concurrent_bar_requests: int = 2


@dataclass(frozen=True)
class DiscoveryVerdict:
    reason: str
    wall_ratio: float | None
    trend_pre_score: float
    technical_summary: dict[str, Any]


async def load_discovery_policy() -> DiscoveryPolicy:
    async with async_session_factory() as session:
        values = {item.key: item.value for item in await list_admin_configs(session)}
    return DiscoveryPolicy(
        minimum_trend_score=float(values["minimumTrendPreScore"]),
        minimum_volume_tl=float(values["discoveryMinimumVolumeTl"]),
        maximum_spread_pct=float(values["discoveryMaximumSpreadPct"]),
        candidate_ttl_hours=max(1, int(values["researchCandidateTtlHours"])),
        max_candidates=max(1, int(values["maxResearchCandidatesPerCycle"])),
        scan_universe_symbols=frozenset(
            symbol.strip().upper()
            for symbol in values["scanUniverseSymbols"].split(",")
            if symbol.strip()
        ),
        historical_bars_ttl_seconds=max(
            60, int(values["discoveryIntervalMinutes"]) * 60
        ),
        max_concurrent_bar_requests=min(
            4, max(1, int(values["maxConcurrentResearchEvaluations"]))
        ),
    )


async def run_discovery_scan(
    gateway: MatriksGatewayClient | None = None,
) -> DiscoveryScanResult:
    """Screen movers and upsert research candidates; never trade symbols."""
    gw = gateway or gateway_client
    policy = await load_discovery_policy()
    try:
        capability_contract = await gw.get_market_ranking_capabilities()
    except (AttributeError, GatewayUnavailable, GatewayError):
        capability_contract = {}
    try:
        movers = await gw.get_movers(limit=50)
    except (GatewayUnavailable, GatewayError) as exc:
        logger.debug("Movers unavailable: %s", exc)
        return DiscoveryScanResult(status="GATEWAY_UNAVAILABLE")
    if not movers.get("available"):
        return DiscoveryScanResult(status="MARKET_DATA_UNAVAILABLE")

    items = {
        str(item.get("symbol") or "").strip().upper(): item
        for item in movers.get("items") or []
        if item.get("symbol")
        and str(item.get("symbol") or "").strip().upper()
        in policy.scan_universe_symbols
    }
    if not capability_contract:
        capabilities = movers.get("rankingCapabilities")
        capability_contract = capabilities if isinstance(capabilities, dict) else {}
    weekly_available = _ranking_available(capability_contract, "weeklyGainers")
    turnover_available = _turnover_ranking_available(capability_contract)
    relative_volume_available = _ranking_available(
        capability_contract, "relativeVolumeLeaders"
    )
    unavailable_signals: dict[str, str] = {}
    if not weekly_available:
        unavailable_signals["WEEKLY_MOMENTUM"] = "WEEKLY_CLOSE_UNAVAILABLE"
    if not relative_volume_available:
        unavailable_signals["RELATIVE_VOLUME"] = "RELATIVE_VOLUME_BASELINE_UNAVAILABLE"

    # The gateway only ranks the deliberately small, configured subscription
    # universe. Never infer a BIST-wide or relative-volume ranking when the
    # capability contract does not publish one.
    sources: dict[str, set[str]] = {}
    ranking_lists: list[tuple[str, str]] = []
    if weekly_available:
        ranking_lists.append(("WEEKLY_GAINER", "weeklyGainers"))
    else:
        ranking_lists.append(("DAILY_MOMENTUM_FALLBACK", "gainers"))
    if turnover_available:
        ranking_lists.append(("TURNOVER_LEADER", "volumeLeaders"))
    if relative_volume_available:
        ranking_lists.append(("RELATIVE_VOLUME", "relativeVolumeLeaders"))

    ranking_input_counts = {
        "WEEKLY_GAINER": 0,
        "TURNOVER_LEADER": 0,
        "RELATIVE_VOLUME": 0,
    }
    for source, key in ranking_lists:
        ranked_symbols = {
            str(symbol_raw).strip().upper()
            for symbol_raw in movers.get(key) or []
            if str(symbol_raw).strip().upper() in items
        }
        if source in ranking_input_counts:
            ranking_input_counts[source] = len(ranked_symbols)
        for symbol in ranked_symbols:
            sources.setdefault(symbol, set()).add(source)
    # A richer, capability-backed gateway may attach these fields directly to
    # ranking items. They do not trigger an extra market-wide subscription.
    for symbol, item in items.items():
        if (_to_float(item.get("changePct30m")) or 0) > 1:
            sources.setdefault(symbol, set()).add("MOMENTUM_30M")
        if (_to_float(item.get("changePct60m")) or 0) > 1:
            sources.setdefault(symbol, set()).add("MOMENTUM_60M")
        if relative_volume_available and (_to_float(item.get("relativeVolume")) or 0) >= 1.5:
            sources.setdefault(symbol, set()).add("RELATIVE_VOLUME")
        if item.get("breakout20Bar") is True:
            sources.setdefault(symbol, set()).add("BREAKOUT_20_BAR")

    historical_requested_count = 0
    historical_success_count = 0
    historical_used = False
    historical_relative_volume_available = False
    if not weekly_available or not relative_volume_available:
        historical_symbols = _historical_shortlist(items, policy.max_candidates)
        (
            historical_payloads,
            historical_requested_count,
            historical_success_count,
        ) = await _fetch_historical_bars(gw, historical_symbols, policy)
        weekly_metric_count = 0
        relative_volume_metric_count = 0
        historical_reasons: dict[str, str] = {}
        for symbol in historical_symbols:
            payload = historical_payloads.get(symbol)
            if payload is None:
                historical_reasons.setdefault(
                    "WEEKLY_MOMENTUM", "HISTORICAL_BARS_UNAVAILABLE"
                )
                historical_reasons.setdefault(
                    "RELATIVE_VOLUME", "HISTORICAL_BARS_UNAVAILABLE"
                )
                continue
            weekly_change, relative_volume, reasons = _historical_bar_metrics(payload)
            if not weekly_available and weekly_change is not None:
                weekly_metric_count += 1
                items[symbol]["weeklyChangePct"] = weekly_change
                if weekly_change > 0:
                    sources.setdefault(symbol, set()).add("WEEKLY_GAINER")
            elif "WEEKLY_MOMENTUM" in reasons:
                historical_reasons["WEEKLY_MOMENTUM"] = reasons["WEEKLY_MOMENTUM"]
            if not relative_volume_available and relative_volume is not None:
                relative_volume_metric_count += 1
                items[symbol]["relativeVolume"] = relative_volume
                if relative_volume >= 1.5:
                    sources.setdefault(symbol, set()).add("RELATIVE_VOLUME")
            elif "RELATIVE_VOLUME" in reasons:
                historical_reasons["RELATIVE_VOLUME"] = reasons["RELATIVE_VOLUME"]

        if not weekly_available and weekly_metric_count:
            ranking_input_counts["WEEKLY_GAINER"] = sum(
                "WEEKLY_GAINER" in symbol_sources
                for symbol_sources in sources.values()
            )
            unavailable_signals.pop("WEEKLY_MOMENTUM", None)
            historical_used = True
        elif not weekly_available:
            unavailable_signals["WEEKLY_MOMENTUM"] = historical_reasons.get(
                "WEEKLY_MOMENTUM", "HISTORICAL_WEEKLY_BASELINE_INSUFFICIENT"
            )
        if not relative_volume_available and relative_volume_metric_count:
            ranking_input_counts["RELATIVE_VOLUME"] = sum(
                "RELATIVE_VOLUME" in symbol_sources
                for symbol_sources in sources.values()
            )
            unavailable_signals.pop("RELATIVE_VOLUME", None)
            historical_relative_volume_available = True
            historical_used = True
        elif not relative_volume_available:
            unavailable_signals["RELATIVE_VOLUME"] = historical_reasons.get(
                "RELATIVE_VOLUME",
                "HISTORICAL_RELATIVE_VOLUME_BASELINE_INSUFFICIENT",
            )
    accepted: list[
        tuple[str, list[str], dict[str, Any], DiscoveryVerdict, dict[str, Any]]
    ] = []
    rejection_reason_counts: dict[str, int] = {}
    ranked_symbols = _limited_ranked_symbols(sources, items, policy.max_candidates)
    for symbol in ranked_symbols:
        candidate_sources = sources[symbol]
        item = items.get(symbol)
        if item is None:
            continue
        verdict, reason_code = await _screen(gw, symbol, item, policy)
        if verdict is None:
            if reason_code:
                rejection_reason_counts[reason_code] = (
                    rejection_reason_counts.get(reason_code, 0) + 1
                )
            continue
        if verdict.trend_pre_score < policy.minimum_trend_score:
            reason_code = "DISCOVERY_TREND_SCORE_BELOW_MINIMUM"
            rejection_reason_counts[reason_code] = (
                rejection_reason_counts.get(reason_code, 0) + 1
            )
            continue
        expanded_sources = sorted(
            candidate_sources | _derived_sources(
                verdict,
                allow_relative_volume=(
                    relative_volume_available
                    or historical_relative_volume_available
                ),
            )
        )
        quality = calculate_quality(item, verdict.wall_ratio)
        accepted.append((symbol, expanded_sources, item, verdict, quality))

    accepted.sort(
        key=lambda row: (
            row[3].trend_pre_score,
            _to_float(row[2].get("relativeVolume")) or 0,
            _volume_tl(row[2]),
        ),
        reverse=True,
    )
    if accepted:
        await _upsert_candidates(accepted, policy)
    await _expire_stale_candidates()

    ranking_sources = [
        source
        for source, _ in ranking_lists
        if any(source in symbol_sources for symbol_sources in sources.values())
    ]
    if historical_used:
        ranking_sources.append("HISTORICAL_BARS_FALLBACK")
    ranking_source = "+".join(ranking_sources) or "UNAVAILABLE"
    ranking_scope = (
        "NATIVE_MARKET_WIDE"
        if capability_contract.get("nativeMarketWide") is True
        else "HISTORICAL_BARS_FALLBACK"
        if historical_used
        else "CONFIGURED_UNIVERSE_FALLBACK"
        if sources
        else "UNAVAILABLE"
    )
    logger.info(
        "DISCOVERY_RANKING_COMPLETED universeCount=%s mergedCandidateCount=%s "
        "acceptedCount=%s filteredCount=%s rankingSource=%s weeklyGainerCount=%s "
        "turnoverLeaderCount=%s relativeVolumeLeaderCount=%s historicalBarRequestedCount=%s "
        "historicalBarSuccessCount=%s enrichmentCount=%s rankingScope=%s "
        "unavailableSignals=%s rejectionReasonCounts=%s",
        len(items),
        len(sources),
        len(accepted),
        sum(rejection_reason_counts.values()),
        ranking_source,
        ranking_input_counts["WEEKLY_GAINER"],
        ranking_input_counts["TURNOVER_LEADER"],
        ranking_input_counts["RELATIVE_VOLUME"],
        historical_requested_count,
        historical_success_count,
        len(ranked_symbols),
        ranking_scope,
        unavailable_signals,
        rejection_reason_counts,
    )
    return DiscoveryScanResult(
        [symbol for symbol, *_ in accepted],
        status="COMPLETED",
        universe_count=len(items),
        candidate_count=len(sources),
        ranking_source=ranking_source,
        ranking_scope=ranking_scope,
        weekly_gainer_count=ranking_input_counts["WEEKLY_GAINER"],
        turnover_leader_count=ranking_input_counts["TURNOVER_LEADER"],
        relative_volume_leader_count=ranking_input_counts["RELATIVE_VOLUME"],
        historical_bar_requested_count=historical_requested_count,
        historical_bar_success_count=historical_success_count,
        enrichment_count=len(ranked_symbols),
        filtered_count=sum(rejection_reason_counts.values()),
        rejection_reason_counts=rejection_reason_counts,
        unavailable_signals=unavailable_signals,
    )

async def _screen(
    gw: MatriksGatewayClient,
    symbol: str,
    item: dict[str, Any],
    policy: DiscoveryPolicy,
) -> tuple[DiscoveryVerdict | None, str | None]:
    change_pct = _to_float(item.get("changePct")) or 0.0
    weekly_change_pct = _to_float(item.get("weeklyChangePct"))
    volume_tl = _volume_tl(item)
    if _is_limit_locked(item, change_pct, policy):
        return None, "DISCOVERY_LIMIT_LOCKED"
    if abs(change_pct) >= policy.maximum_change_pct:
        return None, "DISCOVERY_DAILY_CHANGE_LIMIT"
    if weekly_change_pct is not None and weekly_change_pct >= policy.maximum_weekly_change_pct:
        return None, "DISCOVERY_WEEKLY_CHANGE_LIMIT"
    if volume_tl < policy.minimum_volume_tl:
        return None, "DISCOVERY_VOLUME_BELOW_MINIMUM"

    snapshot_payload: dict[str, Any] = {}
    try:
        snapshot = await gw.get_snapshot(symbol)
        snapshot_payload = snapshot.get("payload") or {}
    except (AttributeError, GatewayUnavailable, GatewayError):
        pass

    depth_payload: dict[str, Any] = snapshot_payload
    if not depth_payload:
        try:
            depth_payload = await gw.get_depth(symbol)
        except (AttributeError, GatewayUnavailable, GatewayError):
            depth_payload = {}
    wall_ratio = _ask_bid_ratio(depth_payload)
    analysis = (
        depth_payload.get("depthAnalysis")
        or depth_payload.get("analysis")
        or (depth_payload.get("payload") or {}).get("depthAnalysis")
        or {}
    )
    spread_pct = _first_float(
        analysis.get("spreadPct"), snapshot_payload.get("spreadPct")
    )
    if analysis.get("orderBookSignal") == "STRONG_SELL_PRESSURE":
        return None, "DISCOVERY_STRONG_SELL_PRESSURE"
    if spread_pct is not None and spread_pct > policy.maximum_spread_pct:
        return None, "DISCOVERY_SPREAD_ABOVE_MAXIMUM"
    if wall_ratio is not None and wall_ratio > policy.maximum_ask_bid_ratio:
        return None, "DISCOVERY_ASK_BID_IMBALANCE"

    combined = {**item, **snapshot_payload}
    combined["volumeTl"] = volume_tl
    combined["spreadPct"] = spread_pct
    combined["askBidRatio"] = wall_ratio
    combined["quoteAgeSeconds"] = _first_float(snapshot_payload.get("quoteAgeSeconds"), item.get("quoteAgeSeconds"))
    combined["snapshotAgeSeconds"] = _first_float(snapshot_payload.get("snapshotAgeSeconds"), snapshot_payload.get("ageSeconds"))
    if _is_stale(combined, policy):
        return None, "DISCOVERY_STALE_DATA"
    score, components = calculate_trend_pre_score(combined, policy)
    summary = {
        **components,
        "lastPrice": _to_float(combined.get("lastPrice")),
        "ema20": _to_float(combined.get("ema20")),
        "ema50": _to_float(combined.get("ema50")),
        "ema20Slope": _to_float(combined.get("ema20Slope")),
        "rsi": _to_float(combined.get("rsi") or combined.get("rsi14")),
        "adx": _to_float(combined.get("adx")),
        "macdState": combined.get("macdState") or combined.get("indicatorConsensus"),
        "natr": _to_float(combined.get("natr")),
        "spreadPct": spread_pct,
        "bidAskRatio": _first_float(
            analysis.get("bidAskRatioTop10"), combined.get("depthBidAskRatioTop10")
        ),
        "depthReliable": bool(
            analysis.get("depthReliable", combined.get("depthReliable", False))
        ),
        "breakout20Bar": bool(combined.get("breakout20Bar", False)),
        "limitLocked": _is_limit_locked(combined, change_pct, policy),
    }
    reason = (
        f"trendPreScore={score:.1f}; weeklyChangePct={weekly_change_pct!s}; changePctDaily={change_pct:+.2f}; "
        f"volumeTl={volume_tl:,.0f}"
    )
    return DiscoveryVerdict(reason, wall_ratio, score, summary), None


def calculate_trend_pre_score(
    data: dict[str, Any], policy: DiscoveryPolicy | None = None
) -> tuple[float, dict[str, Any]]:
    """Return a deterministic research-ranking score; it never grants trading permission."""
    policy = policy or DiscoveryPolicy()
    change = _to_float(data.get("changePct")) or 0.0
    weekly_change = _to_float(data.get("weeklyChangePct"))
    volume = _volume_tl(data)
    relative_volume = _to_float(data.get("relativeVolume"))
    price = _to_float(data.get("lastPrice"))
    ema20 = _to_float(data.get("ema20"))
    ema50 = _to_float(data.get("ema50"))
    ema20_slope = _to_float(data.get("ema20Slope"))
    rsi = _to_float(data.get("rsi") or data.get("rsi14"))
    spread = _to_float(data.get("spreadPct"))
    quote_age = _first_float(data.get("quoteAgeSeconds"), data.get("snapshotAgeSeconds"), data.get("ageSeconds"))
    limit_locked = _is_limit_locked(data, change, policy)
    overextended = change >= policy.maximum_change_pct or (
        weekly_change is not None and weekly_change >= policy.maximum_weekly_change_pct
    )

    score = 0.0
    if weekly_change is not None:
        score += 15 if 2 <= weekly_change < 10 else (8 if 0 < weekly_change < policy.maximum_weekly_change_pct else 0)
    score += 15 if 1 <= change < 5 else (7 if 0 < change < policy.maximum_change_pct else 0)
    if volume >= policy.minimum_volume_tl:
        score += 15 + (5 if volume >= policy.minimum_volume_tl * 2 else 0)
    score += 12 if relative_volume is not None and relative_volume >= 1.5 else 2
    score += 10 if price is not None and ema20 is not None and price > ema20 else 0
    trend_aligned = bool(
        (ema20 is not None and ema50 is not None and ema20 > ema50)
        or (ema20_slope is not None and ema20_slope > 0)
    )
    score += 12 if trend_aligned else 0
    score += 10 if rsi is not None and 52 <= rsi <= 70 else (4 if rsi is not None and 70 < rsi <= 75 else 0)
    score -= 10 if rsi is not None and rsi > 75 else 0
    score += 6 if spread is not None and spread <= policy.maximum_spread_pct else 0
    score += 5 if quote_age is not None and 0 <= quote_age <= policy.maximum_quote_age_seconds else 0
    score += 5 if data.get("breakout20Bar") is True else 0
    score += 5 if str(data.get("macdState") or "").upper() in {"BUY", "BULLISH"} else 0
    score -= 40 if limit_locked else 0
    score -= 25 if overextended else 0

    components = {
        "changePctDaily": change,
        "weeklyChangePct": weekly_change,
        "changePct30m": _to_float(data.get("changePct30m")),
        "changePct60m": _to_float(data.get("changePct60m")),
        "volumeTl": volume,
        "relativeVolume": relative_volume,
        "priceAboveEma20": bool(price is not None and ema20 is not None and price > ema20),
        "emaTrendAligned": trend_aligned,
        "quoteAgeSeconds": quote_age,
        "limitLocked": limit_locked,
        "overextended": overextended,
        "rsiOverbought": rsi is not None and rsi > 75,
    }
    return max(0.0, min(100.0, round(score, 2))), components


def _ranking_available(capabilities: dict[str, Any], name: str) -> bool:
    ranking = capabilities.get(name)
    return isinstance(ranking, dict) and ranking.get("available") is True


def _turnover_ranking_available(capabilities: dict[str, Any]) -> bool:
    ranking = capabilities.get("turnoverLeaders")
    return bool(
        isinstance(ranking, dict)
        and ranking.get("available") is True
        and ranking.get("semantic") == "CUMULATIVE_SESSION_TURNOVER_TL"
    )


def _historical_shortlist(
    items: dict[str, dict[str, Any]], limit: int
) -> list[str]:
    """Select a bounded configured-universe list before requesting bars."""
    return sorted(
        items,
        key=lambda symbol: (
            _volume_tl(items[symbol]),
            _to_float(items[symbol].get("changePct")) or 0.0,
            symbol,
        ),
        reverse=True,
    )[:limit]


async def _fetch_historical_bars(
    gateway: MatriksGatewayClient,
    symbols: list[str],
    policy: DiscoveryPolicy,
) -> tuple[dict[str, dict[str, Any]], int, int]:
    """Fetch bounded bar history with per-gateway TTL caching."""
    now = monotonic()
    payloads: dict[str, dict[str, Any]] = {}
    misses: list[str] = []
    for symbol in symbols:
        cached = _HISTORICAL_BARS_CACHE.get((id(gateway), symbol))
        if cached and now - cached[0] <= policy.historical_bars_ttl_seconds:
            payloads[symbol] = cached[1]
        else:
            misses.append(symbol)

    semaphore = asyncio.Semaphore(policy.max_concurrent_bar_requests)

    async def fetch(symbol: str) -> tuple[str, dict[str, Any] | None]:
        try:
            async with semaphore:
                response = await gateway.get_bars(symbol, count=50)
        except (AttributeError, GatewayUnavailable, GatewayError):
            return symbol, None
        if not response.get("available") or not isinstance(response.get("bars"), list):
            return symbol, None
        return symbol, response

    if misses:
        for symbol, response in await asyncio.gather(*(fetch(symbol) for symbol in misses)):
            if response is None:
                continue
            payloads[symbol] = response
            _HISTORICAL_BARS_CACHE[(id(gateway), symbol)] = (now, response)
    return payloads, len(misses), len(payloads)


def _historical_bar_metrics(
    payload: dict[str, Any],
) -> tuple[float | None, float | None, dict[str, str]]:
    """Derive weekly momentum and relative volume from reliable daily bars."""
    period = str(payload.get("actualBarPeriod") or payload.get("period") or "")
    normalized_period = period.strip().upper().replace("_", "")
    if normalized_period not in {"DAY", "DAILY", "D1", "1D"}:
        reason = "HISTORICAL_BARS_PERIOD_NOT_DAILY"
        return None, None, {
            "WEEKLY_MOMENTUM": reason,
            "RELATIVE_VOLUME": reason,
        }

    bars = [
        bar
        for bar in payload.get("bars") or []
        if isinstance(bar, dict)
        and bar.get("reliable") is True
        and bar.get("closed") is True
        and (_to_float(bar.get("close")) or 0) > 0
    ]
    reasons: dict[str, str] = {}
    weekly_change: float | None = None
    relative_volume: float | None = None

    if len(bars) >= 6:
        latest_close = _to_float(bars[-1].get("close"))
        prior_week_close = _to_float(bars[-6].get("close"))
        if latest_close is not None and prior_week_close and prior_week_close > 0:
            weekly_change = round(
                (latest_close - prior_week_close) / prior_week_close * 100, 4
            )
    if weekly_change is None:
        reasons["WEEKLY_MOMENTUM"] = "HISTORICAL_WEEKLY_BASELINE_INSUFFICIENT"

    if len(bars) >= _MIN_RELATIVE_VOLUME_BASELINE_BARS + 1:
        latest_volume = _to_float(bars[-1].get("volume"))
        baseline = [
            _to_float(bar.get("volume"))
            for bar in bars[-(_MIN_RELATIVE_VOLUME_BASELINE_BARS + 1) : -1]
        ]
        if (
            latest_volume is not None
            and latest_volume > 0
            and all(volume is not None and volume > 0 for volume in baseline)
        ):
            average_volume = sum(volume for volume in baseline if volume is not None) / len(
                baseline
            )
            if average_volume > 0:
                relative_volume = round(latest_volume / average_volume, 4)
    if relative_volume is None:
        reasons["RELATIVE_VOLUME"] = (
            "HISTORICAL_RELATIVE_VOLUME_BASELINE_INSUFFICIENT"
        )
    return weekly_change, relative_volume, reasons


def _limited_ranked_symbols(
    sources: dict[str, set[str]],
    items: dict[str, dict[str, Any]],
    limit: int,
) -> list[str]:
    """Merge and bound rankings before per-symbol enrichment calls."""
    return sorted(
        sources,
        key=lambda symbol: (
            len(sources[symbol]),
            "WEEKLY_GAINER" in sources[symbol],
            "TURNOVER_LEADER" in sources[symbol],
            _to_float(items[symbol].get("weeklyChangePct")) or float("-inf"),
            _volume_tl(items[symbol]),
            symbol,
        ),
        reverse=True,
    )[:limit]


def _derived_sources(
    verdict: DiscoveryVerdict, *, allow_relative_volume: bool
) -> set[str]:
    summary = verdict.technical_summary
    sources: set[str] = set()
    if allow_relative_volume and (summary.get("relativeVolume") or 0) >= 1.5:
        sources.add("RELATIVE_VOLUME")
    if summary.get("emaTrendAligned"):
        sources.add("EMA_UPTREND")
    if summary.get("breakout20Bar"):
        sources.add("BREAKOUT_20_BAR")
    return sources


async def _upsert_candidates(
    accepted: list[
        tuple[str, list[str], dict[str, Any], DiscoveryVerdict, dict[str, Any]]
    ],
    policy: DiscoveryPolicy,
) -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        for symbol, sources, item, verdict, quality in accepted:
            row = (
                await session.execute(
                    select(ResearchCandidate).where(ResearchCandidate.symbol == symbol)
                )
            ).scalar_one_or_none()
            created = row is None
            reactivated = False
            if row is None:
                row = ResearchCandidate(symbol=symbol, source=sources)
                session.add(row)
                await session.flush()
            elif row.status in {"REJECTED", "EXPIRED"}:
                row.status = "RESEARCH_PENDING"
                row.consecutive_pass_count = 0
                row.rejection_reason = None
                reactivated = True
            if row.status not in {"PROMOTED", "QUALIFIED"}:
                row.status = "RESEARCH_PENDING"
            row.source = sources
            row.trend_pre_score = verdict.trend_pre_score
            row.change_pct_daily = _to_float(item.get("changePct"))
            row.change_pct_30m = _to_float(item.get("changePct30m"))
            row.change_pct_60m = _to_float(item.get("changePct60m"))
            row.volume_tl = _volume_tl(item)
            row.relative_volume = _to_float(item.get("relativeVolume"))
            row.technical_summary = verdict.technical_summary
            row.last_detected_at = now
            row.expires_at = now + timedelta(hours=policy.candidate_ttl_hours)
            if created or reactivated:
                session.add(
                    ResearchCandidateEvent(
                        candidate_id=row.id,
                        symbol=symbol,
                        event_type="DETECTED",
                        details={
                            "sources": sources,
                            "trendPreScore": verdict.trend_pre_score,
                            "reason": verdict.reason,
                        },
                    )
                )

            # Legacy research report mirror. It is deliberately not an order list.
            legacy = (
                await session.execute(
                    select(WatchlistSymbol).where(WatchlistSymbol.symbol == symbol)
                )
            ).scalar_one_or_none()
            if legacy is None:
                session.add(
                    WatchlistSymbol(
                        symbol=symbol,
                        source=",".join(sources),
                        reason=verdict.reason,
                        change_pct=row.change_pct_daily,
                        volume=row.volume_tl,
                        is_active=True,
                    )
                )
            else:
                legacy.source = ",".join(sources)
                legacy.reason = verdict.reason
                legacy.change_pct = row.change_pct_daily
                legacy.volume = row.volume_tl
                legacy.is_active = True
                legacy.last_seen_at = now
            score_row = (
                await session.execute(
                    select(WatchlistQualityScore).where(
                        WatchlistQualityScore.symbol == symbol
                    )
                )
            ).scalar_one_or_none()
            values = {
                "quality_score": verdict.trend_pre_score,
                "momentum_score": quality["momentum"],
                "volume_score": quality["volume"],
                "depth_score": quality["depth"],
                "news_score": quality["news"],
                "risk_score": quality["risk"],
                "reason_json": {**quality, **verdict.technical_summary},
            }
            if score_row is None:
                session.add(WatchlistQualityScore(symbol=symbol, **values))
            else:
                for key, value in values.items():
                    setattr(score_row, key, value)
        await session.commit()


async def _expire_stale_candidates() -> None:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ResearchCandidate).where(
                        ResearchCandidate.expires_at < now,
                        ResearchCandidate.status.not_in(("PROMOTED", "EXPIRED")),
                    )
                )
            )
            .scalars()
            .all()
        )
        for row in rows:
            row.status = "EXPIRED"
            session.add(
                ResearchCandidateEvent(
                    candidate_id=row.id,
                    symbol=row.symbol,
                    event_type="EXPIRED",
                    details={"reason": "candidate TTL elapsed"},
                )
            )
        if rows:
            await session.execute(
                update(WatchlistSymbol)
                .where(WatchlistSymbol.symbol.in_([row.symbol for row in rows]))
                .values(is_active=False)
            )
        await session.commit()


async def list_active_watchlist_symbols() -> list[str]:
    """Compatibility alias: active discovery candidates, never order eligibility."""
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(ResearchCandidate.symbol).where(
                        ResearchCandidate.status.in_(
                            ("DETECTED", "RESEARCH_PENDING", "RESEARCHED", "QUALIFIED", "PROMOTED")
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
    return sorted(str(symbol).upper() for symbol in rows)

def _is_limit_locked(data: dict[str, Any], change_pct: float, policy: DiscoveryPolicy) -> bool:
    return bool(
        data.get("limitLocked") is True
        or data.get("isLimitLocked") is True
        or data.get("limitUpLocked") is True
        or data.get("limitDownLocked") is True
        or abs(change_pct) >= policy.maximum_change_pct
    )


def _is_stale(data: dict[str, Any], policy: DiscoveryPolicy) -> bool:
    age = _first_float(data.get("quoteAgeSeconds"), data.get("snapshotAgeSeconds"), data.get("ageSeconds"))
    return age is not None and (age < 0 or age > policy.maximum_quote_age_seconds)
def _ask_bid_ratio(depth: dict[str, Any]) -> float | None:
    payload = depth.get("payload") or depth
    analysis = payload.get("depthAnalysis") or payload.get("analysis") or {}
    bid_ask = _to_float(analysis.get("bidAskRatioTop25"))
    if bid_ask is not None and bid_ask > 0:
        return 1.0 / bid_ask
    bids = payload.get("bids") or []
    asks = payload.get("asks") or []
    total_bid = sum(_to_float(level.get("size")) or 0.0 for level in bids)
    total_ask = sum(_to_float(level.get("size")) or 0.0 for level in asks)
    if total_bid <= 0 or total_ask <= 0:
        return None
    return total_ask / total_bid


def _volume_tl(item: dict[str, Any]) -> float:
    if item.get("volumeSemantic") != "CUMULATIVE_SESSION_TURNOVER_TL":
        return 0.0
    return _to_float(item.get("sessionTurnoverTl")) or 0.0


def _first_float(*values: Any) -> float | None:
    for value in values:
        parsed = _to_float(value)
        if parsed is not None:
            return parsed
    return None


def _to_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and parsed not in (float("inf"), float("-inf")) else None
