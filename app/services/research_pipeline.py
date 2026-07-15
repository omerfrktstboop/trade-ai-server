"""AI research, two-pass promotion, and trade-watchlist lifecycle."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.db import (
    ResearchCandidate,
    ResearchCandidateEvent,
    TradeWatchlistSymbol,
)
from app.models.signal import SignalAction, SignalMode
from app.services.admin_config import list_admin_configs
from app.services.evaluator import EvaluationResult, evaluate_symbol
from app.services.matriks_gateway import MatriksGatewayClient, gateway_client

logger = logging.getLogger(__name__)

ResearchEvaluator = Callable[..., Awaitable[EvaluationResult | None]]


@dataclass(frozen=True)
class ResearchPolicy:
    discovery_interval_minutes: int = 5
    max_candidates_per_cycle: int = 10
    max_active_symbols: int = 10
    max_concurrent_evaluations: int = 2
    cooldown_minutes: int = 15
    max_trade_watchlist_size: int = 20
    minimum_trend_score: float = 60.0
    minimum_research_score: float = 75.0
    minimum_confidence: float = 75.0
    maximum_risk_score: float = 35.0
    consecutive_passes: int = 2
    minimum_pass_interval_minutes: int = 10
    candidate_ttl_hours: int = 24
    trade_watchlist_ttl_hours: int = 24
    minimum_volume_tl: float = 100_000_000.0
    maximum_spread_pct: float = 0.50
    declined_symbols: frozenset[str] = frozenset()


async def load_research_policy(session: AsyncSession | None = None) -> ResearchPolicy:
    owns_session = session is None
    if session is None:
        session = async_session_factory()
    try:
        values = {item.key: item.value for item in await list_admin_configs(session)}
        return ResearchPolicy(
            discovery_interval_minutes=max(1, int(values["discoveryIntervalMinutes"])),
            max_candidates_per_cycle=max(
                1, int(values["maxResearchCandidatesPerCycle"])
            ),
            max_active_symbols=max(1, int(values["maxActiveResearchSymbols"])),
            max_concurrent_evaluations=max(
                1, int(values["maxConcurrentResearchEvaluations"])
            ),
            cooldown_minutes=max(1, int(values["candidateCooldownMinutes"])),
            max_trade_watchlist_size=max(1, int(values["maxTradeWatchlistSize"])),
            minimum_trend_score=float(values["minimumTrendPreScore"]),
            minimum_research_score=float(values["minimumResearchScore"]),
            minimum_confidence=float(values["researchMinimumConfidence"]),
            maximum_risk_score=float(values["researchMaximumRiskScore"]),
            consecutive_passes=max(2, int(values["promotionConsecutivePasses"])),
            minimum_pass_interval_minutes=max(
                1, int(values["promotionMinIntervalMinutes"])
            ),
            candidate_ttl_hours=max(1, int(values["researchCandidateTtlHours"])),
            trade_watchlist_ttl_hours=max(1, int(values["tradeWatchlistTtlHours"])),
            minimum_volume_tl=float(values["discoveryMinimumVolumeTl"]),
            maximum_spread_pct=float(values["discoveryMaximumSpreadPct"]),
            declined_symbols=frozenset(
                symbol.strip().upper()
                for symbol in values["declineSymbols"].split(",")
                if symbol.strip()
            ),
        )
    finally:
        if owns_session:
            await session.close()


async def run_research_cycle(
    gateway: MatriksGatewayClient | None = None,
    *,
    evaluator: ResearchEvaluator | None = None,
) -> list[str]:
    """Evaluate a bounded candidate batch in forced research/PAPER mode."""
    gw = gateway or gateway_client
    evaluator = evaluator or evaluate_symbol
    policy = await load_research_policy()
    now = datetime.now(UTC)
    cooldown_cutoff = now - timedelta(minutes=policy.cooldown_minutes)
    async with async_session_factory() as session:
        candidates = (
            (
                await session.execute(
                    select(ResearchCandidate)
                    .where(
                        ResearchCandidate.status.in_(
                            ("RESEARCH_PENDING", "RESEARCHED", "QUALIFIED")
                        ),
                        ResearchCandidate.trend_pre_score >= policy.minimum_trend_score,
                        ResearchCandidate.expires_at >= now,
                        (ResearchCandidate.last_evaluated_at.is_(None))
                        | (ResearchCandidate.last_evaluated_at <= cooldown_cutoff),
                    )
                    .order_by(
                        ResearchCandidate.trend_pre_score.desc(),
                        ResearchCandidate.relative_volume.desc(),
                        ResearchCandidate.volume_tl.desc(),
                    )
                    .limit(
                        min(
                            policy.max_candidates_per_cycle,
                            policy.max_active_symbols,
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        snapshots = [_candidate_context(row) for row in candidates]

    semaphore = asyncio.Semaphore(policy.max_concurrent_evaluations)

    async def evaluate_one(context: dict[str, Any]) -> str | None:
        async with semaphore:
            symbol = str(context["symbol"])
            try:
                result = await evaluator(
                    symbol,
                    gateway=gw,
                    mode=SignalMode.PAPER,
                    force_paper=True,
                    evaluation_purpose="RESEARCH_DISCOVERY",
                    research_context=context,
                )
            except Exception:
                logger.exception("Research evaluation failed symbol=%s", symbol)
                return None
            if result is None:
                return None
            await apply_research_result(symbol, result, policy=policy)
            return symbol

    evaluated = [
        item
        for item in await asyncio.gather(*(evaluate_one(c) for c in snapshots))
        if item
    ]
    return evaluated


async def apply_research_result(
    symbol: str,
    result: EvaluationResult,
    *,
    policy: ResearchPolicy | None = None,
    now: datetime | None = None,
) -> bool:
    """Persist research output and promote only after every hard gate passes."""
    now = now or datetime.now(UTC)
    symbol = symbol.strip().upper()
    async with async_session_factory() as session:
        policy = policy or await load_research_policy(session)
        candidate = (
            await session.execute(
                select(ResearchCandidate).where(ResearchCandidate.symbol == symbol)
            )
        ).scalar_one_or_none()
        if candidate is None:
            return False

        response = result.response
        research_score = _float_or_none(result.research_score)
        candidate.ai_action = (result.raw_action or response.action).value
        candidate.ai_research_score = research_score
        candidate.ai_confidence_score = response.confidence_score
        candidate.ai_risk_score = response.risk_score
        candidate.ai_reason = response.reason
        candidate.ai_stop_loss = _float_or_none(response.stop_loss)
        candidate.ai_target_price = _float_or_none(response.target_price)
        candidate.last_evaluated_at = now
        candidate.status = "RESEARCHED"

        qualified, reason, reward_risk = _promotion_verdict(candidate, result, policy)
        session.add(
            ResearchCandidateEvent(
                candidate_id=candidate.id,
                symbol=symbol,
                event_type="RESEARCHED",
                details={
                    "researchScore": research_score,
                    "confidence": response.confidence_score,
                    "riskScore": response.risk_score,
                    "action": candidate.ai_action,
                    "rewardRiskRatio": reward_risk,
                    "qualified": qualified,
                    "reason": reason,
                },
            )
        )
        logger.info(
            "Research evaluated symbol=%s trendPreScore=%.1f researchScore=%.1f action=%s",
            symbol,
            candidate.trend_pre_score,
            research_score if research_score is not None else -1.0,
            candidate.ai_action,
        )

        if not qualified:
            # A discovery candidate is a monitoring object, not an order
            # permission. A transient trade gate (for example an unavailable
            # depth event timestamp) must not evict a liquid trend symbol
            # from periodic research. Only promotion into the separate Trade
            # Watchlist remains strict.
            candidate.status = "RESEARCHED"
            candidate.consecutive_pass_count = 0
            candidate.rejected_at = None
            candidate.rejection_reason = reason
            session.add(
                ResearchCandidateEvent(
                    candidate_id=candidate.id,
                    symbol=symbol,
                    event_type="MONITORING",
                    details={"reason": candidate.rejection_reason},
                )
            )
            logger.info(
                "Research candidate retained for monitoring symbol=%s "
                "researchScore=%.1f reason=%s",
                symbol,
                research_score if research_score is not None else -1.0,
                reason,
            )
            await session.commit()
            return False

        previous_success = _as_utc(candidate.last_successful_evaluation_at)
        minimum_gap = timedelta(minutes=policy.minimum_pass_interval_minutes)
        time_spaced_success = (
            previous_success is None or now - previous_success >= minimum_gap
        )
        if not time_spaced_success:
            # A second qualifying response inside the promotion interval is
            # evidence for monitoring only. It cannot reuse a prior pass to
            # trigger a Trade Watchlist promotion.
            candidate.rejection_reason = "PROMOTION_PASSES_NOT_TIME_SPACED"
            await session.commit()
            return False

        candidate.consecutive_pass_count += 1
        candidate.last_successful_evaluation_at = now
        candidate.status = "QUALIFIED"
        candidate.rejection_reason = None
        if candidate.consecutive_pass_count < policy.consecutive_passes:
            await session.commit()
            return False

        active_count = int(
            (
                await session.execute(
                    select(func.count(TradeWatchlistSymbol.id)).where(
                        TradeWatchlistSymbol.is_active.is_(True),
                        (TradeWatchlistSymbol.expires_at.is_(None))
                        | (TradeWatchlistSymbol.expires_at >= now),
                    )
                )
            ).scalar_one()
            or 0
        )
        watch = (
            await session.execute(
                select(TradeWatchlistSymbol).where(
                    TradeWatchlistSymbol.symbol == symbol
                )
            )
        ).scalar_one_or_none()
        if (
            watch is None or not watch.is_active
        ) and active_count >= policy.max_trade_watchlist_size:
            candidate.rejection_reason = "PROMOTION_WATCHLIST_CAPACITY_REACHED"
            await session.commit()
            return False

        promotion_reason = (
            f"{candidate.consecutive_pass_count} consecutive research passes; "
            f"researchScore={research_score:.1f}; rewardRisk={reward_risk:.2f}"
        )
        if watch is None:
            watch = TradeWatchlistSymbol(symbol=symbol)
            session.add(watch)
        watch.is_active = True
        watch.source = "RESEARCH_PROMOTION"
        watch.manual_override = False
        watch.promotion_reason = promotion_reason
        watch.research_score = research_score
        watch.confidence_score = response.confidence_score
        watch.risk_score = response.risk_score
        watch.consecutive_fail_count = 0
        watch.eligible_at = now
        watch.last_qualified_at = now
        watch.expires_at = now + timedelta(hours=policy.trade_watchlist_ttl_hours)
        watch.removed_at = None
        watch.removal_reason = None
        candidate.status = "PROMOTED"
        candidate.promoted_at = now
        session.add(
            ResearchCandidateEvent(
                candidate_id=candidate.id,
                symbol=symbol,
                event_type="PROMOTED",
                details={
                    "reason": promotion_reason,
                    "consecutivePasses": candidate.consecutive_pass_count,
                },
            )
        )
        await session.commit()
        logger.info(
            "Research candidate promoted symbol=%s consecutivePasses=%s",
            symbol,
            candidate.consecutive_pass_count,
        )
        return True


def _promotion_verdict(
    candidate: ResearchCandidate,
    result: EvaluationResult,
    policy: ResearchPolicy,
) -> tuple[bool, str, float | None]:
    response = result.response
    raw_action = result.raw_action or response.action
    if candidate.symbol in policy.declined_symbols:
        return False, "PROMOTION_SYMBOL_DECLINED", None
    if raw_action != SignalAction.BUY:
        return False, "PROMOTION_ACTION_NOT_BUY", None
    research_score = _float_or_none(result.research_score)
    confidence = _float_or_none(response.confidence_score)
    risk_score = _float_or_none(response.risk_score)
    trend_score = _float_or_none(candidate.trend_pre_score)
    volume_tl = _float_or_none(candidate.volume_tl)
    if research_score is None or research_score < policy.minimum_research_score:
        return False, "PROMOTION_RESEARCH_SCORE_BELOW_MINIMUM", None
    if confidence is None or confidence < policy.minimum_confidence:
        return False, "PROMOTION_CONFIDENCE_BELOW_MINIMUM", None
    if risk_score is None or risk_score > policy.maximum_risk_score:
        return False, "PROMOTION_RISK_SCORE_ABOVE_MAXIMUM", None
    if trend_score is None or trend_score < policy.minimum_trend_score:
        return False, "PROMOTION_TREND_SCORE_BELOW_MINIMUM", None
    if (
        response.entry_range is None
        or response.stop_loss is None
        or response.target_price is None
    ):
        return False, "PROMOTION_PRICE_LEVELS_MISSING", None
    reward_risk = _reward_risk_ratio(
        response.entry_range.max, response.stop_loss, response.target_price
    )
    if reward_risk is None or reward_risk < 1.5:
        return False, "PROMOTION_REWARD_RISK_BELOW_MINIMUM", reward_risk
    summary = candidate.technical_summary or {}
    if bool(summary.get("limitLocked")):
        return False, "PROMOTION_LIMIT_LOCKED", reward_risk
    if summary.get("priceAboveEma20") is not True:
        return False, "PROMOTION_PRICE_BELOW_EMA20", reward_risk
    if summary.get("emaTrendAligned") is not True:
        return False, "PROMOTION_EMA_TREND_NOT_ALIGNED", reward_risk
    ema20_slope = _float_or_none(summary.get("ema20Slope"))
    if ema20_slope is None or ema20_slope < 0:
        return False, "PROMOTION_EMA20_SLOPE_INVALID", reward_risk
    if volume_tl is None or volume_tl < policy.minimum_volume_tl:
        return False, "PROMOTION_VOLUME_BELOW_MINIMUM", reward_risk
    spread = _float_or_none(summary.get("spreadPct"))
    if spread is None or spread > policy.maximum_spread_pct:
        return False, "PROMOTION_SPREAD_ABOVE_MAXIMUM", reward_risk
    if summary.get("depthReliable") is not True:
        return False, "PROMOTION_DEPTH_UNRELIABLE", reward_risk
    return True, "PROMOTION_ELIGIBLE", reward_risk


async def list_trade_eligible_symbols() -> list[str]:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        rows = (
            (
                await session.execute(
                    select(TradeWatchlistSymbol.symbol).where(
                        TradeWatchlistSymbol.is_active.is_(True),
                        (TradeWatchlistSymbol.expires_at.is_(None))
                        | (TradeWatchlistSymbol.expires_at >= now),
                    )
                )
            )
            .scalars()
            .all()
        )
    return sorted(str(symbol).upper() for symbol in rows)


async def is_trade_eligible(symbol: str, session: AsyncSession | None = None) -> bool:
    symbol = symbol.strip().upper()
    owns_session = session is None
    if session is None:
        session = async_session_factory()
    try:
        now = datetime.now(UTC)
        row = (
            await session.execute(
                select(TradeWatchlistSymbol.id).where(
                    TradeWatchlistSymbol.symbol == symbol,
                    TradeWatchlistSymbol.is_active.is_(True),
                    (TradeWatchlistSymbol.expires_at.is_(None))
                    | (TradeWatchlistSymbol.expires_at >= now),
                )
            )
        ).scalar_one_or_none()
        return row is not None
    finally:
        if owns_session:
            await session.close()


async def add_manual_trade_symbol(
    session: AsyncSession, symbol: str, *, reason: str
) -> TradeWatchlistSymbol:
    """Explicit operator override; distinct from automatic research promotion."""
    symbol = symbol.strip().upper()
    row = (
        await session.execute(
            select(TradeWatchlistSymbol).where(TradeWatchlistSymbol.symbol == symbol)
        )
    ).scalar_one_or_none()
    now = datetime.now(UTC)
    if row is None:
        row = TradeWatchlistSymbol(symbol=symbol)
        session.add(row)
    row.is_active = True
    row.manual_override = True
    row.source = "MANUAL_OVERRIDE"
    row.promotion_reason = reason
    row.eligible_at = now
    row.last_qualified_at = now
    row.expires_at = None
    row.removed_at = None
    row.removal_reason = None
    await session.flush()
    return row


async def record_trade_watchlist_decision(result: EvaluationResult) -> None:
    """Remove stale/broken symbols while leaving held-position SELL scans intact."""
    symbol = result.response.symbol.strip().upper()
    action = result.raw_action or result.response.action
    async with async_session_factory() as session:
        row = (
            await session.execute(
                select(TradeWatchlistSymbol).where(
                    TradeWatchlistSymbol.symbol == symbol,
                    TradeWatchlistSymbol.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if row is None or row.manual_override:
            return
        immediate_reason = None
        if result.response.confidence_score < 60 and action == SignalAction.BUY:
            immediate_reason = "confidence below 60"
        elif result.research_score is not None and result.research_score < 60:
            immediate_reason = "research score below 60"
        if immediate_reason:
            await _remove_watchlist_row(session, row, immediate_reason)
        elif action in {SignalAction.WAIT, SignalAction.SELL}:
            row.consecutive_fail_count += 1
            if row.consecutive_fail_count >= 2:
                await _remove_watchlist_row(
                    session, row, f"AI produced {action.value} twice"
                )
        else:
            row.consecutive_fail_count = 0
        await session.commit()


async def maintain_trade_watchlist(declined_symbols: set[str]) -> list[str]:
    now = datetime.now(UTC)
    removed: list[str] = []
    async with async_session_factory() as session:
        policy = await load_research_policy(session)
        rows = (
            (
                await session.execute(
                    select(TradeWatchlistSymbol).where(
                        TradeWatchlistSymbol.is_active.is_(True)
                    )
                )
            )
            .scalars()
            .all()
        )
        candidates = {
            row.symbol: row
            for row in (
                (
                    await session.execute(
                        select(ResearchCandidate).where(
                            ResearchCandidate.symbol.in_([item.symbol for item in rows])
                        )
                    )
                )
                .scalars()
                .all()
            )
        }
        for row in rows:
            candidate = candidates.get(row.symbol)
            summary = candidate.technical_summary if candidate else {}
            reason = None
            if row.symbol in declined_symbols:
                reason = "symbol added to declineSymbols"
            elif row.manual_override:
                continue
            elif row.expires_at and _as_utc(row.expires_at) < now:
                reason = "trade watchlist TTL elapsed"
            elif candidate is None or candidate.last_evaluated_at is None:
                reason = "research state unavailable"
            elif now - _as_utc(candidate.last_evaluated_at) > timedelta(
                hours=policy.trade_watchlist_ttl_hours
            ):
                reason = "last successful evaluation older than TTL"
            elif (
                candidate.ai_research_score is not None
                and candidate.ai_research_score < 60
            ):
                reason = "research score below 60"
            elif summary and summary.get("priceAboveEma20") is False:
                reason = "price fell below EMA20"
            elif (
                _float_or_none(summary.get("ema20Slope")) is not None
                and float(summary["ema20Slope"]) < 0
            ):
                reason = "EMA20 slope turned negative"
            elif float(candidate.volume_tl or 0) < policy.minimum_volume_tl:
                reason = "volume support lost"
            elif (
                _float_or_none(summary.get("spreadPct")) is not None
                and float(summary["spreadPct"]) > policy.maximum_spread_pct
            ):
                reason = "spread deteriorated"
            if reason:
                await _remove_watchlist_row(session, row, reason)
                removed.append(row.symbol)
        await session.commit()
    return removed


async def _remove_watchlist_row(
    session: AsyncSession, row: TradeWatchlistSymbol, reason: str
) -> None:
    now = datetime.now(UTC)
    row.is_active = False
    row.removed_at = now
    row.removal_reason = reason
    candidate = (
        await session.execute(
            select(ResearchCandidate).where(ResearchCandidate.symbol == row.symbol)
        )
    ).scalar_one_or_none()
    if candidate is not None:
        candidate.status = "RESEARCHED"
        session.add(
            ResearchCandidateEvent(
                candidate_id=candidate.id,
                symbol=row.symbol,
                event_type="WATCHLIST_REMOVED",
                details={"reason": reason},
            )
        )
    logger.info("Trade watchlist removed symbol=%s reason=%s", row.symbol, reason)


async def get_pipeline_counts() -> dict[str, int]:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        candidate_rows = (
            await session.execute(
                select(
                    ResearchCandidate.status, func.count(ResearchCandidate.id)
                ).group_by(ResearchCandidate.status)
            )
        ).all()
        status_counts = {str(status): int(count) for status, count in candidate_rows}
        trade_count = int(
            (
                await session.execute(
                    select(func.count(TradeWatchlistSymbol.id)).where(
                        TradeWatchlistSymbol.is_active.is_(True),
                        (TradeWatchlistSymbol.expires_at.is_(None))
                        | (TradeWatchlistSymbol.expires_at >= now),
                    )
                )
            ).scalar_one()
            or 0
        )
    return {
        "researchCandidateCount": sum(status_counts.values()),
        "pendingResearchCount": status_counts.get("RESEARCH_PENDING", 0),
        "qualifiedCandidateCount": status_counts.get("QUALIFIED", 0),
        "promotedCandidateCount": status_counts.get("PROMOTED", 0),
        "tradeWatchlistCount": trade_count,
    }


def _candidate_context(row: ResearchCandidate) -> dict[str, Any]:
    summary = row.technical_summary or {}
    return {
        "symbol": row.symbol,
        "evaluationPurpose": "RESEARCH_DISCOVERY",
        "trendPreScore": row.trend_pre_score,
        "candidateSource": list(row.source or []),
        "recentTrend": {
            "changePct30m": row.change_pct_30m,
            "changePct60m": row.change_pct_60m,
            "changePctDaily": row.change_pct_daily,
            "relativeVolume": row.relative_volume,
            "volumeTl": row.volume_tl,
            "ema20Slope": summary.get("ema20Slope"),
        },
    }


def _reward_risk_ratio(entry: Any, stop: Any, target: Any) -> float | None:
    try:
        entry_d = Decimal(str(entry))
        stop_d = Decimal(str(stop))
        target_d = Decimal(str(target))
        risk = entry_d - stop_d
        if risk <= 0 or target_d <= entry_d:
            return None
        return float((target_d - entry_d) / risk)
    except (InvalidOperation, TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return (
        parsed
        if parsed == parsed and parsed not in (float("inf"), float("-inf"))
        else None
    )


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
