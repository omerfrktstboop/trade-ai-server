"""Günlük parasal K/Z ve zarar limiti (v2 Faz 5, ilke #7).

Muhasebe kuralları:

- **Katı hesap yolu:** yalnız aynı ``account_ref`` ile damgalı ``order_fills``
  kronolojik replay edilir. Realized, satış anındaki kanıtlanmış ağırlıklı
  maliyetten; unrealized ise bugün alınmış ve replay sonunda kalmış lotlardan
  hesaplanır. Bilinen miktarı aşan SELL kısmı hesaplanmaz ve data-gap olur.
- **Ledger bütünlüğü:** hesap kapsamındaki ``order_logs.filled_qty`` ile aynı
  hesap fill toplamı uyuşmalıdır; kayıp, fazla veya yanlış hesaplı fill data-gap
  üretir. Bugünkü tüm kanıtlanmış fill ücretleri realized'dan düşülür.
- **Legacy hesapsız yol:** ``account_ref`` verilmezse eski lifecycle fallback
  davranışı uyumluluk için korunur.

Fail yönü: bilinen realized zarar tek başına limiti aşıyorsa veri boşluğuna
rağmen BREACHED; aksi halde eksik PnL veya çözülemeyen yüzde sermaye bazı
UNAVAILABLE olur ve yeni BUY fail-closed bloklanır. SELL ve stop-loss guard bu
limitten HİÇBİR ZAMAN etkilenmez — zararlı günde çıkış her zaman mümkündür.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.db import OrderFill, OrderLog, PositionLifecycle
from app.models.signal import OrderType, SignalAction, SignalResponse
from app.services.admin_config import get_admin_config_value

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DailyPnl:
    realized_tl: Decimal
    unrealized_today_tl: Decimal
    total_tl: Decimal
    #: Hesaplanamayan parçaların gerekçeleri (fiyat yok, maliyet yok, ...).
    data_gaps: tuple[str, ...] = ()


class DailyLossGuardState(str, Enum):
    DISABLED = "DISABLED"
    AVAILABLE = "AVAILABLE"
    BREACHED = "BREACHED"
    UNAVAILABLE = "UNAVAILABLE"


class DailyLossCapitalSource(str, Enum):
    NONE = "NONE"
    ACCOUNT_EQUITY = "ACCOUNT_EQUITY"
    MIN_ACCOUNT_EQUITY_BOT_BUDGET = "MIN_ACCOUNT_EQUITY_BOT_BUDGET"
    BOT_CAPITAL_BUDGET_FALLBACK = "BOT_CAPITAL_BUDGET_FALLBACK"


@dataclass(frozen=True)
class DailyLossGuardStatus:
    status: DailyLossGuardState
    configured_pct: Decimal | None
    configured_tl: Decimal | None
    capital_source: DailyLossCapitalSource = DailyLossCapitalSource.NONE
    capital_base_tl: Decimal | None = None
    percentage_cap_tl: Decimal | None = None
    effective_cap_tl: Decimal | None = None
    pnl: DailyPnl | None = None
    reason: str | None = None

    @property
    def enabled(self) -> bool:
        # An unreadable configuration is conservatively treated as enabled so
        # AUTO_TRADE readiness cannot turn green on a corrupt safety setting.
        if self.status == DailyLossGuardState.DISABLED:
            return False
        return True

    @property
    def blocks_buy(self) -> bool:
        return self.status in {
            DailyLossGuardState.BREACHED,
            DailyLossGuardState.UNAVAILABLE,
        }

    def authenticated_summary(self) -> dict[str, Any]:
        """Small exact summary for authenticated operational surfaces."""

        pnl = self.pnl
        return {
            "status": self.status.value,
            "enabled": self.enabled,
            "configuredPct": _decimal_text(self.configured_pct),
            "configuredTl": _decimal_text(self.configured_tl),
            "capitalSource": self.capital_source.value,
            "capitalBaseTl": _decimal_text(self.capital_base_tl),
            "percentageCapTl": _decimal_text(self.percentage_cap_tl),
            "effectiveCapTl": _decimal_text(self.effective_cap_tl),
            "pnl": (
                {
                    "realizedTl": _decimal_text(pnl.realized_tl),
                    "unrealizedTodayTl": _decimal_text(pnl.unrealized_today_tl),
                    "totalTl": _decimal_text(pnl.total_tl),
                    "complete": not pnl.data_gaps,
                    "dataGapCount": len(pnl.data_gaps),
                }
                if pnl is not None
                else None
            ),
            "reason": self.reason,
        }


_ACCOUNT_EQUITY_ALIASES = ("TotalEquity", "AccountEquity", "Equity", "Overall")
_VERIFIED_ACCOUNT_PROVIDERS = {"MATRIKS_IQ", "MATRIKSIQ", "MATRIKS"}
_FILL_QTY_EPSILON = Decimal("0.0000001")
_MONEY_EPSILON = Decimal("0.000001")
_PRICE_ABSOLUTE_EPSILON = Decimal("0.000001")
_PRICE_RELATIVE_EPSILON = Decimal("0.000000001")
_MAX_PNL_QUOTE_AGE_SECONDS = Decimal("30")
_SHA256_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def _decimal_text(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _finite_decimal(value: Any) -> Decimal:
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError("value is not a decimal") from exc
    if not parsed.is_finite():
        raise ValueError("value is not finite")
    return parsed


def _decimal_matches(
    actual: Decimal,
    expected: Decimal,
    *,
    absolute_epsilon: Decimal,
    relative_epsilon: Decimal = Decimal("0"),
) -> bool:
    tolerance = max(absolute_epsilon, abs(expected) * relative_epsilon)
    return abs(actual - expected) <= tolerance


def _valid_ref(value: Any) -> str | None:
    text = str(value or "").strip()
    return (
        text
        if len(text) == 64 and all(char in _SHA256_HEX_CHARS for char in text)
        else None
    )


def _strict_gateway_identity(
    health: Any,
) -> tuple[str | None, str | None, str | None]:
    if not isinstance(health, dict):
        return None, None, "gateway health payload is unavailable or invalid"
    if health.get("ok") is not True:
        return None, None, "gateway health is not ok"
    if health.get("gatewayContractVersion") != 3:
        return None, None, "gateway contract version is not v3"
    if health.get("configStale") is not False:
        return None, None, "gateway configuration is stale or unverified"
    if str(health.get("accountType") or "").strip().upper() not in {"DEMO", "REAL"}:
        return None, None, "gateway account type is unverified"
    try:
        verification_age = _finite_decimal(
            health.get("accountVerificationAgeSeconds")
        )
    except ValueError:
        return None, None, "gateway account verification age is unavailable"
    if not Decimal("0") <= verification_age <= Decimal("5"):
        return None, None, "gateway account verification is stale"
    account_ref = _valid_ref(health.get("accountRef"))
    session_ref = _valid_ref(health.get("accountSessionRef"))
    if account_ref is None or session_ref is None:
        return None, None, "gateway account/session references are invalid"
    return account_ref, session_ref, None


def _payload_age_seconds(payload: dict[str, Any]) -> Decimal | None:
    for key in ("accountDataAgeSeconds", "dataAgeSeconds"):
        if key not in payload:
            continue
        try:
            age = _finite_decimal(payload[key])
        except ValueError:
            return None
        return age if age >= 0 else None
    for key in ("receivedAtUtc", "receivedAt", "timestamp"):
        raw = payload.get(key)
        if not raw:
            continue
        try:
            observed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if observed.tzinfo is None:
                observed = observed.replace(tzinfo=timezone.utc)
            age = _finite_decimal(
                (datetime.now(timezone.utc) - observed).total_seconds()
            )
        except (TypeError, ValueError):
            return None
        return age if age >= 0 else None
    return None


def _account_payload_claims_usable(
    raw_account: Any, *, maximum_age_seconds: Decimal | None = None
) -> bool:
    usable = bool(
        isinstance(raw_account, dict)
        and raw_account.get("ok") is True
        and raw_account.get("available", True) is True
        and raw_account.get("accountDataReliable") is True
    )
    if not usable or maximum_age_seconds is None:
        return usable
    age = _payload_age_seconds(raw_account)
    return age is not None and age <= maximum_age_seconds


def _verified_account_equity(
    raw_account: dict[str, Any] | None,
    *,
    gateway_health: dict[str, Any] | None,
    expected_account_ref: str | None,
    expected_account_session_ref: str | None,
    maximum_age_seconds: Decimal,
) -> tuple[Decimal | None, str | None]:
    """Read equity without invoking cash-reservation normalization policy."""

    if not isinstance(raw_account, dict) or raw_account.get("ok") is not True:
        return None, "gateway account payload is unavailable"
    if raw_account.get("available", True) is not True:
        return None, "gateway account payload is unavailable"
    if raw_account.get("accountDataReliable") is not True:
        return None, "gateway marked account data unreliable"

    age = _payload_age_seconds(raw_account)
    if age is None or age > maximum_age_seconds:
        return None, "gateway account data is stale or has no reliable age"

    account_ref = _valid_ref(raw_account.get("accountRef"))
    session_ref = _valid_ref(raw_account.get("accountSessionRef"))
    if account_ref is None or session_ref is None:
        return None, "gateway account identity is incomplete"
    if expected_account_ref and account_ref != expected_account_ref:
        return None, "gateway accountRef changed"
    if expected_account_session_ref and session_ref != expected_account_session_ref:
        return None, "gateway accountSessionRef changed"

    if gateway_health is not None:
        health_ref = _valid_ref(gateway_health.get("accountRef"))
        health_session_ref = _valid_ref(gateway_health.get("accountSessionRef"))
        if (
            gateway_health.get("ok") is not True
            or health_ref is None
            or health_session_ref is None
            or health_ref != account_ref
            or health_session_ref != session_ref
        ):
            return None, "gateway health account identity is unavailable or mismatched"

    account_payload = raw_account.get("account")
    if not isinstance(account_payload, dict):
        account_payload = raw_account
    provider = str(
        raw_account.get("sourceProvider")
        or raw_account.get("provider")
        or account_payload.get("sourceProvider")
        or account_payload.get("provider")
        or ""
    ).strip().upper()
    if provider not in _VERIFIED_ACCOUNT_PROVIDERS:
        return None, "gateway account provider mapping is unverified"

    indexed = {str(key).casefold(): value for key, value in account_payload.items()}
    for alias in _ACCOUNT_EQUITY_ALIASES:
        if alias.casefold() not in indexed:
            continue
        try:
            equity = _finite_decimal(indexed[alias.casefold()])
        except ValueError:
            return None, "gateway account equity is not a finite decimal"
        if equity <= 0:
            return None, "gateway account equity is not positive"
        return equity, None
    return None, "gateway account equity field is missing or unverified"


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _day_start_utc(session: AsyncSession) -> datetime:
    """İşlem gününün başlangıcı (admin timezone'unda yerel gece yarısı)."""
    try:
        tz = ZoneInfo(await get_admin_config_value(session, "timezone"))
    except Exception:
        tz = ZoneInfo("Europe/Istanbul")
    local_now = datetime.now(tz)
    return datetime.combine(local_now.date(), time.min, tzinfo=tz).astimezone(
        timezone.utc
    )


async def _fallback_avg_cost(
    session: AsyncSession, symbol: str
) -> Decimal | None:
    """Fill geçmişi eksik satışlar için son çare maliyet bazı: en güncel
    lifecycle'ın ortalama giriş fiyatı. Yoksa None (data-gap)."""
    row = (
        await session.execute(
            select(PositionLifecycle)
            .where(PositionLifecycle.symbol == symbol)
            .order_by(PositionLifecycle.opened_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if row is None or row.average_entry_price is None:
        return None
    return row.average_entry_price


async def _scoped_fill_ledger_gaps(
    session: AsyncSession,
    account_ref: str,
    fills: list[OrderFill],
) -> tuple[tuple[str, ...], frozenset[int]]:
    """Return ledger gaps and order ids safe for account-scoped replay."""

    gaps: list[str] = []
    invalid_order_ids: set[int] = set()

    def invalidate(order_id: int | None, code: str) -> None:
        gaps.append(code)
        if order_id is not None:
            invalid_order_ids.add(order_id)

    active_orders = list(
        (
            await session.execute(
                select(OrderLog).where(
                    OrderLog.account_ref == account_ref,
                    OrderLog.filled_qty > 0,
                )
            )
        )
        .scalars()
        .all()
    )
    orders_by_id = {order.id: order for order in active_orders}
    referenced_ids = {fill.order_log_id for fill in fills}
    missing_order_ids = referenced_ids.difference(orders_by_id)
    if missing_order_ids:
        referenced_orders = list(
            (
                await session.execute(
                    select(OrderLog).where(OrderLog.id.in_(missing_order_ids))
                )
            )
            .scalars()
            .all()
        )
        orders_by_id.update({order.id: order for order in referenced_orders})

    comparison_orders = {
        order.id: order
        for order in orders_by_id.values()
        if order.account_ref == account_ref
    }
    expected_qty_by_order: dict[int, Decimal] = {}
    expected_price_by_order: dict[int, Decimal] = {}
    for order in comparison_orders.values():
        normalized_symbol = str(order.symbol or "").strip().upper()
        normalized_action = str(order.action or "").strip().upper()
        if not str(order.request_id or "").strip():
            invalidate(order.id, f"ORDER_REQUEST_ID_INVALID:{order.id}")
        if not normalized_symbol:
            invalidate(order.id, f"ORDER_SYMBOL_INVALID:{order.request_id}")
        if normalized_action not in {"BUY", "SELL"}:
            invalidate(order.id, f"ORDER_ACTION_INVALID:{order.request_id}")
        if order.account_ref != account_ref:
            invalidate(order.id, f"ORDER_ACCOUNT_MISMATCH:{order.request_id}")
        try:
            expected_qty = _finite_decimal(order.filled_qty)
        except ValueError:
            invalidate(order.id, f"ORDER_FILLED_QTY_INVALID:{order.request_id}")
        else:
            if expected_qty <= 0:
                invalidate(
                    order.id, f"ORDER_FILLED_QTY_NONPOSITIVE:{order.request_id}"
                )
            else:
                expected_qty_by_order[order.id] = expected_qty
        try:
            expected_price = _finite_decimal(order.avg_price)
        except ValueError:
            invalidate(order.id, f"ORDER_AVG_PRICE_INVALID:{order.request_id}")
        else:
            if expected_price <= 0:
                invalidate(
                    order.id, f"ORDER_AVG_PRICE_NONPOSITIVE:{order.request_id}"
                )
            else:
                expected_price_by_order[order.id] = expected_price

    scoped_qty_by_order: dict[int, Decimal] = {}
    scoped_value_by_order: dict[int, Decimal] = {}
    for fill in fills:
        order_id = fill.order_log_id
        order = orders_by_id.get(order_id)
        if order is None:
            invalidate(None, f"FILL_ORDER_MISSING:{fill.request_id}")
            continue
        if order.account_ref != account_ref:
            invalidate(order_id, f"FILL_ORDER_ACCOUNT_MISMATCH:{fill.request_id}")
            continue

        if (
            not str(fill.request_id or "").strip()
            or fill.request_id != order.request_id
        ):
            invalidate(order_id, f"FILL_REQUEST_ID_MISMATCH:{fill.request_id}")
        fill_symbol = str(fill.symbol or "").strip().upper()
        order_symbol = str(order.symbol or "").strip().upper()
        if not fill_symbol or fill_symbol != order_symbol:
            invalidate(order_id, f"FILL_SYMBOL_MISMATCH:{fill.request_id}")
        fill_action = str(fill.action or "").strip().upper()
        order_action = str(order.action or "").strip().upper()
        if (
            fill_action not in {"BUY", "SELL"}
            or order_action not in {"BUY", "SELL"}
            or fill_action != order_action
        ):
            invalidate(order_id, f"FILL_ACTION_MISMATCH:{fill.request_id}")
        if fill.account_ref != account_ref or order.account_ref != account_ref:
            invalidate(order_id, f"FILL_ACCOUNT_MISMATCH:{fill.request_id}")

        fill_qty: Decimal | None = None
        fill_price: Decimal | None = None
        try:
            fill_qty = _finite_decimal(fill.fill_qty)
        except ValueError:
            invalidate(order_id, f"FILL_QTY_INVALID:{fill.request_id}")
        else:
            if fill_qty <= 0:
                invalidate(order_id, f"FILL_QTY_NONPOSITIVE:{fill.request_id}")
                fill_qty = None
        try:
            fill_price = _finite_decimal(fill.fill_price)
        except ValueError:
            invalidate(order_id, f"FILL_PRICE_INVALID:{fill.request_id}")
        else:
            if fill_price <= 0:
                invalidate(order_id, f"FILL_PRICE_NONPOSITIVE:{fill.request_id}")
                fill_price = None

        fee_values: list[Decimal] = []
        for name, raw_value in (
            ("COMMISSION", fill.commission_tl),
            ("EXCHANGE_FEE", fill.exchange_fee_tl),
            ("OTHER_FEE", fill.other_fee_tl),
        ):
            try:
                fee = _finite_decimal(raw_value)
            except ValueError:
                invalidate(order_id, f"FILL_{name}_INVALID:{fill.request_id}")
                continue
            if fee < 0:
                invalidate(order_id, f"FILL_{name}_NEGATIVE:{fill.request_id}")
                continue
            fee_values.append(fee)

        try:
            gross_value = _finite_decimal(fill.gross_value_tl)
        except ValueError:
            invalidate(order_id, f"FILL_GROSS_VALUE_INVALID:{fill.request_id}")
        else:
            if fill_qty is not None and fill_price is not None and not _decimal_matches(
                gross_value,
                fill_qty * fill_price,
                absolute_epsilon=_MONEY_EPSILON,
            ):
                invalidate(order_id, f"FILL_GROSS_VALUE_MISMATCH:{fill.request_id}")

        try:
            total_cost = _finite_decimal(fill.total_cost_tl)
        except ValueError:
            invalidate(order_id, f"FILL_TOTAL_COST_INVALID:{fill.request_id}")
        else:
            if total_cost < 0:
                invalidate(order_id, f"FILL_TOTAL_COST_NEGATIVE:{fill.request_id}")
            elif len(fee_values) == 3 and not _decimal_matches(
                total_cost,
                sum(fee_values, Decimal("0")),
                absolute_epsilon=_MONEY_EPSILON,
            ):
                invalidate(order_id, f"FILL_TOTAL_COST_MISMATCH:{fill.request_id}")

        if fill_qty is not None and fill_price is not None:
            scoped_qty_by_order[order_id] = (
                scoped_qty_by_order.get(order_id, Decimal("0")) + fill_qty
            )
            scoped_value_by_order[order_id] = (
                scoped_value_by_order.get(order_id, Decimal("0"))
                + fill_qty * fill_price
            )

    mismatched_fills = list(
        (
            await session.execute(
                select(OrderFill)
                .join(OrderLog, OrderFill.order_log_id == OrderLog.id)
                .where(
                    OrderLog.account_ref == account_ref,
                    or_(
                        OrderFill.account_ref.is_(None),
                        OrderFill.account_ref != account_ref,
                    ),
                )
            )
        )
        .scalars()
        .all()
    )
    for fill in mismatched_fills:
        invalidate(
            fill.order_log_id,
            f"ORDER_FILL_ACCOUNT_MISMATCH:{fill.request_id}",
        )

    for order in comparison_orders.values():
        expected_qty = expected_qty_by_order.get(order.id)
        expected_price = expected_price_by_order.get(order.id)
        recorded_qty = scoped_qty_by_order.get(order.id, Decimal("0"))
        if expected_qty is not None:
            difference = recorded_qty - expected_qty
            if difference < -_FILL_QTY_EPSILON:
                invalidate(order.id, f"ORDER_FILL_QTY_MISSING:{order.request_id}")
            elif difference > _FILL_QTY_EPSILON:
                invalidate(order.id, f"ORDER_FILL_QTY_EXCESS:{order.request_id}")
        if recorded_qty > 0 and expected_price is not None:
            weighted_fill_price = scoped_value_by_order[order.id] / recorded_qty
            if not _decimal_matches(
                weighted_fill_price,
                expected_price,
                absolute_epsilon=_PRICE_ABSOLUTE_EPSILON,
                relative_epsilon=_PRICE_RELATIVE_EPSILON,
            ):
                invalidate(order.id, f"ORDER_FILL_PRICE_MISMATCH:{order.request_id}")

    trusted_order_ids = frozenset(
        order_id
        for order_id in comparison_orders
        if order_id not in invalid_order_ids
    )
    return tuple(dict.fromkeys(gaps)), trusted_order_ids


async def get_daily_pnl(
    session: AsyncSession,
    gateway=None,
    *,
    price_lookup: dict[str, Decimal] | None = None,
    account_ref: str | None = None,
) -> DailyPnl:
    """Bugünün realized + (bugünkü lotların) unrealized K/Z'si.

    ``account_ref`` verilirse yalnızca SQL'de o hesaba filtrelenmiş fill'ler
    kullanılır. Bu katı yol lifecycle tablosuna hiç bakmaz; fill toplamlarını
    aynı hesaba damgalı OrderLog kümülatif miktarlarıyla da karşılaştırır.
    ``account_ref=None`` eski, hesapsız ölçüm davranışını korur.

    Realized, lifecycle'ın GÜNCEL ortalamasından DEĞİL; sembolün tüm
    fill'leri kronolojik replay edilerek satış anındaki ağırlıklı maliyetten
    hesaplanır (Fix #3) — ek alım sonrası satış, kısmi satış ve taşınan
    pozisyonlar doğru işlenir.

    ``price_lookup`` verilirse canlı fiyatlar oradan; yoksa
    ``gateway.get_snapshot`` kullanılır; o da yoksa unrealized data-gap.
    """
    day_start = await _day_start_utc(session)
    gaps: list[str] = []

    async def _price_for(symbol: str) -> Decimal | None:
        if price_lookup is not None:
            raw = price_lookup.get(symbol)
            if raw is None:
                return None
            try:
                price = _finite_decimal(raw)
            except ValueError:
                return None
            return price if price > 0 else None
        if gateway is None:
            return None
        try:
            snapshot = await gateway.get_snapshot(symbol)
            if not isinstance(snapshot, dict) or snapshot.get("ok") is not True:
                return None
            payload = snapshot.get("payload")
            if not isinstance(payload, dict):
                return None
            raw = payload.get("lastPrice")
            if payload.get("quoteReliable") is not True or raw is None:
                return None
            quote_age = _finite_decimal(payload.get("quoteAgeSeconds"))
            if not Decimal("0") <= quote_age <= _MAX_PNL_QUOTE_AGE_SECONDS:
                return None
            price = _finite_decimal(raw)
            return price if price > 0 else None
        except Exception:
            return None

    if account_ref is not None:
        scoped_fills = list(
            (
                await session.execute(
                    select(OrderFill)
                    .where(OrderFill.account_ref == account_ref)
                    .order_by(OrderFill.filled_at.asc(), OrderFill.id.asc())
                )
            )
            .scalars()
            .all()
        )
        ledger_gaps, trusted_order_ids = await _scoped_fill_ledger_gaps(
            session, account_ref, scoped_fills
        )
        gaps.extend(ledger_gaps)
        replay_fills = [
            fill for fill in scoped_fills if fill.order_log_id in trusted_order_ids
        ]
        fills_today = [
            fill for fill in replay_fills if _aware(fill.filled_at) >= day_start
        ]

        fees = Decimal("0")
        for fill in fills_today:
            try:
                fees += sum(
                    (
                        _finite_decimal(fill.commission_tl or 0),
                        _finite_decimal(fill.exchange_fee_tl or 0),
                        _finite_decimal(fill.other_fee_tl or 0),
                    ),
                    Decimal("0"),
                )
            except ValueError:
                gaps.append(f"FILL_FEE_INVALID:{fill.request_id}")

        inventory: dict[str, dict[str, Decimal]] = {}
        realized = Decimal("0")
        for fill in replay_fills:
            symbol = str(fill.symbol or "").strip().upper()
            action = str(fill.action or "").strip().upper()
            try:
                fill_qty = _finite_decimal(fill.fill_qty)
                fill_price = _finite_decimal(fill.fill_price)
            except ValueError:
                gaps.append(f"FILL_VALUE_INVALID:{fill.request_id}")
                continue
            if not symbol or fill_qty <= 0 or fill_price <= 0:
                gaps.append(f"FILL_VALUE_INVALID:{fill.request_id}")
                continue
            if action not in {"BUY", "SELL"}:
                gaps.append(f"FILL_ACTION_INVALID:{fill.request_id}")
                continue

            state = inventory.setdefault(
                symbol,
                {
                    "running_qty": Decimal("0"),
                    "running_cost": Decimal("0"),
                    "today_qty": Decimal("0"),
                    "today_cost": Decimal("0"),
                },
            )
            is_today = _aware(fill.filled_at) >= day_start
            if action == "BUY":
                state["running_qty"] += fill_qty
                state["running_cost"] += fill_qty * fill_price
                if is_today:
                    state["today_qty"] += fill_qty
                    state["today_cost"] += fill_qty * fill_price
                continue

            running_qty = state["running_qty"]
            known_sold_qty = min(fill_qty, running_qty)
            if known_sold_qty > 0:
                average_cost = state["running_cost"] / running_qty
                if is_today:
                    realized += (fill_price - average_cost) * known_sold_qty

                remaining_fraction = Decimal("1") - (
                    known_sold_qty / running_qty
                )
                state["today_qty"] *= remaining_fraction
                state["today_cost"] *= remaining_fraction

                state["running_qty"] = running_qty - known_sold_qty
                state["running_cost"] = (
                    average_cost * state["running_qty"]
                    if state["running_qty"] > 0
                    else Decimal("0")
                )

            excess_qty = fill_qty - known_sold_qty
            if excess_qty > _FILL_QTY_EPSILON:
                gaps.append(
                    f"SELL_EXCEEDS_SCOPED_QTY:{symbol}:{fill.request_id}"
                )

        realized -= fees
        unrealized = Decimal("0")
        for symbol, state in inventory.items():
            today_qty = state["today_qty"]
            if today_qty <= _FILL_QTY_EPSILON:
                continue
            if state["today_cost"] <= 0:
                gaps.append(f"UNREALIZED_COST_UNKNOWN:{symbol}")
                continue
            price = await _price_for(symbol)
            if price is None:
                gaps.append(f"UNREALIZED_PRICE_UNAVAILABLE:{symbol}")
                continue
            today_average_cost = state["today_cost"] / today_qty
            unrealized += (price - today_average_cost) * today_qty

        return DailyPnl(
            realized_tl=realized,
            unrealized_today_tl=unrealized,
            total_tl=realized + unrealized,
            data_gaps=tuple(dict.fromkeys(gaps)),
        )

    # Legacy, unscoped compatibility path. It may use lifecycle fallback data;
    # strict account-scoped callers return above and can never reach this code.
    all_fills = list(
        (
            await session.execute(
                select(OrderFill).order_by(
                    OrderFill.filled_at.asc(), OrderFill.id.asc()
                )
            )
        )
        .scalars()
        .all()
    )
    fills_by_symbol: dict[str, list[OrderFill]] = {}
    for fill in all_fills:
        fills_by_symbol.setdefault(fill.symbol.upper(), []).append(fill)
    fills_today = [
        fill for fill in all_fills if _aware(fill.filled_at) >= day_start
    ]

    realized = Decimal("0")
    fees = Decimal("0")
    for fill in fills_today:
        fees += (
            (fill.commission_tl or 0)
            + (fill.exchange_fee_tl or 0)
            + (fill.other_fee_tl or 0)
        )
    for symbol, fills in fills_by_symbol.items():
        running_qty = Decimal("0")
        running_cost = Decimal("0")
        for fill in fills:
            if fill.action == "BUY":
                running_qty += fill.fill_qty
                running_cost += fill.fill_qty * fill.fill_price
                continue
            if running_qty > 0:
                avg_cost = running_cost / running_qty
            else:
                avg_cost = await _fallback_avg_cost(session, symbol)
                if avg_cost is None:
                    if _aware(fill.filled_at) >= day_start:
                        gaps.append(
                            f"REALIZED_COST_UNKNOWN:{symbol}:{fill.request_id}"
                        )
                    continue
            sold_qty = fill.fill_qty
            if _aware(fill.filled_at) >= day_start:
                realized += (fill.fill_price - avg_cost) * sold_qty
            running_qty -= sold_qty
            running_cost = avg_cost * running_qty if running_qty > 0 else Decimal("0")
    realized -= fees

    open_rows = list(
        (
            await session.execute(
                select(PositionLifecycle).where(PositionLifecycle.status == "OPEN")
            )
        )
        .scalars()
        .all()
    )
    unrealized = Decimal("0")
    for row in open_rows:
        symbol = row.symbol.strip().upper()
        qty = Decimal(str(row.current_qty or 0))
        if qty <= 0:
            continue
        opened_today = _aware(row.opened_at) >= day_start
        if opened_today:
            if row.average_entry_price is None:
                gaps.append(f"UNREALIZED_COST_UNKNOWN:{symbol}")
                continue
            price = await _price_for(symbol)
            if price is None:
                gaps.append(f"UNREALIZED_PRICE_UNAVAILABLE:{symbol}")
                continue
            unrealized += (price - row.average_entry_price) * qty
            continue

        # Taşınan pozisyon: yalnızca bugünkü add-on BUY lotları sayılır.
        todays_buys = [
            fill
            for fill in fills_today
            if fill.symbol == symbol and fill.action == "BUY"
        ]
        if not todays_buys:
            continue
        buy_qty = sum((fill.fill_qty for fill in todays_buys), Decimal("0"))
        if buy_qty <= 0:
            continue
        weighted_cost = sum(
            (fill.fill_qty * fill.fill_price for fill in todays_buys), Decimal("0")
        )
        w_avg = weighted_cost / buy_qty
        counted_qty = min(buy_qty, qty)
        price = await _price_for(symbol)
        if price is None:
            gaps.append(f"UNREALIZED_PRICE_UNAVAILABLE:{symbol}")
            continue
        unrealized += (price - w_avg) * counted_qty

    return DailyPnl(
        realized_tl=realized,
        unrealized_today_tl=unrealized,
        total_tl=realized + unrealized,
        data_gaps=tuple(dict.fromkeys(gaps)),
    )


async def get_daily_loss_guard_status(
    session: AsyncSession,
    gateway=None,
    *,
    price_lookup: dict[str, Decimal] | None = None,
    account_ref: str | None = None,
    account_session_ref: str | None = None,
    raw_account: dict[str, Any] | None = None,
    gateway_health: dict[str, Any] | None = None,
    require_verified_account_scope: bool = True,
) -> DailyLossGuardStatus:
    """Resolve configuration, capital, account-scoped PnL and guard state.

    Account equity is accepted only from a reliable, fresh, identity-matched
    gateway payload. The check deliberately does not use buying power or cash
    reservation policy: those values have no bearing on the loss-cap base.
    """

    configured_pct: Decimal | None = None
    configured_tl: Decimal | None = None
    try:
        configured_pct = _finite_decimal(
            await get_admin_config_value(session, "dailyMaxLossPct")
        )
        configured_tl = _finite_decimal(
            await get_admin_config_value(session, "dailyMaxLossTl")
        )
        if not Decimal("0") <= configured_pct <= Decimal("10"):
            raise ValueError("dailyMaxLossPct must be between 0 and 10")
        if configured_tl < 0:
            raise ValueError("daily loss guard configuration cannot be negative")
    except Exception as exc:
        logger.exception("Daily loss guard configuration is unavailable")
        return DailyLossGuardStatus(
            status=DailyLossGuardState.UNAVAILABLE,
            configured_pct=configured_pct,
            configured_tl=configured_tl,
            reason=f"daily loss guard configuration unavailable: {exc}",
        )

    if configured_pct == 0 and configured_tl == 0:
        return DailyLossGuardStatus(
            status=DailyLossGuardState.DISABLED,
            configured_pct=configured_pct,
            configured_tl=configured_tl,
            reason="daily percentage and absolute loss caps are disabled",
        )

    bot_budget = Decimal("0")
    maximum_account_age = Decimal("0")
    if configured_pct > 0:
        try:
            bot_budget = _finite_decimal(
                await get_admin_config_value(
                    session, "sizingTotalBotCapitalBudgetTl"
                )
            )
            maximum_account_age = _finite_decimal(
                await get_admin_config_value(
                    session, "sizingMaxAccountDataAgeSeconds"
                )
            )
            if bot_budget < 0 or maximum_account_age < 0:
                raise ValueError(
                    "daily loss capital configuration cannot be negative"
                )
        except Exception as exc:
            logger.exception("Daily loss capital configuration is unavailable")
            return DailyLossGuardStatus(
                status=DailyLossGuardState.UNAVAILABLE,
                configured_pct=configured_pct,
                configured_tl=configured_tl,
                reason=f"daily loss capital configuration unavailable: {exc}",
            )

    health = gateway_health
    if health is None and gateway is not None:
        try:
            health = await gateway.health()
        except Exception as exc:
            if require_verified_account_scope:
                return DailyLossGuardStatus(
                    status=DailyLossGuardState.UNAVAILABLE,
                    configured_pct=configured_pct,
                    configured_tl=configured_tl,
                    reason=f"gateway health unavailable: {exc}",
                )

    explicit_ref = str(account_ref or "").strip() or None
    explicit_session_ref = str(account_session_ref or "").strip() or None
    strict_health_ref, strict_session_ref, strict_identity_error = (
        _strict_gateway_identity(health)
    )
    if require_verified_account_scope and strict_identity_error is not None:
        return DailyLossGuardStatus(
            status=DailyLossGuardState.UNAVAILABLE,
            configured_pct=configured_pct,
            configured_tl=configured_tl,
            reason=strict_identity_error,
        )
    if require_verified_account_scope:
        if explicit_ref is not None and (
            _valid_ref(explicit_ref) is None or explicit_ref != strict_health_ref
        ):
            return DailyLossGuardStatus(
                status=DailyLossGuardState.UNAVAILABLE,
                configured_pct=configured_pct,
                configured_tl=configured_tl,
                reason="explicit accountRef does not match verified gateway account",
            )
        if explicit_session_ref is not None and (
            _valid_ref(explicit_session_ref) is None
            or explicit_session_ref != strict_session_ref
        ):
            return DailyLossGuardStatus(
                status=DailyLossGuardState.UNAVAILABLE,
                configured_pct=configured_pct,
                configured_tl=configured_tl,
                reason=(
                    "explicit accountSessionRef does not match verified gateway session"
                ),
            )

    account_payload = raw_account
    account_payload_error: str | None = None
    if configured_pct > 0 and account_payload is None and gateway is not None:
        try:
            account_payload = await gateway.get_account()
        except Exception as exc:
            account_payload_error = f"gateway account unavailable: {exc}"

    payload_ref = (
        _valid_ref(account_payload.get("accountRef"))
        if isinstance(account_payload, dict)
        else None
    )
    payload_session_ref = (
        _valid_ref(account_payload.get("accountSessionRef"))
        if isinstance(account_payload, dict)
        else None
    )
    if require_verified_account_scope and _account_payload_claims_usable(
        account_payload,
        maximum_age_seconds=(
            maximum_account_age if configured_pct > 0 else None
        ),
    ):
        if payload_ref != strict_health_ref or payload_session_ref != strict_session_ref:
            return DailyLossGuardStatus(
                status=DailyLossGuardState.UNAVAILABLE,
                configured_pct=configured_pct,
                configured_tl=configured_tl,
                reason="gateway account payload identity does not match health",
            )
    elif account_payload is not None:
        account_payload_error = "gateway account payload is unavailable or unreliable"

    if require_verified_account_scope:
        resolved_account_ref = strict_health_ref
        resolved_session_ref = strict_session_ref
    else:
        health_ref = (
            _valid_ref(health.get("accountRef"))
            if isinstance(health, dict)
            else None
        )
        health_session_ref = (
            _valid_ref(health.get("accountSessionRef"))
            if isinstance(health, dict)
            else None
        )
        resolved_account_ref = explicit_ref or health_ref or payload_ref
        resolved_session_ref = (
            explicit_session_ref or health_session_ref or payload_session_ref
        )
    if not require_verified_account_scope and resolved_account_ref is None:
        from app.services.account_watcher import account_watcher

        resolved_account_ref = account_watcher.current_account_ref()

    positive_budget = bot_budget if bot_budget > 0 else None
    equity: Decimal | None = None
    equity_error = account_payload_error
    if configured_pct > 0 and account_payload is not None:
        equity, equity_error = _verified_account_equity(
            account_payload,
            gateway_health=health if require_verified_account_scope else None,
            expected_account_ref=resolved_account_ref,
            expected_account_session_ref=resolved_session_ref,
            maximum_age_seconds=maximum_account_age,
        )

    capital_base: Decimal | None = None
    capital_source = DailyLossCapitalSource.NONE
    if configured_pct > 0:
        if equity is not None and positive_budget is not None:
            capital_base = min(equity, positive_budget)
            capital_source = DailyLossCapitalSource.MIN_ACCOUNT_EQUITY_BOT_BUDGET
        elif equity is not None:
            capital_base = equity
            capital_source = DailyLossCapitalSource.ACCOUNT_EQUITY
        elif positive_budget is not None:
            capital_base = positive_budget
            capital_source = DailyLossCapitalSource.BOT_CAPITAL_BUDGET_FALLBACK

    percentage_cap = (
        capital_base * configured_pct / Decimal("100")
        if configured_pct > 0 and capital_base is not None
        else None
    )
    known_caps = [
        cap
        for cap in (
            configured_tl if configured_tl > 0 else None,
            percentage_cap,
        )
        if cap is not None and cap > 0
    ]
    known_effective_cap = min(known_caps) if known_caps else None

    if known_effective_cap is None:
        return DailyLossGuardStatus(
            status=DailyLossGuardState.UNAVAILABLE,
            configured_pct=configured_pct,
            configured_tl=configured_tl,
            capital_source=capital_source,
            capital_base_tl=capital_base,
            percentage_cap_tl=percentage_cap,
            reason=(
                "percentage daily loss cap has no verified capital base"
                + (f": {equity_error}" if equity_error else "")
            ),
        )

    try:
        pnl = await get_daily_pnl(
            session,
            gateway,
            price_lookup=price_lookup,
            account_ref=resolved_account_ref,
        )
    except Exception as exc:
        logger.exception("Daily PnL calculation failed")
        return DailyLossGuardStatus(
            status=DailyLossGuardState.UNAVAILABLE,
            configured_pct=configured_pct,
            configured_tl=configured_tl,
            capital_source=capital_source,
            capital_base_tl=capital_base,
            percentage_cap_tl=percentage_cap,
            effective_cap_tl=known_effective_cap,
            reason=f"daily PnL calculation unavailable: {exc}",
        )

    # A loss already known from realized fills proves a breach even if the
    # percentage base or unrealized component is otherwise unavailable.
    if pnl.realized_tl <= -known_effective_cap:
        return DailyLossGuardStatus(
            status=DailyLossGuardState.BREACHED,
            configured_pct=configured_pct,
            configured_tl=configured_tl,
            capital_source=capital_source,
            capital_base_tl=capital_base,
            percentage_cap_tl=percentage_cap,
            effective_cap_tl=known_effective_cap,
            pnl=pnl,
            reason=(
                f"daily realized loss {pnl.realized_tl} TL breaches effective "
                f"daily loss cap {known_effective_cap} TL"
            ),
        )

    if pnl.data_gaps:
        logger.warning(
            "DAILY_PNL_DATA_GAP — blocking new BUY fail-closed gaps=%s "
            "realized=%s",
            ";".join(pnl.data_gaps),
            pnl.realized_tl,
        )
        return DailyLossGuardStatus(
            status=DailyLossGuardState.UNAVAILABLE,
            configured_pct=configured_pct,
            configured_tl=configured_tl,
            capital_source=capital_source,
            capital_base_tl=capital_base,
            percentage_cap_tl=percentage_cap,
            effective_cap_tl=known_effective_cap,
            pnl=pnl,
            reason=f"daily PnL has {len(pnl.data_gaps)} unresolved data gap(s)",
        )

    if pnl.total_tl <= -known_effective_cap:
        return DailyLossGuardStatus(
            status=DailyLossGuardState.BREACHED,
            configured_pct=configured_pct,
            configured_tl=configured_tl,
            capital_source=capital_source,
            capital_base_tl=capital_base,
            percentage_cap_tl=percentage_cap,
            effective_cap_tl=known_effective_cap,
            pnl=pnl,
            reason=(
                f"daily loss {pnl.total_tl} TL (realized={pnl.realized_tl}, "
                f"unrealizedToday={pnl.unrealized_today_tl}) breaches effective "
                f"daily loss cap {known_effective_cap} TL"
            ),
        )

    if configured_pct > 0 and percentage_cap is None:
        return DailyLossGuardStatus(
            status=DailyLossGuardState.UNAVAILABLE,
            configured_pct=configured_pct,
            configured_tl=configured_tl,
            capital_source=capital_source,
            capital_base_tl=capital_base,
            effective_cap_tl=known_effective_cap,
            pnl=pnl,
            reason=(
                "percentage daily loss cap has no verified capital base"
                + (f": {equity_error}" if equity_error else "")
            ),
        )

    return DailyLossGuardStatus(
        status=DailyLossGuardState.AVAILABLE,
        configured_pct=configured_pct,
        configured_tl=configured_tl,
        capital_source=capital_source,
        capital_base_tl=capital_base,
        percentage_cap_tl=percentage_cap,
        effective_cap_tl=known_effective_cap,
        pnl=pnl,
    )


async def is_daily_loss_limit_breached(
    session: AsyncSession,
    gateway=None,
    *,
    price_lookup: dict[str, Decimal] | None = None,
    account_ref: str | None = None,
) -> tuple[bool, str | None]:
    """Compatibility wrapper returning only an actual breach and its reason."""

    status = await get_daily_loss_guard_status(
        session,
        gateway,
        price_lookup=price_lookup,
        account_ref=account_ref,
        require_verified_account_scope=False,
    )
    if status.status == DailyLossGuardState.BREACHED:
        return True, status.reason
    return False, None


async def apply_daily_loss_limit(
    response: SignalResponse, *, gateway=None
) -> SignalResponse:
    """Return a clean WAIT copy when an actionable BUY cannot pass the guard."""

    if response.action != SignalAction.BUY:
        return response
    if not response.allow_order:
        return response
    try:
        async with async_session_factory() as session:
            status = await get_daily_loss_guard_status(session, gateway)
        blocked = status.blocks_buy
        reason = status.reason
        state = status.status.value
    except Exception as exc:
        logger.exception("Daily loss guard check failed — blocking BUY fail-closed")
        blocked = True
        reason = f"daily loss guard check failed: {exc}"
        state = DailyLossGuardState.UNAVAILABLE.value
    if not blocked:
        return response
    logger.warning(
        "DAILY_LOSS_GUARD_BLOCKED_BUY symbol=%s requestId=%s status=%s reason=%s",
        response.symbol,
        response.request_id,
        state,
        reason,
    )
    return response.model_copy(
        update={
            "action": SignalAction.WAIT,
            "allow_order": False,
            "qty": 0,
            "order_type": OrderType.NONE,
            "price": None,
            "entry_range": None,
            "stop_loss": None,
            "target_price": None,
            "target_allocation_pct": None,
            "reason": f"Daily loss limit ({state}): {reason} | {response.reason}",
        }
    )
