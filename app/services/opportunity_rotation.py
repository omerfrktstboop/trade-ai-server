"""Durable, fill-confirmed rotation from a weaker bot holding to a better BUY."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select

from app.db.session import async_session_factory
from app.models.db import (
    AiDecision,
    MarketSnapshot,
    OrderLog,
    PositionLifecycle,
    RiskDecision as RiskDecisionModel,
    RotationPlan,
)
from app.models.signal import OrderType, SignalAction, SignalResponse
from app.services.admin_config import (
    build_runtime_risk_config,
    get_admin_config_value,
)
from app.services.bot_ownership import BotOwnershipSnapshot, load_bot_ownership
from app.services.cash_reservation import acquire_account_reservation_lock
from app.services.daily_trade_count import get_today_trade_counts
from app.services.effective_risk_config import resolve_effective_risk_config
from app.services.evaluation.pipeline import EvaluationResult
from app.services.order_ledger import FINAL_STATES, PENDING_STATES
from app.services.position_sizing import PositionSizingService

logger = logging.getLogger(__name__)

ACTIVE_ROTATION_STATES = {
    "PLANNED",
    "SELL_PENDING",
    "SELL_FILLED_WAIT_REFRESH",
    "CASH_CONFIRMED",
    "BUY_PENDING",
    "MANUAL_REVIEW",
}


@dataclass(frozen=True)
class RotationPolicy:
    enabled: bool
    minimum_score_advantage: Decimal
    minimum_return_advantage_pct: Decimal
    review_interval: timedelta
    assessment_max_age: timedelta
    minimum_holding_age: timedelta
    plan_expiry: timedelta
    max_per_day: int


@dataclass(frozen=True)
class OpportunityAssessment:
    request_id: str
    symbol: str
    action: str
    score: Decimal
    expected_return_pct: Decimal
    created_at: datetime


EvaluateFunc = Callable[[str], Awaitable[EvaluationResult | None]]
DispatchFunc = Callable[[EvaluationResult], Awaitable[Any]]


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return result if result.is_finite() else None


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _position_value(row: dict[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        if key in row:
            return _decimal(row[key])
    return None


def _position_row(positions: dict[str, Any], symbol: str) -> dict[str, Any] | None:
    normalized = symbol.strip().upper()
    for row in positions.get("positions") or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbol") or row.get("Symbol") or "").strip().upper() == normalized:
            return row
    return None


def _payload_age_seconds(payload: dict[str, Any]) -> Decimal | None:
    for key in ("accountDataAgeSeconds", "snapshotAgeSeconds", "dataAgeSeconds"):
        if key in payload:
            age = _decimal(payload.get(key))
            return age if age is not None and age >= 0 else None
    for key in ("receivedAtUtc", "receivedAt", "timestamp"):
        value = payload.get(key)
        if not value:
            continue
        try:
            observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return max(
            Decimal("0"),
            Decimal(str((datetime.now(timezone.utc) - _aware(observed)).total_seconds())),
        )
    return None


def _account_buying_power(account: dict[str, Any]) -> Decimal | None:
    values = account.get("account")
    if not isinstance(values, dict):
        return None
    indexed = {str(key).casefold(): value for key, value in values.items()}
    for key in (
        "OrderableCash",
        "AvailableBuyingPower",
        "AvailableBalanceForBuyOrders",
        "PurchasingPower",
        "AvailableMargin",
    ):
        if key.casefold() in indexed:
            return _decimal(indexed[key.casefold()])
    return None


async def load_rotation_policy(session) -> RotationPolicy:
    values = {
        key: await get_admin_config_value(session, key)
        for key in (
            "portfolioRotationEnabled",
            "rotationMinimumOpportunityScoreAdvantage",
            "rotationMinimumExpectedReturnAdvantagePct",
            "rotationReviewIntervalMinutes",
            "rotationAssessmentMaxAgeMinutes",
            "rotationMinimumHoldingMinutes",
            "rotationPlanExpiryMinutes",
            "rotationMaxPerDay",
        )
    }
    return RotationPolicy(
        enabled=str(values["portfolioRotationEnabled"]).lower() == "true",
        minimum_score_advantage=Decimal(
            values["rotationMinimumOpportunityScoreAdvantage"]
        ),
        minimum_return_advantage_pct=Decimal(
            values["rotationMinimumExpectedReturnAdvantagePct"]
        ),
        review_interval=timedelta(
            minutes=max(1, int(values["rotationReviewIntervalMinutes"]))
        ),
        assessment_max_age=timedelta(
            minutes=max(1, int(values["rotationAssessmentMaxAgeMinutes"]))
        ),
        minimum_holding_age=timedelta(
            minutes=max(1, int(values["rotationMinimumHoldingMinutes"]))
        ),
        plan_expiry=timedelta(
            minutes=max(1, int(values["rotationPlanExpiryMinutes"]))
        ),
        max_per_day=max(0, int(values["rotationMaxPerDay"])),
    )


async def _active_plan(session, account_ref: str) -> RotationPlan | None:
    return (
        await session.execute(
            select(RotationPlan)
            .where(
                RotationPlan.account_ref == account_ref,
                RotationPlan.state.in_(ACTIVE_ROTATION_STATES),
            )
            .order_by(RotationPlan.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _assessment_from_rows(
    decision: AiDecision, snapshot: MarketSnapshot
) -> OpportunityAssessment | None:
    raw = decision.raw_response if isinstance(decision.raw_response, dict) else {}
    score = _decimal(raw.get("opportunity_score", raw.get("opportunityScore")))
    target = _decimal(raw.get("target_price", raw.get("targetPrice")))
    current = _decimal(snapshot.close)
    action = str(raw.get("action") or decision.action or "WAIT").upper()
    if (
        score is None
        or not Decimal("0") <= score <= Decimal("100")
        or target is None
        or current is None
        or target <= 0
        or current <= 0
        or action not in {"BUY", "SELL", "WAIT"}
    ):
        return None
    return OpportunityAssessment(
        request_id=decision.request_id,
        symbol=decision.symbol.strip().upper(),
        action=action,
        score=score,
        expected_return_pct=(target - current) * Decimal("100") / current,
        created_at=_aware(decision.created_at),
    )


async def _latest_assessment(
    session,
    symbol: str,
    *,
    exclude_request_id: str | None = None,
) -> OpportunityAssessment | None:
    statement = (
        select(AiDecision, MarketSnapshot)
        .join(MarketSnapshot, MarketSnapshot.request_id == AiDecision.request_id)
        .where(AiDecision.symbol == symbol.strip().upper())
        .order_by(AiDecision.created_at.desc())
        .limit(12)
    )
    if exclude_request_id:
        statement = statement.where(AiDecision.request_id != exclude_request_id)
    for decision, snapshot in (await session.execute(statement)).all():
        assessment = _assessment_from_rows(decision, snapshot)
        if assessment is not None:
            return assessment
    return None


async def _strict_source_qty(
    session,
    positions: dict[str, Any],
    symbol: str,
    *,
    account_ref: str,
    ownership: BotOwnershipSnapshot,
) -> tuple[int | None, str | None]:
    normalized = symbol.strip().upper()
    runtime_config = await build_runtime_risk_config(session)
    if runtime_config.is_long_term_locked(normalized):
        return None, "source symbol is long-term locked"
    if str(positions.get("accountRef") or "").strip() != account_ref:
        return None, "gateway positions belong to a different account"

    row = _position_row(positions, normalized)
    if row is None:
        return None, "source position is absent from gateway"
    bot_qty = _position_value(row, "botQty", "botPositionQty")
    total_qty = _position_value(row, "totalQty", "accountNetQty", "qty")
    sellable_qty = _position_value(row, "sellableQty")
    locked_qty = _position_value(row, "lockedLongTermQty")
    values = (bot_qty, total_qty, sellable_qty, locked_qty)
    if any(value is None or value != value.to_integral_value() for value in values):
        return None, "source ownership quantities are unavailable or fractional"
    if locked_qty != 0:
        return None, "source position has locked quantity"
    if bot_qty <= 0 or not (bot_qty == total_qty == sellable_qty):
        return None, "source contains manual, unavailable, or non-bot quantity"

    ledger_qty = ownership.quantities.get(normalized, Decimal("0"))
    if ledger_qty != bot_qty:
        return None, "current-account bot ledger does not match gateway ownership"

    lifecycle = (
        await session.execute(
            select(PositionLifecycle)
            .where(
                PositionLifecycle.symbol == normalized,
                PositionLifecycle.status == "OPEN",
            )
            .order_by(PositionLifecycle.opened_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if (
        lifecycle is None
        or lifecycle.current_qty != bot_qty
        or lifecycle.data_quality not in {"VERIFIED", "RECONCILED"}
        or lifecycle.is_backfilled
    ):
        return None, "source lifecycle is missing or not fully verified"
    entry_order = (
        await session.execute(
            select(OrderLog.id)
            .where(
                OrderLog.request_id == lifecycle.entry_request_id,
                OrderLog.account_ref == account_ref,
                OrderLog.request_fingerprint.is_not(None),
                OrderLog.action == "BUY",
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if entry_order is None:
        return None, "source lifecycle is not bound to the current account ledger"

    pending = (
        await session.execute(
            select(OrderLog.id)
            .where(
                OrderLog.symbol == normalized,
                OrderLog.status.in_(PENDING_STATES),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if pending is not None:
        return None, "source symbol already has a pending order"
    return int(bot_qty), None


def _candidate_expected_return(candidate: EvaluationResult) -> Decimal | None:
    entry = _decimal(candidate.decision_entry_price)
    target = _decimal(candidate.decision_target_price)
    if entry is None or target is None or entry <= 0 or target <= entry:
        return None
    return (target - entry) * Decimal("100") / entry


def _target_has_required_advantage(
    candidate: EvaluationResult,
    plan: RotationPlan,
    policy: RotationPolicy,
) -> bool:
    target_score = _decimal(candidate.opportunity_score)
    target_return = _candidate_expected_return(candidate)
    return bool(
        candidate.raw_action == SignalAction.BUY
        and candidate.decision_source == "llm"
        and candidate.rotation_eligible
        and target_score is not None
        and target_return is not None
        and target_score - Decimal(str(plan.source_opportunity_score))
        >= policy.minimum_score_advantage
        and target_return - Decimal(str(plan.source_expected_return_pct))
        >= policy.minimum_return_advantage_pct
    )


def _projected_post_sale_size(
    candidate: EvaluationResult,
    *,
    source_value_tl: Decimal,
) -> int:
    account = candidate.sizing_account
    trade = candidate.sizing_trade
    limits = candidate.effective_limits
    if (
        account is None
        or trade is None
        or limits is None
        or source_value_tl <= 0
        or account.effective_available_cash_tl is None
        or account.total_account_exposure_tl is None
        or account.total_bot_exposure_tl is None
    ):
        return 0
    conservative_proceeds = source_value_tl * Decimal("0.98")
    projected = account.model_copy(
        update={
            "effective_available_cash_tl": (
                account.effective_available_cash_tl + conservative_proceeds
            ),
            "total_account_exposure_tl": max(
                Decimal("0"), account.total_account_exposure_tl - source_value_tl
            ),
            "total_bot_exposure_tl": max(
                Decimal("0"), account.total_bot_exposure_tl - source_value_tl
            ),
        }
    )
    result = PositionSizingService().calculate_buy_size(
        account=projected,
        trade=trade,
        limits=limits,
    )
    return result.qty if result.allowed else 0


async def maybe_create_rotation_plan(
    candidates: list[EvaluationResult],
    *,
    gateway: Any,
    account_ref: str | None,
) -> RotationPlan | None:
    """Create one confirmed-opportunity plan; never sends an order."""
    if not account_ref or len(account_ref) != 64 or not candidates:
        return None
    try:
        positions = await gateway.get_positions()
    except Exception:
        logger.exception("ROTATION_PLAN_POSITIONS_UNAVAILABLE")
        return None
    if not positions.get("positionsLoaded") or not positions.get("snapshotCompleteFlag"):
        return None
    if str(positions.get("accountRef") or "").strip() != account_ref:
        return None

    now = datetime.now(timezone.utc)
    async with async_session_factory() as session:
        await acquire_account_reservation_lock(session)
        policy = await load_rotation_policy(session)
        limits = await resolve_effective_risk_config(session)
        ownership = await load_bot_ownership(session, account_ref)
        if (
            not policy.enabled
            or policy.max_per_day <= 0
            or limits.total_bot_capital_budget_tl <= 0
            or await _active_plan(session, account_ref) is not None
        ):
            await session.rollback()
            return None

        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
        started_today = (
            await session.execute(
                select(func.count())
                .select_from(RotationPlan)
                .where(
                    RotationPlan.account_ref == account_ref,
                    RotationPlan.created_at >= start_of_day,
                )
            )
        ).scalar_one()
        if started_today >= policy.max_per_day:
            await session.rollback()
            return None

        any_pending_order = (
            await session.execute(
                select(OrderLog.id)
                .where(OrderLog.status.in_(PENDING_STATES))
                .limit(1)
            )
        ).scalar_one_or_none()
        if any_pending_order is not None:
            await session.rollback()
            return None

        held_symbols = sorted(ownership.quantities)
        ranked_candidates = sorted(
            candidates,
            key=lambda item: item.opportunity_score or -1,
            reverse=True,
        )
        for candidate in ranked_candidates:
            if (
                candidate.decision_source != "llm"
                or candidate.raw_action != SignalAction.BUY
                or candidate.opportunity_score is None
                or candidate.target_allocation_pct is None
                or not candidate.rotation_eligible
                or candidate.response.action != SignalAction.WAIT
                or candidate.response.allow_order
                or not set(candidate.sizing_binding_limits)
                & {"bot_budget", "cash_budget", "account_exposure"}
            ):
                continue
            target_score = _decimal(candidate.opportunity_score)
            target_return = _candidate_expected_return(candidate)
            if target_score is None or target_return is None:
                continue
            previous_target = await _latest_assessment(
                session,
                candidate.response.symbol,
                exclude_request_id=candidate.response.request_id,
            )
            if (
                previous_target is None
                or previous_target.action != "BUY"
                or now - previous_target.created_at > policy.assessment_max_age
                or candidate.decision_created_utc - previous_target.created_at
                < policy.review_interval
                or abs(previous_target.score - target_score) > Decimal("10")
            ):
                continue

            best_source: tuple[Decimal, str, OpportunityAssessment, int] | None = None
            for source_symbol in held_symbols:
                if source_symbol == candidate.response.symbol.strip().upper():
                    continue
                source_qty, rejection = await _strict_source_qty(
                    session,
                    positions,
                    source_symbol,
                    account_ref=account_ref,
                    ownership=ownership,
                )
                if rejection or source_qty is None:
                    continue
                try:
                    source_snapshot = await gateway.get_snapshot(source_symbol)
                except Exception:
                    continue
                source_price = _decimal(
                    (source_snapshot.get("payload") or {}).get("lastPrice")
                )
                if (
                    source_price is None
                    or source_price <= 0
                    or source_qty > limits.max_qty_per_order
                    or Decimal(source_qty) * source_price
                    > limits.max_order_value_tl
                    or _projected_post_sale_size(
                        candidate,
                        source_value_tl=Decimal(source_qty) * source_price,
                    )
                    <= 0
                ):
                    continue
                lifecycle = (
                    await session.execute(
                        select(PositionLifecycle)
                        .where(
                            PositionLifecycle.symbol == source_symbol,
                            PositionLifecycle.status == "OPEN",
                        )
                        .order_by(PositionLifecycle.opened_at.desc())
                        .limit(1)
                    )
                ).scalar_one()
                if now - _aware(lifecycle.opened_at) < policy.minimum_holding_age:
                    continue
                source = await _latest_assessment(session, source_symbol)
                if (
                    source is None
                    or source.action != "WAIT"
                    or now - source.created_at > policy.assessment_max_age
                ):
                    continue
                score_advantage = target_score - source.score
                return_advantage = target_return - source.expected_return_pct
                if (
                    score_advantage < policy.minimum_score_advantage
                    or return_advantage < policy.minimum_return_advantage_pct
                ):
                    continue
                if best_source is None or source.score < best_source[0]:
                    best_source = (source.score, source_symbol, source, source_qty)

            if best_source is None:
                continue
            _, source_symbol, source, source_qty = best_source
            generation = positions.get("snapshotGeneration")
            generation_int = int(generation) if generation is not None else None
            plan = RotationPlan(
                account_ref=account_ref,
                source_symbol=source_symbol,
                target_symbol=candidate.response.symbol.strip().upper(),
                source_qty=source_qty,
                state="PLANNED",
                source_opportunity_score=float(source.score),
                target_opportunity_score=float(target_score),
                source_expected_return_pct=float(source.expected_return_pct),
                target_expected_return_pct=float(target_return),
                source_assessment_request_id=source.request_id,
                target_assessment_request_id=candidate.response.request_id,
                source_position_generation=generation_int,
                not_before=now + policy.review_interval,
                expires_at=now + policy.plan_expiry,
            )
            session.add(plan)
            await session.commit()
            await session.refresh(plan)
            logger.warning(
                "ROTATION_PLANNED id=%s source=%s target=%s qty=%s scoreAdvantage=%s",
                plan.id,
                plan.source_symbol,
                plan.target_symbol,
                plan.source_qty,
                target_score - source.score,
            )
            return plan
        await session.rollback()
    return None


async def _set_plan_state(
    plan_id: int,
    state: str,
    *,
    expected_state: str,
    failure_reason: str | None = None,
    buy_request_id: str | None = None,
    target_qty: int | None = None,
    source_fill_position_generation: int | None = None,
) -> bool:
    async with async_session_factory() as session:
        plan = await session.get(RotationPlan, plan_id, with_for_update=True)
        if plan is None or plan.state != expected_state:
            return False
        plan.state = state
        plan.failure_reason = failure_reason
        if buy_request_id is not None:
            plan.buy_request_id = buy_request_id
        if target_qty is not None:
            plan.target_qty = target_qty
        if source_fill_position_generation is not None:
            plan.source_fill_position_generation = source_fill_position_generation
        await session.commit()
        return True


async def _rotation_sell_result(plan: RotationPlan, gateway: Any) -> EvaluationResult | None:
    snapshot = await gateway.get_snapshot(plan.source_symbol)
    price = _decimal((snapshot.get("payload") or {}).get("lastPrice"))
    if price is None or price <= 0:
        return None
    request_id = plan.sell_request_id or f"rotation-{plan.id}-sell"
    reason = (
        f"Portfolio rotation {plan.source_symbol}->{plan.target_symbol}; "
        "source opportunity is materially weaker"
    )
    async with async_session_factory() as session:
        exists = (
            await session.execute(
                select(RiskDecisionModel.id).where(
                    RiskDecisionModel.request_id == request_id
                )
            )
        ).scalar_one_or_none()
        if exists is None:
            session.add(
                RiskDecisionModel(
                    request_id=request_id,
                    symbol=plan.source_symbol,
                    action="SELL",
                    confidence=100.0,
                    risk_score=0.0,
                    # This row is a committed dispatch audit, not an accepted
                    # order. The OrderLog reservation becomes the daily-slot
                    # source only after scanner preflight succeeds.
                    allow_order=False,
                    reason=reason,
                    order_type="LIMIT",
                    qty=plan.source_qty,
                )
            )
            await session.commit()
    return EvaluationResult(
        response=SignalResponse(
            requestId=request_id,
            symbol=plan.source_symbol,
            action=SignalAction.SELL,
            qty=plan.source_qty,
            orderType=OrderType.LIMIT,
            price=price,
            confidenceScore=100.0,
            riskScore=0.0,
            allowOrder=True,
            reason=reason,
        ),
        dispatch_eligible=True,
        decision_created_utc=datetime.now(timezone.utc),
        evaluation_purpose="ROTATION",
        raw_action=SignalAction.SELL,
        decision_source="rotation",
    )


async def advance_rotation_plan(
    *,
    gateway: Any,
    account_ref: str | None,
    evaluate: EvaluateFunc,
    dispatch: DispatchFunc,
) -> bool:
    """Advance at most one durable state per call; return whether BUYs stay blocked."""
    if not account_ref or len(account_ref) != 64:
        return False
    async with async_session_factory() as session:
        plan = await _active_plan(session, account_ref)
        if plan is None:
            return False
        policy = await load_rotation_policy(session)
    now = datetime.now(timezone.utc)
    policy_disabled = not policy.enabled
    plan_expired = now >= _aware(plan.expires_at)
    if policy_disabled and plan.state in {"PLANNED", "CASH_CONFIRMED"}:
        await _set_plan_state(
            plan.id,
            "MANUAL_REVIEW" if plan.state == "CASH_CONFIRMED" else "ABORTED",
            expected_state=plan.state,
            failure_reason=(
                "rotation disabled after source SELL"
                if plan.state == "CASH_CONFIRMED"
                else "rotation disabled"
            ),
        )
        return True
    if plan_expired and plan.state in {"PLANNED", "CASH_CONFIRMED"}:
        await _set_plan_state(
            plan.id,
            "MANUAL_REVIEW" if plan.state == "CASH_CONFIRMED" else "EXPIRED",
            expected_state=plan.state,
            failure_reason=(
                "plan expired after source SELL"
                if plan.state == "CASH_CONFIRMED"
                else "plan expired"
            ),
        )
        return True

    if plan.state == "PLANNED":
        if now < _aware(plan.not_before):
            return True
        target = await evaluate(plan.target_symbol)
        if (
            target is None
            or not _target_has_required_advantage(target, plan, policy)
            or target.response.action != SignalAction.WAIT
            or target.response.allow_order
            or not set(target.sizing_binding_limits)
            & {"bot_budget", "cash_budget", "account_exposure"}
        ):
            await _set_plan_state(
                plan.id,
                "ABORTED",
                expected_state="PLANNED",
                failure_reason="target is not a fully viable capital-blocked BUY",
            )
            return True
        positions = await gateway.get_positions()
        async with async_session_factory() as session:
            limits = await resolve_effective_risk_config(session)
            counts = await get_today_trade_counts(session, plan.source_symbol)
            target_counts = await get_today_trade_counts(
                session, plan.target_symbol
            )
            if (
                counts.bot_count + 2 > limits.daily_order_limit
                or counts.symbol_count + 1
                > limits.per_symbol_daily_order_limit
                or target_counts.symbol_count + 1
                > limits.per_symbol_daily_order_limit
            ):
                await _set_plan_state(
                    plan.id,
                    "ABORTED",
                    expected_state="PLANNED",
                    failure_reason="daily order limit reached before rotation SELL",
                )
                return True
            ownership = await load_bot_ownership(session, account_ref)
            qty, rejection = await _strict_source_qty(
                session,
                positions,
                plan.source_symbol,
                account_ref=account_ref,
                ownership=ownership,
            )
            if rejection or qty != plan.source_qty:
                await _set_plan_state(
                    plan.id,
                    "ABORTED",
                    expected_state="PLANNED",
                    failure_reason=rejection or "source quantity changed",
                )
                return True
            source_snapshot = await gateway.get_snapshot(plan.source_symbol)
            source_price = _decimal(
                (source_snapshot.get("payload") or {}).get("lastPrice")
            )
            if (
                source_price is None
                or source_price <= 0
                or qty > limits.max_qty_per_order
                or Decimal(qty) * source_price > limits.max_order_value_tl
            ):
                await _set_plan_state(
                    plan.id,
                    "ABORTED",
                    expected_state="PLANNED",
                    failure_reason=(
                        "rotation source cannot be liquidated in one hard-cap-safe order"
                    ),
                )
                return True
            if (
                _projected_post_sale_size(
                    target,
                    source_value_tl=Decimal(qty) * source_price,
                )
                <= 0
            ):
                await _set_plan_state(
                    plan.id,
                    "ABORTED",
                    expected_state="PLANNED",
                    failure_reason="projected post-sale target size is zero",
                )
                return True
            locked = await session.get(RotationPlan, plan.id, with_for_update=True)
            if locked is None or locked.state != "PLANNED":
                return True
            locked.sell_request_id = f"rotation-{plan.id}-sell"
            locked.state = "SELL_PENDING"
            await session.commit()
            plan.sell_request_id = locked.sell_request_id
            plan.state = locked.state
        result = await _rotation_sell_result(plan, gateway)
        if result is not None:
            await dispatch(result)
        return True

    if plan.state == "SELL_PENDING":
        async with async_session_factory() as session:
            order = (
                await session.execute(
                    select(OrderLog).where(
                        OrderLog.request_id == plan.sell_request_id,
                        OrderLog.account_ref == account_ref,
                        OrderLog.request_fingerprint.is_not(None),
                    )
                )
            ).scalar_one_or_none()
        if order is None:
            if policy_disabled or plan_expired:
                await _set_plan_state(
                    plan.id,
                    "ABORTED" if policy_disabled else "EXPIRED",
                    expected_state="SELL_PENDING",
                    failure_reason=(
                        "rotation disabled before SELL dispatch"
                        if policy_disabled
                        else "plan expired before SELL dispatch"
                    ),
                )
                return True
            result = await _rotation_sell_result(plan, gateway)
            if result is not None:
                await dispatch(result)
            return True
        status = str(order.status or "").upper()
        if status == "FILLED" and Decimal(str(order.filled_qty or 0)) >= Decimal(
            plan.source_qty
        ):
            fill_positions = await gateway.get_positions()
            fill_generation = fill_positions.get("snapshotGeneration")
            if (
                str(fill_positions.get("accountRef") or "").strip()
                != account_ref
                or fill_generation is None
            ):
                return True
            await _set_plan_state(
                plan.id,
                "SELL_FILLED_WAIT_REFRESH",
                expected_state="SELL_PENDING",
                source_fill_position_generation=int(fill_generation),
            )
        elif status in FINAL_STATES:
            await _set_plan_state(
                plan.id,
                "MANUAL_REVIEW" if Decimal(str(order.filled_qty or 0)) > 0 else "ABORTED",
                expected_state="SELL_PENDING",
                failure_reason=f"SELL ended as {status}",
            )
        return True

    if plan.state == "SELL_FILLED_WAIT_REFRESH":
        positions = await gateway.get_positions()
        async with async_session_factory() as session:
            limits = await resolve_effective_risk_config(session)
            ownership = await load_bot_ownership(session, account_ref)
        position_age = _payload_age_seconds(positions)
        if (
            not positions.get("ok", True)
            or positions.get("positionsLoaded") is not True
            or positions.get("snapshotCompleteFlag") is not True
            or str(positions.get("confidence") or "").upper()
            not in {"HIGH", "MEDIUM"}
            or position_age is None
            or position_age > limits.max_account_data_age_seconds
            or str(positions.get("accountRef") or "").strip() != account_ref
            or ownership.quantities.get(plan.source_symbol, Decimal("0")) > 0
        ):
            return True
        generation = positions.get("snapshotGeneration")
        if generation is None or (
            plan.source_fill_position_generation is None
            or int(generation) <= plan.source_fill_position_generation
        ):
            return True
        source = _position_row(positions, plan.source_symbol)
        source_bot_qty = (
            Decimal("0")
            if source is None
            else _position_value(source, "botQty", "botPositionQty")
        )
        source_account_qty = (
            Decimal("0")
            if source is None
            else _position_value(source, "accountNetQty", "totalQty", "qty")
        )
        source_available_qty = (
            Decimal("0")
            if source is None
            else _position_value(source, "accountAvailableQty", "sellableQty")
        )
        if (
            source_bot_qty is None
            or source_bot_qty != 0
            or source_account_qty is None
            or source_account_qty != 0
            or source_available_qty is None
            or source_available_qty != 0
        ):
            return True
        account = await gateway.get_account()
        account_age = _payload_age_seconds(account)
        observed_raw = account.get("receivedAtUtc") or account.get("receivedAt")
        try:
            observed_at = (
                _aware(datetime.fromisoformat(str(observed_raw).replace("Z", "+00:00")))
                if observed_raw
                else None
            )
        except (TypeError, ValueError):
            observed_at = None
        buying_power = _account_buying_power(account)
        if (
            not account.get("ok", True)
            or account.get("accountDataReliable", True) is not True
            or str(account.get("accountRef") or "").strip() != account_ref
            or account_age is None
            or account_age > limits.max_account_data_age_seconds
            or observed_at is None
            or observed_at <= _aware(plan.updated_at)
            or buying_power is None
            or buying_power <= 0
        ):
            return True
        await _set_plan_state(
            plan.id,
            "CASH_CONFIRMED",
            expected_state="SELL_FILLED_WAIT_REFRESH",
        )
        return True

    if plan.state == "CASH_CONFIRMED":
        result = await evaluate(plan.target_symbol)
        if result is None:
            return True
        if (
            not _target_has_required_advantage(result, plan, policy)
        ):
            await _set_plan_state(
                plan.id,
                "MANUAL_REVIEW",
                expected_state="CASH_CONFIRMED",
                failure_reason="target opportunity no longer has required advantage",
            )
            return True
        if not result.response.allow_order or result.response.qty <= 0:
            return True
        # Persist the exact BUY identity before crossing the network boundary.
        # A crash can then require review, but can never create a fresh request
        # id and silently submit a duplicate BUY on the next tick.
        async with async_session_factory() as session:
            locked = await session.get(RotationPlan, plan.id, with_for_update=True)
            if locked is None or locked.state != "CASH_CONFIRMED":
                return True
            locked.state = "BUY_PENDING"
            locked.buy_request_id = result.response.request_id
            locked.target_qty = int(result.response.qty)
            locked.failure_reason = None
            await session.commit()
        await dispatch(result)
        return True

    if plan.state == "BUY_PENDING":
        async with async_session_factory() as session:
            order = (
                await session.execute(
                    select(OrderLog).where(
                        OrderLog.request_id == plan.buy_request_id,
                        OrderLog.account_ref == account_ref,
                        OrderLog.request_fingerprint.is_not(None),
                    )
                )
            ).scalar_one_or_none()
        if order is None:
            await _set_plan_state(
                plan.id,
                "MANUAL_REVIEW",
                expected_state="BUY_PENDING",
                failure_reason="BUY ledger row is missing",
            )
            return True
        status = str(order.status or "").upper()
        expected = Decimal(str(order.order_qty or order.qty or plan.target_qty or 0))
        if status == "FILLED" and expected > 0 and Decimal(
            str(order.filled_qty or 0)
        ) >= expected:
            await _set_plan_state(
                plan.id, "COMPLETED", expected_state="BUY_PENDING"
            )
        elif status in FINAL_STATES:
            await _set_plan_state(
                plan.id,
                "MANUAL_REVIEW",
                expected_state="BUY_PENDING",
                failure_reason=f"BUY ended as {status}",
            )
        return True

    return True
