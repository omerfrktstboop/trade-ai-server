"""Real fill delta recording and transaction cost / slippage calculation
(Task 1). This is the only place OrderFill rows are created, and it must be
called from inside the same row-locked transaction that updates the parent
OrderLog row, so a duplicate gateway retry naturally computes delta_qty <= 0
before ever reaching here.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import OrderFill, OrderLog
from app.services.admin_config import FeeConfig, get_fee_config

logger = logging.getLogger(__name__)


def to_decimal(value: float | Decimal | str | None) -> Decimal | None:
    """Reject non-finite/unparseable values instead of guessing a number."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    if not result.is_finite():
        return None
    return result


def compute_fill_costs(
    fee_config: FeeConfig, gross_value_tl: Decimal
) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    """Return (commission_tl, exchange_fee_tl, other_fee_tl, total_cost_tl).

    Minimum commission only applies when the rate itself is > 0 (Task 1.2) -
    an unconfigured (all-zero) system must keep computing exactly zero cost,
    matching behavior before this feature existed.
    """
    if fee_config.commission_bps > 0:
        commission_tl = max(
            fee_config.minimum_commission_tl,
            gross_value_tl * fee_config.commission_bps / Decimal(10000),
        )
    else:
        commission_tl = Decimal("0")
    exchange_fee_tl = gross_value_tl * fee_config.exchange_fee_bps / Decimal(10000)
    other_fee_tl = gross_value_tl * fee_config.other_fee_bps / Decimal(10000)
    total_cost_tl = commission_tl + exchange_fee_tl + other_fee_tl
    return commission_tl, exchange_fee_tl, other_fee_tl, total_cost_tl


def compute_slippage(
    action: str, fill_price: Decimal, limit_price: Decimal | None
) -> tuple[Decimal | None, Decimal | None]:
    """Return (slippage_tl, slippage_pct); (None, None) if limit_price unknown
    (Task 1.4) - never a fabricated zero.

    BUY slippage = fill - limit (positive = paid more than intended).
    SELL slippage = limit - fill (positive = received less than intended).
    """
    if limit_price is None or limit_price <= 0:
        return None, None
    if action.upper() == "BUY":
        slippage_tl = fill_price - limit_price
    else:
        slippage_tl = limit_price - fill_price
    slippage_pct = (slippage_tl / limit_price) * Decimal(100)
    return slippage_tl, slippage_pct


def _fill_event_key(
    *,
    request_id: str,
    cumulative_filled_qty: Decimal,
    cumulative_avg_price: Decimal,
) -> str:
    """Fingerprint of the callback state that produced this fill delta. A
    duplicate gateway retry reports the same cumulative filled_qty/avg_price
    for the same request_id and therefore hashes to the same key."""
    raw = f"{request_id}|{cumulative_filled_qty}|{cumulative_avg_price}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def record_fill_delta(
    session: AsyncSession,
    row: OrderLog,
    *,
    old_filled_qty: float | Decimal | None,
    old_avg_price: float | Decimal | None,
    new_filled_qty: float | Decimal | None,
    new_avg_price: float | Decimal | None,
    limit_price: float | Decimal | None,
    order_id: str | None,
    filled_at: datetime,
) -> OrderFill | None:
    """Create at most one OrderFill for the new quantity in this callback.

    Must be called from inside the same row-locked transaction that updates
    ``row``, after ``row.id`` is assigned. Returns None (no fill recorded)
    for zero/negative delta, non-finite values, or an order side other than
    BUY/SELL - rejected/canceled/never-filled orders naturally produce a
    delta_qty <= 0 and therefore never reach here with a fill.
    """
    action = str(row.action or "").upper()
    if action not in ("BUY", "SELL"):
        return None

    old_qty_d = to_decimal(old_filled_qty) or Decimal("0")
    new_qty_d = to_decimal(new_filled_qty)
    if new_qty_d is None:
        return None
    delta_qty = new_qty_d - old_qty_d
    if delta_qty <= 0:
        return None

    old_price_d = to_decimal(old_avg_price)
    new_price_d = to_decimal(new_avg_price)
    if new_price_d is None or new_price_d <= 0:
        logger.warning(
            "FILL_DELTA_SKIPPED_NO_PRICE request_id=%s deltaQty=%s",
            row.request_id,
            delta_qty,
        )
        return None

    if old_qty_d <= 0 or old_price_d is None or old_price_d <= 0:
        delta_fill_price = new_price_d
    else:
        delta_fill_price = (
            (new_price_d * new_qty_d) - (old_price_d * old_qty_d)
        ) / delta_qty
    if delta_fill_price <= 0:
        logger.warning(
            "FILL_DELTA_SKIPPED_NONPOSITIVE_PRICE request_id=%s derivedPrice=%s",
            row.request_id,
            delta_fill_price,
        )
        return None

    limit_price_d = to_decimal(limit_price)
    gross_value_tl = delta_qty * delta_fill_price
    fee_config = await get_fee_config(session)
    commission_tl, exchange_fee_tl, other_fee_tl, total_cost_tl = compute_fill_costs(
        fee_config, gross_value_tl
    )
    slippage_tl, slippage_pct = compute_slippage(action, delta_fill_price, limit_price_d)

    fill_event_key = _fill_event_key(
        request_id=row.request_id,
        cumulative_filled_qty=new_qty_d,
        cumulative_avg_price=new_price_d,
    )
    values = dict(
        order_log_id=row.id,
        request_id=row.request_id,
        order_id=order_id or row.order_id,
        # Fill'in hesabı, emrin GÖNDERİLDİĞİ anda OrderLog'a yazılan sabit
        # accountRef'tir (callback anındaki canlı hesap değil) — Fix #1.
        account_ref=getattr(row, "account_ref", None),
        symbol=row.symbol.upper(),
        action=action,
        fill_qty=delta_qty,
        fill_price=delta_fill_price,
        limit_price=limit_price_d,
        gross_value_tl=gross_value_tl,
        commission_tl=commission_tl,
        exchange_fee_tl=exchange_fee_tl,
        other_fee_tl=other_fee_tl,
        total_cost_tl=total_cost_tl,
        slippage_tl=slippage_tl,
        slippage_pct=slippage_pct,
        fill_event_key=fill_event_key,
        fill_source="CALLBACK_DELTA",
        filled_at=filled_at,
    )
    return await _insert_fill_if_new(session, values, fill_event_key)


async def _insert_fill_if_new(
    session: AsyncSession, values: dict, fill_event_key: str
) -> OrderFill | None:
    dialect = session.bind.dialect.name
    statement = (
        (pg_insert(OrderFill) if dialect == "postgresql" else sqlite_insert(OrderFill))
        .values(**values)
        .on_conflict_do_nothing(index_elements=["fill_event_key"])
    )
    result = await session.execute(statement)
    await session.flush()
    if result.rowcount == 0:
        logger.info(
            "FILL_INSERT_DUPLICATE_SKIPPED request_id=%s fillEventKey=%s",
            values.get("request_id"),
            fill_event_key,
        )
        return None
    return (
        await session.execute(
            select(OrderFill).where(OrderFill.fill_event_key == fill_event_key)
        )
    ).scalar_one()


# Reconciled prices must never be wildly implausible relative to the order's
# own cumulative average - this bounds the "sonuç mantıklı değilse kayıt
# oluşturma" requirement (Task 1.1) without hand-picking an arbitrary tick
# tolerance the way exchange-specific price bands would.
_RECONCILED_PRICE_MAX_RATIO = Decimal(10)
_RECONCILED_PRICE_MIN_RATIO = Decimal("0.1")


async def find_missing_fill_gap(
    session: AsyncSession, row: OrderLog
) -> tuple[Decimal, Decimal] | None:
    """Return (missing_qty, missing_price) for ``row``, or None if there is
    nothing to reconcile / the gap cannot be safely computed (Task 1.1).

    ``expected_cumulative_fill`` and ``expected_total_cost`` come from
    OrderLog's own authoritative cumulative fields; ``recorded_*`` sums every
    OrderFill already on record for this order regardless of fill_source, so
    a partial reconciliation followed by a further real fill is handled
    correctly on the next pass.
    """
    expected_qty = to_decimal(row.filled_qty)
    expected_price = to_decimal(row.avg_price)
    if expected_qty is None or expected_qty <= 0:
        return None
    if expected_price is None or expected_price <= 0:
        return None

    recorded = (
        await session.execute(
            select(
                func.coalesce(func.sum(OrderFill.fill_qty), 0),
                func.coalesce(func.sum(OrderFill.fill_qty * OrderFill.fill_price), 0),
            ).where(OrderFill.order_log_id == row.id)
        )
    ).one()
    recorded_qty = to_decimal(recorded[0]) or Decimal("0")
    recorded_cost = to_decimal(recorded[1]) or Decimal("0")

    missing_qty = expected_qty - recorded_qty
    if missing_qty <= 0:
        return None

    expected_cost = expected_price * expected_qty
    missing_price = (expected_cost - recorded_cost) / missing_qty
    if missing_price <= 0 or not missing_price.is_finite():
        logger.warning(
            "FILL_RECONCILIATION_SKIPPED_NONPOSITIVE_PRICE request_id=%s "
            "missingQty=%s missingPrice=%s",
            row.request_id,
            missing_qty,
            missing_price,
        )
        return None
    if (
        missing_price > expected_price * _RECONCILED_PRICE_MAX_RATIO
        or missing_price < expected_price * _RECONCILED_PRICE_MIN_RATIO
    ):
        logger.warning(
            "FILL_RECONCILIATION_SKIPPED_IMPLAUSIBLE_PRICE request_id=%s "
            "missingPrice=%s cumulativeAvgPrice=%s",
            row.request_id,
            missing_price,
            expected_price,
        )
        return None
    return missing_qty, missing_price


async def record_reconciliation_fill(
    session: AsyncSession, row: OrderLog
) -> OrderFill | None:
    """Create the OrderFill for ``row``'s missing_fill_qty, if any and if
    it can be computed safely (Task 1.1). Idempotent: the fingerprint keys
    off the order's current cumulative filled_qty, so re-running after the
    gap has already been closed - by this function or a later real fill -
    computes no gap and returns None.
    """
    action = str(row.action or "").upper()
    if action not in ("BUY", "SELL"):
        return None

    gap = await find_missing_fill_gap(session, row)
    if gap is None:
        return None
    missing_qty, missing_price = gap

    limit_price_d = to_decimal(row.limit_price)
    gross_value_tl = missing_qty * missing_price
    fee_config = await get_fee_config(session)
    commission_tl, exchange_fee_tl, other_fee_tl, total_cost_tl = compute_fill_costs(
        fee_config, gross_value_tl
    )
    slippage_tl, slippage_pct = compute_slippage(action, missing_price, limit_price_d)

    expected_qty = to_decimal(row.filled_qty) or Decimal("0")
    fill_event_key = _fill_event_key(
        request_id=f"{row.request_id}|RECONCILIATION",
        cumulative_filled_qty=expected_qty,
        cumulative_avg_price=to_decimal(row.avg_price) or Decimal("0"),
    )
    values = dict(
        order_log_id=row.id,
        request_id=row.request_id,
        order_id=row.order_id,
        account_ref=getattr(row, "account_ref", None),
        symbol=row.symbol.upper(),
        action=action,
        fill_qty=missing_qty,
        fill_price=missing_price,
        limit_price=limit_price_d,
        gross_value_tl=gross_value_tl,
        commission_tl=commission_tl,
        exchange_fee_tl=exchange_fee_tl,
        other_fee_tl=other_fee_tl,
        total_cost_tl=total_cost_tl,
        slippage_tl=slippage_tl,
        slippage_pct=slippage_pct,
        fill_event_key=fill_event_key,
        fill_source="RECONCILIATION",
        filled_at=row.finalized_at or row.updated_at or datetime.now(timezone.utc),
    )
    fill = await _insert_fill_if_new(session, values, fill_event_key)
    if fill is not None:
        logger.warning(
            "FILL_RECONCILED request_id=%s missingQty=%s missingPrice=%s",
            row.request_id,
            missing_qty,
            missing_price,
        )
    return fill
