"""Transactional BUY sizing and durable open-order cash reservations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import OrderCashReservation, OrderLog
from app.services.account_context import MatriksAccountContextAdapter
from app.services.effective_risk_config import EffectiveRiskConfig
from app.services.order_ledger import FINAL_STATES, PENDING_STATES, reserve_order
from app.services.position_sizing import (
    AccountSizingContext,
    PositionSizingResult,
    PositionSizingService,
    TradeSizingContext,
)


ACTIVE_RESERVATION_STATES = {
    "RESERVED",
    "SEND_STARTED",
    "SEND_IN_PROGRESS",
    "SENT_PENDING",
    "NEW",
    "PARTIALLY_FILLED",
    "SEND_UNKNOWN",
    "CANCEL_REQUESTED",
}
RELEASED_RESERVATION_STATES = {
    "FILLED",
    "REJECTED",
    "CANCELED",
    "CANCELLED",
    "EXPIRED",
}
_ACCOUNT_RESERVATION_LOCK_KEY = 824731906214


def _money(value: Any) -> Decimal:
    result = Decimal(str(value))
    if not result.is_finite():
        raise ValueError("reservation value must be finite")
    return result


def _remaining_order(row: OrderLog) -> tuple[int, Decimal]:
    order_qty = _money(row.order_qty or row.qty or 0)
    filled_qty = _money(row.filled_qty or 0)
    remaining = max(Decimal("0"), order_qty - filled_qty)
    if remaining != remaining.to_integral_value():
        raise ValueError("order remaining quantity must be an integer")
    price_raw = row.rounded_limit_price or row.limit_price or row.price
    if price_raw is None:
        raise ValueError("BUY reservation requires a limit price")
    price = _money(price_raw)
    if price <= 0:
        raise ValueError("BUY reservation price must be positive")
    return int(remaining), price


async def acquire_account_reservation_lock(session: AsyncSession) -> None:
    """Acquire a DB-scoped lock; no process-local mutex is used."""
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": _ACCOUNT_RESERVATION_LOCK_KEY},
        )
    elif dialect == "sqlite":
        # A write to one singleton row serializes reservation transactions even
        # when config SELECTs already opened a deferred SQLite transaction.
        await session.execute(
            text(
                "INSERT OR IGNORE INTO account_reservation_scopes "
                "(scope_key, lock_version) VALUES ('GLOBAL', 0)"
            )
        )
        await session.execute(
            text(
                "UPDATE account_reservation_scopes "
                "SET lock_version = lock_version + 1 WHERE scope_key = 'GLOBAL'"
            )
        )
    else:
        raise RuntimeError(f"Unsupported reservation dialect: {dialect}")


async def sync_cash_reservation(
    session: AsyncSession, row: OrderLog, *, strict: bool = True
) -> OrderCashReservation | None:
    """Mirror an authoritative BUY ledger row into its one reservation row."""
    if str(row.action).upper() != "BUY":
        return None
    status = str(row.status or row.state or "").upper()
    existing = (
        await session.execute(
            select(OrderCashReservation)
            .where(OrderCashReservation.request_id == row.request_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if status in RELEASED_RESERVATION_STATES or status in FINAL_STATES:
        if existing is not None:
            existing.status = status
            existing.remaining_qty = 0
            existing.reserved_amount_tl = Decimal("0")
            existing.released_at = datetime.now(timezone.utc)
            await session.flush()
        return existing
    if status not in ACTIVE_RESERVATION_STATES and status not in PENDING_STATES:
        return existing
    try:
        remaining_qty, price = _remaining_order(row)
    except ValueError:
        # A callback may omit the original limit price.  Persisting that
        # lifecycle event must still succeed, but an existing reservation must
        # never be released or guessed.  The strict reconciliation/sizing path
        # raises later and therefore blocks every new BUY until the ledger is
        # enriched from the gateway/exchange snapshot.
        if strict:
            raise
        return existing
    amount = Decimal(remaining_qty) * price
    if existing is None:
        existing = OrderCashReservation(
            request_id=row.request_id,
            symbol=row.symbol.upper(),
            side="BUY",
            reserved_qty=int(_money(row.order_qty or row.qty)),
            remaining_qty=remaining_qty,
            limit_price=price,
            reserved_amount_tl=amount,
            status=status,
        )
        session.add(existing)
    else:
        existing.symbol = row.symbol.upper()
        existing.reserved_qty = max(
            existing.reserved_qty, int(_money(row.order_qty or row.qty))
        )
        existing.remaining_qty = remaining_qty
        existing.limit_price = price
        existing.reserved_amount_tl = amount
        existing.status = status
        existing.released_at = None
    await session.flush()
    return existing


async def calculate_backend_reserved_cash(session: AsyncSession) -> Decimal:
    """Return exact open BUY cash, backfilling legacy ledger rows once."""
    open_rows = (
        (
            await session.execute(
                select(OrderLog).where(
                    OrderLog.action == "BUY", OrderLog.status.in_(PENDING_STATES)
                )
            )
        )
        .scalars()
        .all()
    )
    for row in open_rows:
        await sync_cash_reservation(session, row)
    total = (
        await session.execute(
            select(func.sum(OrderCashReservation.reserved_amount_tl)).where(
                OrderCashReservation.status.in_(ACTIVE_RESERVATION_STATES)
            )
        )
    ).scalar_one()
    return Decimal("0") if total is None else _money(total)


async def _transient_reserved_cash(
    session: AsyncSession, *, account_age_seconds: Decimal | None
) -> Decimal:
    if account_age_seconds is None:
        return Decimal("0")
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=float(account_age_seconds))
    total = (
        await session.execute(
            select(func.sum(OrderCashReservation.reserved_amount_tl)).where(
                OrderCashReservation.status.in_(ACTIVE_RESERVATION_STATES),
                OrderCashReservation.created_at > cutoff,
            )
        )
    ).scalar_one()
    return Decimal("0") if total is None else _money(total)


@dataclass(frozen=True)
class BuyReservationResult:
    allowed: bool
    qty: int
    reason: str
    ledger: OrderLog | None
    sizing: PositionSizingResult
    account_context: AccountSizingContext


async def reserve_sized_buy(
    session: AsyncSession,
    *,
    request_id: str,
    symbol: str,
    original_decision_qty: int,
    limit_price: Decimal,
    mode: str,
    raw_account: dict[str, Any],
    raw_positions: list[dict[str, Any]],
    raw_open_orders: list[dict[str, Any]],
    market_prices: dict[str, Decimal],
    trade: TradeSizingContext,
    limits: EffectiveRiskConfig,
    adapter: MatriksAccountContextAdapter,
) -> BuyReservationResult:
    """Lock, recalculate, reserve ledger+cash, and commit before gateway send."""
    if isinstance(original_decision_qty, bool) or original_decision_qty <= 0:
        raise ValueError("original_decision_qty must be a positive integer")
    await acquire_account_reservation_lock(session)
    try:
        backend_reserved = await calculate_backend_reserved_cash(session)
        account = adapter.normalize(
            raw_account=raw_account,
            raw_positions=raw_positions,
            raw_open_orders=raw_open_orders,
            backend_reserved_cash_tl=backend_reserved,
            symbol=symbol,
            market_prices=market_prices,
        )
        await adapter.add_audit(session, request_id=request_id, symbol=symbol)

        # A fresh broker snapshot can include older broker-side reservations.
        # Only reservations created after that snapshot are deducted here, so
        # concurrent workers cannot reuse cash without double-deducting orders
        # already reflected by the broker.
        if adapter.reservation_handling == "BROKER_ALREADY_DEDUCTED":
            transient = await _transient_reserved_cash(
                session, account_age_seconds=account.account_data_age_seconds
            )
            if account.effective_available_cash_tl is not None and transient:
                account = account.model_copy(
                    update={
                        "effective_available_cash_tl": max(
                            Decimal("0"),
                            account.effective_available_cash_tl - transient,
                        )
                    }
                )

        sizing = PositionSizingService().calculate_buy_size(
            account=account, trade=trade, limits=limits
        )
        final_qty = min(original_decision_qty, sizing.qty) if sizing.allowed else 0
        if final_qty <= 0:
            await session.commit()  # retain fail-closed normalization audit
            return BuyReservationResult(
                allowed=False,
                qty=0,
                reason=sizing.reason,
                ledger=None,
                sizing=sizing,
                account_context=account,
            )

        ledger, may_send, rejection = await reserve_order(
            session,
            request_id=request_id,
            symbol=symbol,
            side="BUY",
            qty=final_qty,
            limit_price=limit_price,
            mode=mode,
            config_version=limits.system_config_version,
            profile_code=limits.trade_profile_code,
            commit=False,
        )
        if not may_send:
            await session.commit()
            return BuyReservationResult(
                allowed=False,
                qty=0,
                reason=rejection or f"request already reserved: {ledger.status}",
                ledger=ledger,
                sizing=sizing,
                account_context=account,
            )
        ledger.status = "SEND_IN_PROGRESS"
        ledger.state = "SEND_IN_PROGRESS"
        ledger.send_started_at = datetime.now(timezone.utc)
        await sync_cash_reservation(session, ledger)
        await session.commit()
        return BuyReservationResult(
            allowed=True,
            qty=final_qty,
            reason="BUY cash reserved atomically",
            ledger=ledger,
            sizing=sizing,
            account_context=account,
        )
    except Exception:
        await session.rollback()
        raise
