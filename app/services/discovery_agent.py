"""Low-cost market discovery that creates research-only candidates.

Discovery never grants order permission.  It ranks the broad, data-only scan
universe with Matriks movers/snapshot data and persists candidates for the
separate AI research pipeline.  The legacy ``watchlist_symbols`` tables are
still mirrored for backwards-compatible admin reports, but the order scanner
does not consume them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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


@dataclass(frozen=True)
class DiscoveryPolicy:
    minimum_trend_score: float = 60.0
    minimum_volume_tl: float = 100_000_000.0
    maximum_change_pct: float = 9.3
    maximum_spread_pct: float = 0.50
    maximum_ask_bid_ratio: float = 3.0
    candidate_ttl_hours: int = 24


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
    )


async def run_discovery_scan(
    gateway: MatriksGatewayClient | None = None,
) -> list[str]:
    """Screen movers and upsert research candidates; never trade symbols."""
    gw = gateway or gateway_client
    policy = await load_discovery_policy()
    try:
        movers = await gw.get_movers(limit=50)
    except (GatewayUnavailable, GatewayError) as exc:
        logger.debug("Movers unavailable: %s", exc)
        return []
    if not movers.get("available"):
        return []

    items = {
        str(item.get("symbol") or "").strip().upper(): item
        for item in movers.get("items") or []
        if item.get("symbol")
    }
    sources: dict[str, set[str]] = {}
    for source, key in (("GAINER", "gainers"), ("VOLUME_LEADER", "volumeLeaders")):
        for symbol_raw in movers.get(key) or []:
            symbol = str(symbol_raw).strip().upper()
            if symbol:
                sources.setdefault(symbol, set()).add(source)

    # Movers may already expose richer fields in future gateway versions.
    # Include those candidates without requiring a new endpoint contract.
    for symbol, item in items.items():
        if (_to_float(item.get("changePct30m")) or 0) > 1:
            sources.setdefault(symbol, set()).add("MOMENTUM_30M")
        if (_to_float(item.get("changePct60m")) or 0) > 1:
            sources.setdefault(symbol, set()).add("MOMENTUM_60M")
        if (_to_float(item.get("relativeVolume")) or 0) >= 1.5:
            sources.setdefault(symbol, set()).add("RELATIVE_VOLUME")
        if item.get("breakout20Bar") is True:
            sources.setdefault(symbol, set()).add("BREAKOUT_20_BAR")

    accepted: list[
        tuple[str, list[str], dict[str, Any], DiscoveryVerdict, dict[str, Any]]
    ] = []
    for symbol, candidate_sources in sources.items():
        item = items.get(symbol)
        if item is None:
            continue
        verdict = await _screen(gw, symbol, item, policy)
        if verdict is None or verdict.trend_pre_score < policy.minimum_trend_score:
            continue
        expanded_sources = sorted(candidate_sources | _derived_sources(verdict))
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

    logger.info(
        "Discovery scanned universe=%s candidates=%s shortlisted=%s",
        movers.get("universeSize", len(items)),
        len(sources),
        len(accepted),
    )
    return [symbol for symbol, *_ in accepted]


async def _screen(
    gw: MatriksGatewayClient,
    symbol: str,
    item: dict[str, Any],
    policy: DiscoveryPolicy,
) -> DiscoveryVerdict | None:
    change_pct = _to_float(item.get("changePct")) or 0.0
    volume_tl = _volume_tl(item)
    if abs(change_pct) >= policy.maximum_change_pct:
        return None
    if volume_tl < policy.minimum_volume_tl:
        return None

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
        return None
    if spread_pct is not None and spread_pct > policy.maximum_spread_pct:
        return None
    if wall_ratio is not None and wall_ratio > policy.maximum_ask_bid_ratio:
        return None

    combined = {**item, **snapshot_payload}
    combined["volumeTl"] = volume_tl
    combined["spreadPct"] = spread_pct
    combined["askBidRatio"] = wall_ratio
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
        "limitLocked": False,
    }
    reason = (
        f"trendPreScore={score:.1f}; changePctDaily={change_pct:+.2f}; "
        f"volumeTl={volume_tl:,.0f}"
    )
    return DiscoveryVerdict(reason, wall_ratio, score, summary)


def calculate_trend_pre_score(
    data: dict[str, Any], policy: DiscoveryPolicy | None = None
) -> tuple[float, dict[str, Any]]:
    """Return a deterministic 0-100 trend score and auditable components."""
    policy = policy or DiscoveryPolicy()
    score = 0.0
    change = _to_float(data.get("changePct")) or 0.0
    volume = _volume_tl(data)
    relative_volume = _to_float(data.get("relativeVolume"))
    price = _to_float(data.get("lastPrice"))
    ema20 = _to_float(data.get("ema20"))
    ema50 = _to_float(data.get("ema50"))
    ema20_slope = _to_float(data.get("ema20Slope"))
    rsi = _to_float(data.get("rsi") or data.get("rsi14"))
    spread = _to_float(data.get("spreadPct"))

    if 1 < change < policy.maximum_change_pct:
        score += 20
    elif 0 < change < policy.maximum_change_pct:
        score += 10
    if volume >= policy.minimum_volume_tl:
        score += 20
        if volume >= policy.minimum_volume_tl * 2:
            score += 5
    score += 15 if relative_volume is not None and relative_volume >= 1.5 else 5
    score += 15 if price and ema20 and price > ema20 else (5 if ema20 is None else 0)
    trend_aligned = bool(
        (ema20 is not None and ema50 is not None and ema20 > ema50)
        or (ema20_slope is not None and ema20_slope > 0)
    )
    score += 15 if trend_aligned else (5 if ema20 is None and ema20_slope is None else 0)
    score += 10 if rsi is not None and 50 <= rsi <= 75 else (5 if rsi is None else 0)
    score += 5 if spread is None or spread <= policy.maximum_spread_pct else 0
    if data.get("breakout20Bar") is True:
        score += 5
    if str(data.get("macdState") or "").upper() in {"BUY", "BULLISH"}:
        score += 5

    components = {
        "changePctDaily": change,
        "changePct30m": _to_float(data.get("changePct30m")),
        "changePct60m": _to_float(data.get("changePct60m")),
        "volumeTl": volume,
        "relativeVolume": relative_volume,
        "priceAboveEma20": bool(price and ema20 and price > ema20),
        "emaTrendAligned": trend_aligned,
    }
    return min(100.0, round(score, 2)), components


def _derived_sources(verdict: DiscoveryVerdict) -> set[str]:
    summary = verdict.technical_summary
    sources: set[str] = set()
    if (summary.get("relativeVolume") or 0) >= 1.5:
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
    return _first_float(
        item.get("volumeTl"), item.get("sessionTurnoverTl"), item.get("volume")
    ) or 0.0


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
