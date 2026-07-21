"""Deterministic, AI-independent position-exit enforcement.

The scanner's normal order path only re-evaluates a held position through
the AI (on ``scan_interval_minutes``, or ``portfolio_scan_interval_minutes``
for positions that fell off the watchlist). If the AI is unavailable, slow,
or simply returns HOLD/WAIT, a losing position can ride well past its
stop-loss between evaluations. This module closes that gap: it is called
every scanner tick, reads open positions directly from the DB, and compares
a fresh gateway snapshot price against each position's recorded stop —
independent of any AI call.

``check_stop_loss_positions`` only *detects* breaches and builds the exit
decision; it does not send orders itself. The caller (scanner._tick) routes
each returned ``EvaluationResult`` through the existing ``_maybe_send_order``
order-dispatch path, so kill switch, cutoff, preflight, ownership, and hard
caps still apply - this guard cannot bypass them, it can only *originate* a
SELL for that path to accept or reject.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from sqlalchemy import or_, select

from app.db.session import async_session_factory
from app.models.db import OrderLog, PositionLifecycle, PositionStopEvent
from app.models.signal import OrderType, SignalAction, SignalResponse
from app.services.account_context import is_position_snapshot_complete
from app.services.admin_config import (
    PositionExitConfig,
    get_position_exit_config,
    get_stop_guard_maximum_quote_age_seconds,
)
from app.services.bot_ownership import load_bot_ownership
from app.services.daily_trade_count import _start_of_trading_day
from app.services.evaluation.pipeline import EvaluationResult
from app.services.fill_ledger import to_decimal
from app.services.market_observation import record_market_observation_standalone
from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
)
from app.services.order_ledger import PENDING_STATES
from app.services.position_lifecycle_engine import (
    record_position_exit_trigger,
    tighten_active_stop,
)

logger = logging.getLogger(__name__)

_MAX_POSITION_SNAPSHOT_AGE_SECONDS = Decimal("60")
_PERCENT = Decimal("100")
_MICROSECONDS_PER_SECOND = Decimal("1000000")
_SAFE_DISABLED_EXIT_CONFIG = PositionExitConfig(
    take_profit_enabled=False,
    break_even_enabled=False,
    break_even_trigger_pct=Decimal("1.0"),
    trailing_stop_enabled=False,
    trailing_activation_pct=Decimal("2.0"),
    trailing_distance_pct=Decimal("1.0"),
)


def _validated_gateway_state(
    health: object,
    positions: object,
    *,
    expected_account_ref: str | None = None,
    expected_session_ref: str | None = None,
    expected_account_type: str | None = None,
) -> tuple[str, str, str, Decimal, dict[str, dict]] | None:
    if not isinstance(health, dict) or not isinstance(positions, dict):
        return None
    account_ref = str(health.get("accountRef") or "").strip()
    session_ref = str(health.get("accountSessionRef") or "").strip()
    account_type = str(health.get("accountType") or "").strip().upper()
    position_age = to_decimal(positions.get("snapshotAgeSeconds"))
    raw_gateway_positions = positions.get("positions")
    if (
        health.get("ok") is not True
        or health.get("gatewayContractVersion") != 3
        or health.get("configStale") is not False
        or account_type not in {"DEMO", "REAL"}
        or len(account_ref) != 64
        or len(session_ref) != 64
        or (expected_account_ref is not None and account_ref != expected_account_ref)
        or (expected_session_ref is not None and session_ref != expected_session_ref)
        or (expected_account_type is not None and account_type != expected_account_type)
        or positions.get("ok") is not True
        or str(positions.get("accountRef") or "").strip() != account_ref
        or str(positions.get("accountSessionRef") or "").strip() != session_ref
        or health.get("positionsLoaded") is not True
        or positions.get("positionsLoaded") is not True
        or not is_position_snapshot_complete(positions)
        or str(positions.get("confidence") or "").upper()
        not in {"HIGH", "MEDIUM"}
        or position_age is None
        or position_age < 0
        or position_age > _MAX_POSITION_SNAPSHOT_AGE_SECONDS
        or not isinstance(raw_gateway_positions, list)
    ):
        return None

    gateway_positions: dict[str, dict] = {}
    for row in raw_gateway_positions:
        if not isinstance(row, dict):
            return None
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or symbol in gateway_positions:
            return None
        gateway_positions[symbol] = row
    return account_ref, session_ref, account_type, position_age, gateway_positions


def _exact_integral_bot_qty(row: dict | None) -> Decimal | None:
    qty = to_decimal(row.get("botOwnedQty")) if row is not None else None
    if qty is None or qty < 0 or qty != qty.to_integral_value():
        return None
    return qty


def _elapsed_seconds(start: datetime, end: datetime) -> Decimal:
    elapsed = end - start
    microseconds = (
        (elapsed.days * 86400 + elapsed.seconds) * 1000000 + elapsed.microseconds
    )
    return Decimal(max(0, microseconds)) / _MICROSECONDS_PER_SECOND


class StopLossGuard:
    """Tracks deterministic exits so symbols are not immediately re-bought.

    In-memory only, matching the existing scan-timing/order-cooldown state
    on ``SymbolScanner`` (``_last_scan_by_symbol``, ``_last_order_sent_at``):
    it resets on process restart. A restart-persisted ban was judged
    unnecessary scope for this guard - the underlying exit protection
    itself does not depend on this cooldown.
    """

    def __init__(self) -> None:
        self._triggered_on: dict[str, date] = {}

    def is_symbol_cooling_down(self, symbol: str) -> bool:
        triggered_date = self._triggered_on.get(symbol.strip().upper())
        if triggered_date is None:
            return False
        return triggered_date == _start_of_trading_day().date()

    def mark_triggered(self, symbol: str) -> None:
        self._triggered_on[symbol.strip().upper()] = _start_of_trading_day().date()


stop_loss_guard = StopLossGuard()


async def check_stop_loss_positions(
    gateway: MatriksGatewayClient,
) -> list[EvaluationResult]:
    """Return deterministic stop-loss or take-profit exits for owned positions.

    Does not send orders or check kill switch/cutoff - the caller must route
    each result through the normal order-dispatch path for those gates.
    """
    try:
        health = await gateway.health()
        positions = await gateway.get_positions()
        validated_state = _validated_gateway_state(health, positions)
        if validated_state is None:
            return []
        (
            account_ref,
            session_ref,
            account_type,
            _initial_position_age,
            gateway_positions,
        ) = validated_state

        async with async_session_factory() as session:
            try:
                exit_config = await get_position_exit_config(session)
            except Exception:
                logger.exception(
                    "POSITION_EXIT_GUARD_OPTIONAL_CONFIG_INVALID; "
                    "optional exits disabled"
                )
                exit_config = _SAFE_DISABLED_EXIT_CONFIG
            max_quote_age_seconds = await get_stop_guard_maximum_quote_age_seconds(
                session
            )
            if max_quote_age_seconds < 0:
                return []
            ownership = await load_bot_ownership(session, account_ref)
            lifecycle_eligibility = PositionLifecycle.active_stop_loss.is_not(None)
            if exit_config.take_profit_enabled:
                lifecycle_eligibility = or_(
                    lifecycle_eligibility,
                    PositionLifecycle.active_target_price.is_not(None),
                )
            lifecycles = (
                (
                    await session.execute(
                        select(PositionLifecycle).where(
                            PositionLifecycle.status == "OPEN",
                            PositionLifecycle.current_qty > 0,
                            PositionLifecycle.data_quality.in_(
                                ("VERIFIED", "RECONCILED")
                            ),
                            PositionLifecycle.is_backfilled.is_(False),
                            lifecycle_eligibility,
                        )
                    )
                )
                .scalars()
                .all()
            )
    except Exception:
        logger.exception("POSITION_EXIT_GUARD_INITIAL_READ_FAILED")
        return []

    if not lifecycles:
        return []

    triggered: list[EvaluationResult] = []
    for lifecycle in lifecycles:
        symbol = lifecycle.symbol.strip().upper()
        qty = to_decimal(lifecycle.current_qty)
        if (
            qty is None
            or qty <= 0
            or qty != qty.to_integral_value()
        ):
            continue
        sell_qty = int(qty)
        if sell_qty <= 0:
            continue
        active_stop = to_decimal(lifecycle.active_stop_loss)
        has_valid_stop = active_stop is not None and active_stop > 0
        if not has_valid_stop:
            if not exit_config.take_profit_enabled:
                continue
            initial_average = to_decimal(lifecycle.average_entry_price)
            initial_target = to_decimal(lifecycle.active_target_price)
            if (
                initial_average is None
                or initial_average <= 0
                or initial_target is None
                or initial_target <= initial_average
            ):
                continue
        gateway_position = gateway_positions.get(symbol)
        ledger_qty = ownership.quantities.get(symbol, Decimal("0"))
        gateway_bot_qty = _exact_integral_bot_qty(gateway_position)
        if (
            gateway_bot_qty is None
            or ledger_qty != qty
            or gateway_bot_qty != qty
        ):
            logger.error(
                "POSITION_EXIT_GUARD_OWNERSHIP_MISMATCH symbol=%s lifecycleQty=%s "
                "ledgerQty=%s gatewayBotQty=%s",
                symbol,
                qty,
                ledger_qty,
                gateway_bot_qty,
            )
            continue

        try:
            async with async_session_factory() as session:
                entry_order = (
                    await session.execute(
                        select(OrderLog.id)
                        .where(
                            OrderLog.request_id == lifecycle.entry_request_id,
                            OrderLog.symbol == symbol,
                            OrderLog.account_ref == account_ref,
                            OrderLog.request_fingerprint.is_not(None),
                            OrderLog.action == "BUY",
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                pending_sell = (
                    await session.execute(
                        select(OrderLog.id)
                        .where(
                            OrderLog.symbol == symbol,
                            OrderLog.action == "SELL",
                            OrderLog.account_ref == account_ref,
                            OrderLog.status.in_(PENDING_STATES),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
        except Exception:
            logger.exception(
                "POSITION_EXIT_GUARD_DB_PRECHECK_FAILED symbol=%s", symbol
            )
            continue
        if entry_order is None:
            logger.error(
                "POSITION_EXIT_GUARD_ENTRY_ACCOUNT_MISMATCH symbol=%s", symbol
            )
            continue
        if pending_sell is not None:
            logger.info(
                "POSITION_EXIT_GUARD_NO_OP symbol=%s reason=sell_already_pending",
                symbol,
            )
            continue

        try:
            snapshot = await gateway.get_snapshot(symbol)
        except (GatewayUnavailable, GatewayError) as exc:
            logger.warning(
                "POSITION_EXIT_GUARD_SNAPSHOT_UNAVAILABLE symbol=%s error=%s",
                symbol,
                exc,
            )
            continue
        except Exception:
            logger.exception("POSITION_EXIT_GUARD_SNAPSHOT_FAILED symbol=%s", symbol)
            continue

        snapshot_received_utc = datetime.now(timezone.utc)
        if not isinstance(snapshot, dict) or snapshot.get("ok") is not True:
            continue
        guard_payload = snapshot.get("payload")
        if not isinstance(guard_payload, dict):
            continue

        quote_age_seconds = to_decimal(guard_payload.get("quoteAgeSeconds"))
        last_price_d = to_decimal(guard_payload.get("lastPrice"))
        price_source = guard_payload.get("priceSource")
        if (
            guard_payload.get("quoteReliable") is not True
            or guard_payload.get("quoteFresh") is not True
            or guard_payload.get("sessionOpen") is not True
            or quote_age_seconds is None
            or quote_age_seconds < 0
            or quote_age_seconds > Decimal(max_quote_age_seconds)
            or not isinstance(price_source, str)
            or not price_source.strip()
            or last_price_d is None
            or last_price_d <= 0
        ):
            logger.info(
                "POSITION_EXIT_GUARD_NO_OP symbol=%s reason=quote_invalid_or_stale",
                symbol,
            )
            continue

        try:
            revalidated_health = await gateway.health()
            revalidated_positions = await gateway.get_positions()
        except (GatewayUnavailable, GatewayError) as exc:
            logger.warning(
                "POSITION_EXIT_GUARD_REVALIDATION_UNAVAILABLE symbol=%s error=%s",
                symbol,
                exc,
            )
            continue
        except Exception:
            logger.exception(
                "POSITION_EXIT_GUARD_REVALIDATION_FAILED symbol=%s", symbol
            )
            continue

        revalidated_at = datetime.now(timezone.utc)
        revalidated_state = _validated_gateway_state(
            revalidated_health,
            revalidated_positions,
            expected_account_ref=account_ref,
            expected_session_ref=session_ref,
            expected_account_type=account_type,
        )
        if revalidated_state is None:
            continue
        (
            _revalidated_account_ref,
            _revalidated_session_ref,
            _revalidated_account_type,
            revalidated_position_age,
            revalidated_gateway_positions,
        ) = revalidated_state
        revalidated_gateway_qty = _exact_integral_bot_qty(
            revalidated_gateway_positions.get(symbol)
        )
        if revalidated_gateway_qty is None or revalidated_gateway_qty != qty:
            continue

        quote_age_at_revalidation = quote_age_seconds + _elapsed_seconds(
            snapshot_received_utc, revalidated_at
        )
        if quote_age_at_revalidation > Decimal(max_quote_age_seconds):
            continue
        decision_created_utc = revalidated_at
        await record_market_observation_standalone(symbol, guard_payload)

        committed_trigger: str | None = None
        committed_request_id: str | None = None
        committed_reason: str | None = None
        effective_stop: Decimal | None = None
        target_price: Decimal | None = None
        average_entry_price: Decimal | None = None
        try:
            async with async_session_factory() as trigger_session:
                fresh_lifecycle = (
                    await trigger_session.execute(
                        select(PositionLifecycle)
                        .where(PositionLifecycle.id == lifecycle.id)
                        .with_for_update()
                    )
                ).scalar_one_or_none()
                lock_acquired_at = datetime.now(timezone.utc)
                elapsed_since_revalidation = _elapsed_seconds(
                    revalidated_at, lock_acquired_at
                )
                effective_position_age = (
                    revalidated_position_age + elapsed_since_revalidation
                )
                effective_quote_age = (
                    quote_age_at_revalidation + elapsed_since_revalidation
                )
                fresh_qty = (
                    to_decimal(fresh_lifecycle.current_qty)
                    if fresh_lifecycle is not None
                    else None
                )
                if (
                    fresh_lifecycle is None
                    or fresh_lifecycle.symbol.strip().upper() != symbol
                    or fresh_lifecycle.status != "OPEN"
                    or fresh_lifecycle.data_quality not in {"VERIFIED", "RECONCILED"}
                    or fresh_lifecycle.is_backfilled
                    or fresh_qty is None
                    or fresh_qty <= 0
                    or fresh_qty != fresh_qty.to_integral_value()
                    or fresh_qty != qty
                    or revalidated_gateway_qty != fresh_qty
                    or effective_position_age
                    > _MAX_POSITION_SNAPSHOT_AGE_SECONDS
                    or effective_quote_age > Decimal(max_quote_age_seconds)
                ):
                    continue

                fresh_ownership = await load_bot_ownership(
                    trigger_session, account_ref
                )
                if (
                    fresh_ownership.quantities.get(symbol, Decimal("0"))
                    != fresh_qty
                ):
                    continue

                fresh_entry_order = (
                    await trigger_session.execute(
                        select(OrderLog.id)
                        .where(
                            OrderLog.request_id
                            == fresh_lifecycle.entry_request_id,
                            OrderLog.symbol == symbol,
                            OrderLog.account_ref == account_ref,
                            OrderLog.request_fingerprint.is_not(None),
                            OrderLog.action == "BUY",
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                pending_sell = (
                    await trigger_session.execute(
                        select(OrderLog.id)
                        .where(
                            OrderLog.symbol == symbol,
                            OrderLog.action == "SELL",
                            OrderLog.account_ref == account_ref,
                            OrderLog.status.in_(PENDING_STATES),
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if fresh_entry_order is None or pending_sell is not None:
                    continue

                state_check_at = datetime.now(timezone.utc)
                elapsed_before_mutation = _elapsed_seconds(
                    revalidated_at, state_check_at
                )
                if (
                    revalidated_position_age + elapsed_before_mutation
                    > _MAX_POSITION_SNAPSHOT_AGE_SECONDS
                    or quote_age_at_revalidation + elapsed_before_mutation
                    > Decimal(max_quote_age_seconds)
                ):
                    continue

                effective_stop = to_decimal(fresh_lifecycle.active_stop_loss)
                has_valid_stop = effective_stop is not None and effective_stop > 0
                if not has_valid_stop and not exit_config.take_profit_enabled:
                    continue

                candidate_stop: Decimal | None = None
                candidate_event_type: str | None = None
                candidate_reason: str | None = None
                if has_valid_stop and (
                    exit_config.break_even_enabled
                    or exit_config.trailing_stop_enabled
                ):
                    parsed_average = to_decimal(
                        fresh_lifecycle.average_entry_price
                    )
                    if parsed_average is not None and parsed_average > 0:
                        average_entry_price = parsed_average

                if (
                    has_valid_stop
                    and average_entry_price is not None
                    and exit_config.break_even_enabled
                ):
                    break_even_threshold = average_entry_price * (
                        Decimal("1")
                        + exit_config.break_even_trigger_pct / _PERCENT
                    )
                    if last_price_d >= break_even_threshold:
                        candidate_stop = average_entry_price
                        candidate_event_type = "BREAK_EVEN_ACTIVATED"
                        candidate_reason = (
                            f"observedPrice={last_price_d} reached "
                            f"breakEvenThreshold={break_even_threshold}; "
                            f"candidateStop={candidate_stop}"
                        )

                if (
                    has_valid_stop
                    and average_entry_price is not None
                    and exit_config.trailing_stop_enabled
                ):
                    trailing_threshold = average_entry_price * (
                        Decimal("1")
                        + exit_config.trailing_activation_pct / _PERCENT
                    )
                    if last_price_d >= trailing_threshold:
                        trailing_candidate = last_price_d * (
                            Decimal("1")
                            - exit_config.trailing_distance_pct / _PERCENT
                        )
                        if (
                            trailing_candidate < last_price_d
                            and (
                                candidate_stop is None
                                or trailing_candidate > candidate_stop
                            )
                        ):
                            candidate_stop = trailing_candidate
                            candidate_event_type = "TRAILING_STOP_TIGHTENED"
                            candidate_reason = (
                                f"observedPrice={last_price_d} reached "
                                f"trailingThreshold={trailing_threshold}; "
                                f"candidateStop={trailing_candidate}"
                            )

                if (
                    candidate_stop is not None
                    and candidate_event_type is not None
                    and candidate_reason is not None
                ):
                    await tighten_active_stop(
                        trigger_session,
                        fresh_lifecycle,
                        candidate_stop,
                        event_type=candidate_event_type,
                        reason=candidate_reason,
                    )

                effective_stop = to_decimal(fresh_lifecycle.active_stop_loss)
                if (
                    effective_stop is not None
                    and effective_stop > 0
                    and last_price_d <= effective_stop
                ):
                    committed_trigger = "STOP_BREACHED"
                    committed_reason = (
                        f"observedPrice={last_price_d} <= "
                        f"effectiveStop={effective_stop}"
                    )
                elif exit_config.take_profit_enabled:
                    if average_entry_price is None:
                        parsed_average = to_decimal(
                            fresh_lifecycle.average_entry_price
                        )
                        if parsed_average is not None and parsed_average > 0:
                            average_entry_price = parsed_average
                    parsed_target = to_decimal(
                        fresh_lifecycle.active_target_price
                    )
                    if (
                        average_entry_price is not None
                        and parsed_target is not None
                        and parsed_target > average_entry_price
                        and last_price_d >= parsed_target
                    ):
                        target_price = parsed_target
                        committed_trigger = "TAKE_PROFIT_TRIGGERED"
                        committed_reason = (
                            f"observedPrice={last_price_d} >= "
                            f"targetPrice={target_price}"
                        )

                if committed_trigger is None:
                    await trigger_session.commit()
                    continue

                sell_qty = int(fresh_qty)
                trigger_code = (
                    "SL" if committed_trigger == "STOP_BREACHED" else "TP"
                )
                bucket = int(decision_created_utc.timestamp()) // 30
                committed_request_id = (
                    f"PEXIT-{fresh_lifecycle.id}-{trigger_code}-"
                    f"Q{sell_qty}-B{bucket}"
                )
                if len(committed_request_id) > 64:
                    raise ValueError("position exit request id exceeds 64 characters")

                exact_trigger = (
                    await trigger_session.execute(
                        select(PositionStopEvent.id)
                        .where(
                            PositionStopEvent.position_lifecycle_id
                            == fresh_lifecycle.id,
                            PositionStopEvent.symbol == symbol,
                            PositionStopEvent.event_type == committed_trigger,
                            PositionStopEvent.source_request_id
                            == committed_request_id,
                        )
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if exact_trigger is None:
                    await record_position_exit_trigger(
                        trigger_session,
                        fresh_lifecycle,
                        event_type=committed_trigger,
                        source_request_id=committed_request_id,
                        reason=committed_reason,
                    )
                await trigger_session.commit()
        except Exception:
            logger.exception(
                "POSITION_EXIT_GUARD_AUDIT_TRANSACTION_FAILED symbol=%s", symbol
            )
            continue

        if (
            committed_trigger is None
            or committed_request_id is None
            or committed_reason is None
        ):
            continue
        logger.warning(
            "POSITION_EXIT_GUARD_TRIGGERED symbol=%s eventType=%s lastPrice=%s "
            "effectiveStop=%s targetPrice=%s qty=%s",
            symbol,
            committed_trigger,
            last_price_d,
            effective_stop,
            target_price,
            sell_qty,
        )
        response = SignalResponse(
            requestId=committed_request_id,
            symbol=symbol,
            action=SignalAction.SELL,
            qty=sell_qty,
            orderType=OrderType.LIMIT,
            price=last_price_d,
            confidenceScore=100.0,
            riskScore=100.0,
            allowOrder=True,
            reason=(
                f"Position exit guard {committed_trigger}: {committed_reason}; "
                "deterministic exit independent of AI"
            ),
            entryRange=None,
            stopLoss=effective_stop,
            targetPrice=target_price,
        )
        triggered.append(
            EvaluationResult(
                response=response,
                dispatch_eligible=True,
                decision_created_utc=decision_created_utc,
                evaluation_purpose="POSITION_EXIT_GUARD",
                decision_entry_price=average_entry_price,
                decision_target_price=target_price,
            )
        )

    return triggered
