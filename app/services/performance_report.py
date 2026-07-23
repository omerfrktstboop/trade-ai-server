"""Read-only decision, order, fill-ledger, and lifecycle performance
aggregates.

Task 2 replaces the naive qty x price-diff P&L estimate with a report built
from the real OrderFill/PositionLifecycle ledger (Task 1). The old
order-level estimate (``estimatedRealizedPnl``/``pnlExperimental``) is kept
unchanged for backward compatibility - it is still exactly what it always
was, a rough estimate from OrderLog.qty/price, clearly separate from the new
``pnlSource``-labeled ledger fields.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from statistics import median
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.db.session import async_session_factory
from app.models.db import (
    DecisionOutcome,
    MeasurementRepairJob,
    OrderFill,
    OrderLog,
    PositionLifecycle,
    PositionStopEvent,
    RiskDecision,
)
from app.services.block_reason_classifier import classify_block_reason
from app.services.event_counters import build_event_counters
from app.services.fill_ledger import to_decimal
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
)

# Round-trip'ler tamamlandıkları takvim gününe (BIST seansına) göre gruplanır.
# Sistem geneli gibi Europe/Istanbul yerel saatiyle: UTC'ye göre gruplasaydık
# akşam kapanan işlemler bir sonraki güne kayardı.
_ISTANBUL = ZoneInfo("Europe/Istanbul")


def range_start(value: str) -> datetime | None:
    hours = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}.get(value)
    return datetime.now(timezone.utc) - timedelta(hours=hours) if hours else None


def _f(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None


async def build_performance_report(
    range_value: str = "7d",
    symbol: str | None = None,
    gateway: MatriksGatewayClient | None = None,
) -> dict[str, Any]:
    since = range_start(range_value)
    symbol = symbol.upper() if symbol else None

    async with async_session_factory() as session:
        risks_stmt = select(RiskDecision)
        orders_stmt = select(OrderLog)
        closed_stmt = select(PositionLifecycle).where(PositionLifecycle.status == "CLOSED")
        open_stmt = select(PositionLifecycle).where(PositionLifecycle.status == "OPEN")
        fills_stmt = select(OrderFill)
        if since:
            risks_stmt = risks_stmt.where(RiskDecision.created_at >= since)
            orders_stmt = orders_stmt.where(OrderLog.created_at >= since)
            closed_stmt = closed_stmt.where(PositionLifecycle.closed_at >= since)
            fills_stmt = fills_stmt.where(OrderFill.filled_at >= since)
        if symbol:
            risks_stmt = risks_stmt.where(RiskDecision.symbol == symbol)
            orders_stmt = orders_stmt.where(OrderLog.symbol == symbol)
            closed_stmt = closed_stmt.where(PositionLifecycle.symbol == symbol)
            open_stmt = open_stmt.where(PositionLifecycle.symbol == symbol)
            fills_stmt = fills_stmt.where(OrderFill.symbol == symbol)

        outcomes_stmt = select(DecisionOutcome)
        if since:
            outcomes_stmt = outcomes_stmt.where(DecisionOutcome.decision_at >= since)
        if symbol:
            outcomes_stmt = outcomes_stmt.where(DecisionOutcome.symbol == symbol)

        risks = list((await session.execute(risks_stmt)).scalars().all())
        orders = list((await session.execute(orders_stmt)).scalars().all())
        closed_lifecycles = list((await session.execute(closed_stmt)).scalars().all())
        open_lifecycles = list((await session.execute(open_stmt)).scalars().all())
        fills = list((await session.execute(fills_stmt)).scalars().all())
        outcomes = list((await session.execute(outcomes_stmt)).scalars().all())

        entry_request_ids = [
            lc.entry_request_id for lc in closed_lifecycles if lc.entry_request_id
        ]
        outcomes_by_request: dict[str, DecisionOutcome] = {}
        if entry_request_ids:
            outcome_rows = (
                (
                    await session.execute(
                        select(DecisionOutcome).where(
                            DecisionOutcome.request_id.in_(entry_request_ids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            outcomes_by_request = {row.request_id: row for row in outcome_rows}

        close_reason_by_lifecycle: dict[int, str | None] = {}
        if closed_lifecycles:
            close_events = (
                (
                    await session.execute(
                        select(PositionStopEvent)
                        .where(
                            PositionStopEvent.position_lifecycle_id.in_(
                                [lc.id for lc in closed_lifecycles]
                            ),
                            PositionStopEvent.event_type == "POSITION_CLOSED",
                        )
                        .order_by(PositionStopEvent.created_at.desc())
                    )
                )
                .scalars()
                .all()
            )
            for event in close_events:
                close_reason_by_lifecycle.setdefault(
                    event.position_lifecycle_id, event.reason
                )

        # Repair-job counts are a live operational snapshot, not scoped to
        # the report's date range - a stuck job from last week is still
        # relevant right now (Task 8).
        repair_status_counts = Counter(
            row[0]
            for row in (
                await session.execute(select(MeasurementRepairJob.status))
            ).all()
        )

        # Faz 3.1: gerçek order/lifecycle olay sayaçları (risk kararları değil).
        event_counters = await build_event_counters(session, since)

    actions = Counter(row.action for row in risks)
    categories = Counter(
        classify_block_reason(row.reason) for row in risks if not row.allow_order
    )
    statuses = Counter(row.status.upper() for row in orders)
    by_symbol_decisions = Counter(row.symbol for row in risks)

    ledger = _build_ledger_summary(closed_lifecycles, outcomes_by_request)
    sessions = _session_summary(closed_lifecycles)
    slippage = _slippage_summary(fills)
    outcome_summary = _outcome_summary(outcomes)
    missing_fill_count = _count_missing_fills(orders, fills)
    unrealized_pnl, unrealized_available, unrealized_unavailable = (
        await _open_position_unrealized_pnl(open_lifecycles, gateway)
    )

    if closed_lifecycles:
        pnl_source = "FILL_LEDGER"
    elif any(row.status.upper() == "FILLED" for row in orders):
        pnl_source = "ORDER_LOG_FALLBACK"
    else:
        pnl_source = "UNAVAILABLE"

    return {
        "range": range_value,
        "symbol": symbol,
        "totalDecisions": len(risks),
        "buyCount": actions["BUY"],
        "sellCount": actions["SELL"],
        "waitCount": actions["WAIT"],
        "allowedOrders": sum(row.allow_order for row in risks),
        "blockedDecisions": sum(not row.allow_order for row in risks),
        "topBlockReason": categories.most_common(1)[0][0] if categories else "-",
        "topSymbols": by_symbol_decisions.most_common(10),
        "averageConfidence": round(sum(row.confidence for row in risks) / len(risks), 2)
        if risks
        else 0,
        "averageRiskScore": round(sum(row.risk_score for row in risks) / len(risks), 2)
        if risks
        else 0,
        "orderStatuses": dict(statuses),
        "ordersSent": statuses["SENT_PENDING"],
        "filledOrders": statuses["FILLED"],
        "rejectedOrders": statuses["REJECTED"],
        "errorOrders": statuses["ERROR"],
        "estimatedRealizedPnl": _estimated_pnl(orders),
        "pnlExperimental": True,
        "latestDecisions": risks[-50:],
        "pnlSource": pnl_source,
        "openPositionCount": len(open_lifecycles),
        "unrealizedPnl": unrealized_pnl,
        "unrealizedPnlAvailableCount": unrealized_available,
        "unrealizedPnlUnavailableCount": unrealized_unavailable,
        "recentClosedTrades": _recent_closed_trades(
            closed_lifecycles, close_reason_by_lifecycle
        ),
        "missingFillCount": missing_fill_count,
        "pendingRepairJobCount": repair_status_counts.get("PENDING", 0)
        + repair_status_counts.get("PROCESSING", 0),
        "failedRepairJobCount": repair_status_counts.get("FAILED", 0)
        + repair_status_counts.get("MANUAL_REVIEW", 0),
        "eventCounters": event_counters,
        **ledger,
        **sessions,
        **slippage,
        **outcome_summary,
    }


def _count_missing_fills(orders: list[OrderLog], fills: list[OrderFill]) -> int:
    """Orders with real filled_qty progress but a short OrderFill total -
    exactly what measurement_reconciliation.py looks for (Task 8)."""
    recorded_by_order: dict[int, Decimal] = defaultdict(lambda: Decimal("0"))
    for fill in fills:
        recorded_by_order[fill.order_log_id] += fill.fill_qty
    missing = 0
    for order in orders:
        filled_qty = to_decimal(order.filled_qty)
        if filled_qty is None or filled_qty <= 0:
            continue
        if recorded_by_order.get(order.id, Decimal("0")) < filled_qty:
            missing += 1
    return missing


def _recent_closed_trades(
    closed_lifecycles: list[PositionLifecycle],
    close_reason_by_lifecycle: dict[int, str | None],
    limit: int = 50,
) -> list[dict[str, Any]]:
    ordered = sorted(
        closed_lifecycles,
        key=lambda lc: lc.closed_at or lc.opened_at,
        reverse=True,
    )[:limit]
    trades: list[dict[str, Any]] = []
    for lc in ordered:
        total_qty = None
        average_exit_price = None
        if lc.average_entry_price and lc.average_entry_price > 0:
            total_qty = (lc.gross_buy_value_tl or Decimal("0")) / lc.average_entry_price
            if total_qty > 0:
                average_exit_price = (lc.gross_sell_value_tl or Decimal("0")) / total_qty
        trades.append(
            {
                "symbol": lc.symbol,
                "openedAt": lc.opened_at,
                "closedAt": lc.closed_at,
                "averageEntryPrice": _f(lc.average_entry_price),
                "averageExitPrice": _f(average_exit_price),
                "quantity": _f(total_qty),
                "grossPnl": _f(lc.gross_realized_pnl_tl),
                "totalCost": _f(
                    (lc.total_buy_cost_tl or Decimal("0"))
                    + (lc.total_sell_cost_tl or Decimal("0"))
                ),
                "netPnl": _f(lc.net_realized_pnl_tl),
                "stopLoss": _f(lc.active_stop_loss),
                "targetPrice": _f(lc.active_target_price),
                "closeReason": close_reason_by_lifecycle.get(lc.id),
                "strategyVersion": lc.strategy_version,
                "profileCode": lc.profile_code,
                "dataQuality": lc.data_quality,
                "pnlVerified": lc.pnl_verified,
            }
        )
    return trades


def _outcome_summary(outcomes: list[DecisionOutcome]) -> dict[str, Any]:
    status_counts = Counter(row.outcome_status for row in outcomes)
    mfe_values = [row.mfe_pct for row in outcomes if row.mfe_pct is not None]
    mae_values = [row.mae_pct for row in outcomes if row.mae_pct is not None]
    resolved = [row for row in outcomes if row.target_hit_before_stop is not None]
    target_first = sum(1 for row in resolved if row.target_hit_before_stop is True)
    stop_first = sum(1 for row in resolved if row.target_hit_before_stop is False)
    return {
        "outcomePendingCount": status_counts.get("PENDING", 0),
        "outcomePartialCount": status_counts.get("PARTIAL", 0),
        "outcomeCompleteCount": status_counts.get("COMPLETE", 0),
        "outcomeUnavailableCount": status_counts.get("UNAVAILABLE", 0),
        "outcomeAmbiguousCount": status_counts.get("AMBIGUOUS", 0),
        "outcomeDataGapCount": status_counts.get("DATA_GAP", 0),
        "averageMfePct": _f(sum(mfe_values) / len(mfe_values)) if mfe_values else None,
        "averageMaePct": _f(sum(mae_values) / len(mae_values)) if mae_values else None,
        "targetBeforeStopRatio": (
            round(target_first / len(resolved) * 100, 2) if resolved else None
        ),
        "stopBeforeTargetRatio": (
            round(stop_first / len(resolved) * 100, 2) if resolved else None
        ),
    }


def _build_ledger_summary(
    closed_lifecycles: list[PositionLifecycle],
    outcomes_by_request: dict[str, DecisionOutcome],
) -> dict[str, Any]:
    total = len(closed_lifecycles)
    # Task 8: strategy metrics (win rate, profit factor, avg win/loss, best/
    # worst trade) are computed only from pnl_verified=true lifecycles by
    # default - a backfilled position's unknown buy-cost history must never
    # silently distort them.
    verified = [lc for lc in closed_lifecycles if lc.pnl_verified]
    unverified = [lc for lc in closed_lifecycles if not lc.pnl_verified]
    reconciled_count = sum(1 for lc in closed_lifecycles if lc.data_quality == "RECONCILED")
    manual_review_count = sum(
        1 for lc in closed_lifecycles if lc.data_quality == "MANUAL_REVIEW"
    )

    wins = [lc for lc in verified if (lc.net_realized_pnl_tl or Decimal("0")) > 0]
    losses = [lc for lc in verified if (lc.net_realized_pnl_tl or Decimal("0")) < 0]

    verified_gross_pnl = sum((lc.gross_realized_pnl_tl or Decimal("0")) for lc in verified)
    verified_net_pnl = sum((lc.net_realized_pnl_tl or Decimal("0")) for lc in verified)
    unverified_net_pnl = sum((lc.net_realized_pnl_tl or Decimal("0")) for lc in unverified)
    total_cost = sum(
        (lc.total_buy_cost_tl or Decimal("0")) + (lc.total_sell_cost_tl or Decimal("0"))
        for lc in closed_lifecycles
    )
    win_sum = sum((lc.net_realized_pnl_tl or Decimal("0")) for lc in wins)
    loss_sum = sum((lc.net_realized_pnl_tl or Decimal("0")) for lc in losses)  # <= 0
    avg_win = (win_sum / len(wins)) if wins else None
    avg_loss = (loss_sum / len(losses)) if losses else None
    avg_win_loss_ratio = float(avg_win / abs(avg_loss)) if wins and losses else None
    # profit_factor = sum(gains) / abs(sum(losses)); never a fake infinity
    # when there are no losses (Task 2) - report None instead.
    profit_factor = float(win_sum / abs(loss_sum)) if losses else None
    best_trade = max((lc.net_realized_pnl_tl for lc in verified), default=None)
    worst_trade = min((lc.net_realized_pnl_tl for lc in verified), default=None)

    by_strategy: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "netPnl": Decimal("0")}
    )
    by_profile: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "netPnl": Decimal("0")}
    )
    by_symbol: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "netPnl": Decimal("0")}
    )
    by_regime: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "netPnl": Decimal("0")}
    )
    by_discovery: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"count": 0, "netPnl": Decimal("0")}
    )

    for lc in closed_lifecycles:
        net = lc.net_realized_pnl_tl or Decimal("0")
        by_strategy[lc.strategy_version or "UNKNOWN"]["count"] += 1
        by_strategy[lc.strategy_version or "UNKNOWN"]["netPnl"] += net
        by_profile[lc.profile_code or "UNKNOWN"]["count"] += 1
        by_profile[lc.profile_code or "UNKNOWN"]["netPnl"] += net
        by_symbol[lc.symbol]["count"] += 1
        by_symbol[lc.symbol]["netPnl"] += net

        outcome = (
            outcomes_by_request.get(lc.entry_request_id) if lc.entry_request_id else None
        )
        regime_key = (outcome.market_regime if outcome else None) or "UNKNOWN"
        by_regime[regime_key]["count"] += 1
        by_regime[regime_key]["netPnl"] += net
        sources = (outcome.discovery_sources if outcome else None) or ["UNKNOWN"]
        for source in sources:
            by_discovery[str(source)]["count"] += 1
            by_discovery[str(source)]["netPnl"] += net

    def _finalize(mapping: dict[str, dict[str, Any]]) -> dict[str, Any]:
        return {
            key: {"count": value["count"], "netPnl": _f(value["netPnl"])}
            for key, value in mapping.items()
        }

    return {
        "totalClosedTrades": total,
        "verifiedClosedTradeCount": len(verified),
        "unverifiedClosedTradeCount": len(unverified),
        "reconciledTradeCount": reconciled_count,
        "manualReviewTradeCount": manual_review_count,
        "winningTrades": len(wins),
        "losingTrades": len(losses),
        "winRate": round(len(wins) / len(verified) * 100, 2) if verified else None,
        "grossRealizedPnl": _f(verified_gross_pnl) if verified else None,
        "totalTransactionCost": _f(total_cost) if total else None,
        "netRealizedPnl": _f(verified_net_pnl) if verified else None,
        "verifiedGrossPnl": _f(verified_gross_pnl) if verified else None,
        "verifiedNetPnl": _f(verified_net_pnl) if verified else None,
        "unverifiedEstimatedPnl": _f(unverified_net_pnl) if unverified else None,
        "averageWin": _f(avg_win),
        "averageLoss": _f(avg_loss),
        "avgWinLossRatio": avg_win_loss_ratio,
        "profitFactor": profit_factor,
        "bestTrade": _f(best_trade),
        "worstTrade": _f(worst_trade),
        "resultsByStrategyVersion": _finalize(by_strategy),
        "resultsByProfileCode": _finalize(by_profile),
        "resultsBySymbol": _finalize(by_symbol),
        "resultsByMarketRegime": _finalize(by_regime),
        "resultsByDiscoverySource": _finalize(by_discovery),
    }


def _session_summary(closed_lifecycles: list[PositionLifecycle]) -> dict[str, Any]:
    """Tamamlanmış round-trip'leri BIST seansına (Europe/Istanbul takvim günü)
    göre grupla. Pilotun başarı kriteri "seans başına medyan 5-10 round trip"
    ve net beklenti seans düzeyinde okunabilsin diye (plan bölüm 8/11).

    PnL/beklenti metrikleri ``pnl_verified=true`` lifecycle'lardan hesaplanır
    (ledger özetiyle aynı ilke); round-trip sayımı ise gerçekten kapanmış tüm
    lifecycle'ları içerir çünkü reconciled bir çıkış da tamamlanmış bir
    round-trip'tir, yalnızca P&L'i doğrulanmamıştır.
    """
    by_day: dict[str, list[PositionLifecycle]] = defaultdict(list)
    for lc in closed_lifecycles:
        if lc.closed_at is None:
            continue
        closed_at = lc.closed_at
        if closed_at.tzinfo is None:
            closed_at = closed_at.replace(tzinfo=timezone.utc)
        day = closed_at.astimezone(_ISTANBUL).date().isoformat()
        by_day[day].append(lc)

    sessions: list[dict[str, Any]] = []
    for day in sorted(by_day):
        day_lcs = by_day[day]
        verified = [lc for lc in day_lcs if lc.pnl_verified]
        wins = [lc for lc in verified if (lc.net_realized_pnl_tl or Decimal("0")) > 0]
        net_pnl = sum((lc.net_realized_pnl_tl or Decimal("0")) for lc in verified)
        gross_pnl = sum((lc.gross_realized_pnl_tl or Decimal("0")) for lc in verified)
        cost = sum(
            (lc.total_buy_cost_tl or Decimal("0")) + (lc.total_sell_cost_tl or Decimal("0"))
            for lc in day_lcs
        )
        hold_minutes = [
            (lc.closed_at - lc.opened_at).total_seconds() / 60
            for lc in day_lcs
            if lc.closed_at is not None and lc.opened_at is not None
        ]
        sessions.append(
            {
                "date": day,
                "completedRoundTrips": len(day_lcs),
                "verifiedRoundTrips": len(verified),
                "winningTrades": len(wins),
                "winRate": round(len(wins) / len(verified) * 100, 2) if verified else None,
                "grossPnl": _f(gross_pnl) if verified else None,
                "netPnl": _f(net_pnl) if verified else None,
                "transactionCost": _f(cost) if day_lcs else None,
                # Net beklenti = doğrulanmış round-trip başına ortalama net P&L.
                "netExpectancyPerTrip": _f(net_pnl / len(verified)) if verified else None,
                "avgHoldingMinutes": round(sum(hold_minutes) / len(hold_minutes), 1)
                if hold_minutes
                else None,
            }
        )

    round_trip_counts = [s["completedRoundTrips"] for s in sessions]
    return {
        "sessions": sessions,
        "sessionCount": len(sessions),
        "medianRoundTripsPerSession": float(median(round_trip_counts))
        if round_trip_counts
        else None,
    }


def _slippage_summary(fills: list[OrderFill]) -> dict[str, Any]:
    buy_slippages = [
        f.slippage_tl for f in fills if f.action == "BUY" and f.slippage_tl is not None
    ]
    sell_slippages = [
        f.slippage_tl for f in fills if f.action == "SELL" and f.slippage_tl is not None
    ]
    all_slippages = buy_slippages + sell_slippages
    return {
        "totalBuySlippageTl": _f(sum(buy_slippages)) if buy_slippages else None,
        "totalSellSlippageTl": _f(sum(sell_slippages)) if sell_slippages else None,
        "averageSlippageTl": (
            _f(sum(all_slippages) / len(all_slippages)) if all_slippages else None
        ),
        "slippageAvailableFillCount": len(all_slippages),
        "slippageUnavailableFillCount": len(fills) - len(all_slippages),
    }


async def _open_position_unrealized_pnl(
    open_lifecycles: list[PositionLifecycle], gateway: MatriksGatewayClient | None
) -> tuple[float | None, int, int]:
    """Unrealized P&L only for lifecycles where a fresh, reliable price was
    obtainable (Task 2) - a position is never dropped just because its price
    lookup failed; it is simply excluded from the summed total and counted
    as unavailable instead."""
    if not open_lifecycles or gateway is None:
        return None, 0, len(open_lifecycles)
    total = Decimal("0")
    available = 0
    for lifecycle in open_lifecycles:
        if lifecycle.average_entry_price is None or lifecycle.current_qty is None:
            continue
        try:
            snapshot = await gateway.get_snapshot(lifecycle.symbol)
        except (GatewayUnavailable, GatewayError):
            continue
        except Exception:
            continue
        payload = snapshot.get("payload") or {}
        if not payload.get("quoteReliable"):
            continue
        price = to_decimal(payload.get("lastPrice"))
        if price is None or price <= 0:
            continue
        total += lifecycle.current_qty * (price - lifecycle.average_entry_price)
        available += 1
    unavailable = len(open_lifecycles) - available
    return (_f(total) if available else None), available, unavailable


def _estimated_pnl(orders: list[OrderLog]) -> float:
    """Unchanged legacy estimate - order-level qty x price-diff, kept only
    as a backward-compatible fallback display value (see pnlSource)."""
    costs: dict[str, tuple[float, float]] = {}
    pnl = 0.0
    for row in sorted(
        (r for r in orders if r.status.upper() == "FILLED"), key=lambda r: r.created_at
    ):
        qty, price = row.qty, row.price or 0.0
        held, cost = costs.get(row.symbol, (0.0, 0.0))
        if row.action.upper() == "BUY":
            costs[row.symbol] = (
                held + qty,
                ((held * cost) + qty * price) / (held + qty),
            )
        elif row.action.upper() == "SELL" and held:
            sold = min(held, qty)
            pnl += sold * (price - cost)
            costs[row.symbol] = (held - sold, cost)
    return round(pnl, 2)
