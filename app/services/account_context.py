"""Fail-closed normalization of Matriks/broker account data for BUY sizing.

Raw gateway payloads never cross this boundary.  Field mappings are explicit,
money is represented by :class:`Decimal`, and ambiguous buying-power or open
order semantics make the resulting context unreliable instead of inventing a
zero/default balance.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import AccountNormalizationAudit
from app.services.admin_config import get_admin_config_value
from app.services.effective_risk_config import decimal_from_external
from app.services.position_sizing import AccountSizingContext


ReservationHandling = Literal["BROKER_ALREADY_DEDUCTED", "BACKEND_DEDUCTED", "UNKNOWN"]


async def get_account_reservation_handling(
    session: AsyncSession,
) -> ReservationHandling:
    raw = await get_admin_config_value(session, "accountReservationHandling")
    value = str(raw).strip().upper()
    if value not in {
        "BROKER_ALREADY_DEDUCTED",
        "BACKEND_DEDUCTED",
        "UNKNOWN",
    }:
        return "UNKNOWN"
    return value  # type: ignore[return-value]


class NormalizedBrokerAccount(BaseModel):
    model_config = ConfigDict(frozen=True)

    account_equity_tl: Decimal | None
    settled_cash_tl: Decimal | None
    broker_reported_buying_power_tl: Decimal | None
    withdrawable_cash_tl: Decimal | None
    unsettled_receivables_tl: Decimal | None
    credit_limit_tl: Decimal | None
    used_credit_tl: Decimal | None
    broker_reserved_cash_tl: Decimal | None
    backend_reserved_cash_tl: Decimal
    effective_available_cash_tl: Decimal | None
    total_account_exposure_tl: Decimal | None
    account_data_age_seconds: Decimal | None
    account_data_reliable: bool
    unreliable_reasons: list[str]
    reservation_handling: ReservationHandling
    source_provider: str
    source_fields: dict[str, str]
    normalization_policy: str
    margin_buying_enabled: bool


class _MappedValues(BaseModel):
    account_equity_tl: Decimal | None = None
    settled_cash_tl: Decimal | None = None
    broker_reported_buying_power_tl: Decimal | None = None
    withdrawable_cash_tl: Decimal | None = None
    unsettled_receivables_tl: Decimal | None = None
    credit_limit_tl: Decimal | None = None
    used_credit_tl: Decimal | None = None
    broker_reserved_cash_tl: Decimal | None = None
    source_fields: dict[str, str] = Field(default_factory=dict)
    unreliable_reasons: list[str] = Field(default_factory=list)


def _casefolded(raw: dict[str, Any]) -> dict[str, tuple[str, Any]]:
    return {str(key).casefold(): (str(key), value) for key, value in raw.items()}


class BaseAccountNormalizer:
    """Strategy base; subclasses declare every accepted semantic alias."""

    provider_names: ClassVar[frozenset[str]] = frozenset()
    normalization_policy: ClassVar[str] = "FAIL_CLOSED_UNKNOWN_PROVIDER"
    field_aliases: ClassVar[dict[str, tuple[str, ...]]] = {}
    margin_buying_power_aliases: ClassVar[tuple[str, ...]] = ()

    def normalize(
        self, raw: dict[str, Any], *, allow_margin_buying: bool
    ) -> _MappedValues:
        indexed = _casefolded(raw)
        values: dict[str, Any] = {}
        sources: dict[str, str] = {}
        reasons: list[str] = []
        for target, aliases in self.field_aliases.items():
            value, source, error = self._read_decimal(indexed, aliases)
            values[target] = value
            if source is not None:
                sources[target] = f"raw.account.{source}"
            if error is not None:
                reasons.append(f"{target}: {error}")

        buying_power = values.get("broker_reported_buying_power_tl")
        if buying_power is None and allow_margin_buying:
            value, source, error = self._read_decimal(
                indexed, self.margin_buying_power_aliases
            )
            values["broker_reported_buying_power_tl"] = value
            if source is not None:
                sources["broker_reported_buying_power_tl"] = (
                    f"raw.account.{source} (margin)"
                )
            if error is not None:
                reasons.append(f"broker_reported_buying_power_tl: {error}")
        return _MappedValues(
            **values, source_fields=sources, unreliable_reasons=reasons
        )

    @staticmethod
    def _read_decimal(
        indexed: dict[str, tuple[str, Any]], aliases: tuple[str, ...]
    ) -> tuple[Decimal | None, str | None, str | None]:
        for alias in aliases:
            found = indexed.get(alias.casefold())
            if found is None:
                continue
            source, raw_value = found
            if raw_value is None or str(raw_value).strip() == "":
                return None, source, "value is missing"
            try:
                return decimal_from_external(raw_value), source, None
            except ValueError:
                return None, source, "value is not a finite decimal"
        return None, None, None


class DefaultMatriksAccountNormalizer(BaseAccountNormalizer):
    """Mapping for semantically named fields exposed by TradeAiGateway.

    ``AvailableBalance`` is intentionally absent until its exact meaning is
    verified in the target Matriks/araci-kurum combination.
    """

    provider_names = frozenset({"MATRIKS_IQ", "MATRIKSIQ", "MATRIKS"})
    normalization_policy = "MATRIKS_EXPLICIT_ORDERABLE_CASH_V1"
    field_aliases = {
        "account_equity_tl": (
            "TotalEquity",
            "AccountEquity",
            "Equity",
            "Overall",
        ),
        "settled_cash_tl": ("SettledCash", "CashBalance"),
        "broker_reported_buying_power_tl": (
            "OrderableCash",
            "AvailableBuyingPower",
            "AvailableBalanceForBuyOrders",
            "PurchasingPower",
            "AvailableMargin",
        ),
        "withdrawable_cash_tl": ("WithdrawableCash", "WithdrawableBalance"),
        "unsettled_receivables_tl": (
            "UnsettledReceivables",
            "PendingSaleReceivables",
            "T1Balance",
            "T2Balance",
        ),
        "credit_limit_tl": ("CreditLimit", "MarginLimit"),
        "used_credit_tl": ("UsedCredit", "UsedMargin"),
        "broker_reserved_cash_tl": ("ReservedCash", "OpenOrderReservedCash"),
    }
    margin_buying_power_aliases = ("MarginBuyingPower", "CreditPurchasingPower")


class BrokerSpecificAccountNormalizer(BaseAccountNormalizer):
    """Base for verified broker-specific mappings registered by provider id."""

    normalization_policy = "BROKER_SPECIFIC_VERIFIED_MAPPING"


class UnknownBrokerAccountNormalizer(BaseAccountNormalizer):
    normalization_policy = "FAIL_CLOSED_UNKNOWN_PROVIDER"

    def normalize(
        self, raw: dict[str, Any], *, allow_margin_buying: bool
    ) -> _MappedValues:
        del raw, allow_margin_buying
        return _MappedValues(unreliable_reasons=["unknown broker/provider mapping"])


_NORMALIZER_REGISTRY: dict[str, type[BaseAccountNormalizer]] = {
    provider: DefaultMatriksAccountNormalizer
    for provider in DefaultMatriksAccountNormalizer.provider_names
}


def register_broker_normalizer(
    normalizer: type[BrokerSpecificAccountNormalizer],
) -> None:
    """Register a target-verified broker strategy without changing adapter flow."""
    for provider in normalizer.provider_names:
        _NORMALIZER_REGISTRY[provider.upper()] = normalizer


def _parse_age(raw_account: dict[str, Any]) -> tuple[Decimal | None, str | None]:
    for key in ("accountDataAgeSeconds", "dataAgeSeconds"):
        if key in raw_account:
            try:
                age = decimal_from_external(raw_account[key])
            except ValueError:
                return None, f"{key} is not a finite decimal"
            if age < 0:
                return None, f"{key} cannot be negative"
            return age, None
    for key in ("receivedAtUtc", "receivedAt", "timestamp"):
        raw_value = raw_account.get(key)
        if not raw_value:
            continue
        try:
            timestamp = datetime.fromisoformat(str(raw_value).replace("Z", "+00:00"))
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            age_seconds = max(
                Decimal("0"),
                Decimal(str((datetime.now(timezone.utc) - timestamp).total_seconds())),
            )
            return age_seconds, None
        except (ValueError, TypeError):
            return None, f"{key} is not a valid timestamp"
    return None, "account data age is unknown"


def _position_symbol(position: dict[str, Any]) -> str:
    return str(position.get("symbol") or position.get("Symbol") or "").strip().upper()


def _position_qty(position: dict[str, Any]) -> int:
    for key in ("accountNetQty", "totalQty", "qty", "quantity"):
        if key not in position:
            continue
        value = decimal_from_external(position[key])
        integral = value.to_integral_value()
        if value != integral:
            raise ValueError(f"position {key} must be an integer")
        return int(integral)
    raise ValueError("position quantity is missing")


def _bot_position_qty(position: dict[str, Any]) -> int:
    for key in ("botQty", "botPositionQty"):
        if key not in position:
            continue
        value = decimal_from_external(position[key])
        integral = value.to_integral_value()
        if value != integral or value < 0:
            raise ValueError(f"position {key} must be a non-negative integer")
        return int(integral)
    raise ValueError("position bot quantity is missing")


class MatriksAccountContextAdapter:
    """Convert one fresh account/position bundle into TASK 1A's typed input."""

    def __init__(
        self,
        *,
        reservation_handling: ReservationHandling = "UNKNOWN",
        allow_margin_buying: bool = False,
        max_account_data_age_seconds: Decimal | None = None,
    ) -> None:
        self.reservation_handling = reservation_handling
        self.allow_margin_buying = bool(allow_margin_buying)
        self.max_account_data_age_seconds = max_account_data_age_seconds
        self.last_normalized: NormalizedBrokerAccount | None = None

    def normalize(
        self,
        *,
        raw_account: dict,
        raw_positions: list[dict],
        raw_open_orders: list[dict],
        backend_reserved_cash_tl: Decimal,
        symbol: str = "",
        market_prices: dict[str, Decimal] | None = None,
        bot_owned_qty_by_symbol: dict[str, Decimal] | None = None,
        current_symbol_reserved_cash_tl: Decimal = Decimal("0"),
    ) -> AccountSizingContext:
        del raw_open_orders  # reservations are authoritative backend ledger values
        backend_reserved = decimal_from_external(backend_reserved_cash_tl)
        symbol_reserved = decimal_from_external(current_symbol_reserved_cash_tl)
        if backend_reserved < 0:
            raise ValueError("backend_reserved_cash_tl cannot be negative")
        if symbol_reserved < 0:
            raise ValueError("current_symbol_reserved_cash_tl cannot be negative")

        account_payload = raw_account.get("account")
        if not isinstance(account_payload, dict):
            account_payload = raw_account
        provider = (
            str(
                raw_account.get("sourceProvider")
                or raw_account.get("provider")
                or account_payload.get("sourceProvider")
                or account_payload.get("provider")
                or "UNKNOWN"
            )
            .strip()
            .upper()
        )
        normalizer_type = _NORMALIZER_REGISTRY.get(
            provider, UnknownBrokerAccountNormalizer
        )
        normalizer = normalizer_type()
        mapped = normalizer.normalize(
            account_payload, allow_margin_buying=self.allow_margin_buying
        )
        reasons = list(mapped.unreliable_reasons)
        age, age_error = _parse_age(raw_account)
        if age_error:
            reasons.append(age_error)
        if (
            age is not None
            and self.max_account_data_age_seconds is not None
            and age > self.max_account_data_age_seconds
        ):
            reasons.append("account data is stale")

        if mapped.account_equity_tl is None:
            reasons.append("account equity field is missing or unverified")
        elif mapped.account_equity_tl <= 0:
            reasons.append("account equity must be positive")
        buying_power = mapped.broker_reported_buying_power_tl
        if buying_power is None:
            reasons.append("orderable buying power field is missing or unverified")
        elif buying_power < 0:
            reasons.append("broker buying power cannot be negative")

        if self.reservation_handling == "BROKER_ALREADY_DEDUCTED":
            effective_cash = buying_power
        elif self.reservation_handling == "BACKEND_DEDUCTED":
            effective_cash = (
                None
                if buying_power is None
                else max(Decimal("0"), buying_power - backend_reserved)
            )
        else:
            effective_cash = None
            reasons.append("open-order reservation handling is unknown")

        normalized_symbol = symbol.strip().upper()
        prices = {
            key.strip().upper(): decimal_from_external(value)
            for key, value in (market_prices or {}).items()
        }
        authoritative_bot_qty = (
            {
                key.strip().upper(): decimal_from_external(value)
                for key, value in bot_owned_qty_by_symbol.items()
            }
            if bot_owned_qty_by_symbol is not None
            else None
        )
        seen_symbols: set[str] = set()
        current_qty = 0
        current_value: Decimal | None = Decimal("0")
        total_exposure: Decimal | None = Decimal("0")
        current_bot_value: Decimal | None = Decimal("0")
        total_bot_exposure: Decimal | None = Decimal("0")
        for position in raw_positions:
            position_symbol = _position_symbol(position)
            if not position_symbol:
                reasons.append("position symbol is missing")
                total_exposure = None
                total_bot_exposure = None
                continue
            seen_symbols.add(position_symbol)
            try:
                qty = _position_qty(position)
            except ValueError as exc:
                reasons.append(f"{position_symbol}: {exc}")
                total_exposure = None
                total_bot_exposure = None
                if position_symbol == normalized_symbol:
                    current_value = None
                    current_bot_value = None
                continue
            if authoritative_bot_qty is not None:
                bot_qty_value = authoritative_bot_qty.get(position_symbol, Decimal("0"))
                if bot_qty_value != bot_qty_value.to_integral_value() or bot_qty_value < 0:
                    reasons.append(
                        f"{position_symbol}: authoritative bot quantity is invalid"
                    )
                    total_bot_exposure = None
                    if position_symbol == normalized_symbol:
                        current_bot_value = None
                    bot_qty = 0
                else:
                    bot_qty = int(bot_qty_value)
            else:
                try:
                    bot_qty = _bot_position_qty(position)
                except ValueError as exc:
                    reasons.append(f"{position_symbol}: {exc}")
                    total_bot_exposure = None
                    if position_symbol == normalized_symbol:
                        current_bot_value = None
                    bot_qty = 0
            if bot_qty > max(0, qty):
                reasons.append(
                    f"{position_symbol}: bot quantity exceeds account quantity"
                )
                total_bot_exposure = None
                if position_symbol == normalized_symbol:
                    current_bot_value = None
            if position_symbol == normalized_symbol:
                current_qty = max(0, qty)
            if qty == 0:
                continue
            price = prices.get(position_symbol)
            if price is None or price <= 0:
                reasons.append(f"fresh market price missing for {position_symbol}")
                total_exposure = None
                total_bot_exposure = None
                if position_symbol == normalized_symbol:
                    current_value = None
                    current_bot_value = None
                continue
            value = Decimal(abs(qty)) * price
            if total_exposure is not None:
                total_exposure += value
            if position_symbol == normalized_symbol:
                current_value = Decimal(max(0, qty)) * price
                if current_bot_value is not None:
                    current_bot_value = Decimal(bot_qty) * price
            if total_bot_exposure is not None:
                total_bot_exposure += Decimal(bot_qty) * price

        if authoritative_bot_qty is not None:
            missing_bot_symbols = {
                symbol
                for symbol, qty in authoritative_bot_qty.items()
                if qty > 0 and symbol not in seen_symbols
            }
            if missing_bot_symbols:
                reasons.append(
                    "bot-owned symbols absent from account snapshot: "
                    + ",".join(sorted(missing_bot_symbols))
                )
                total_bot_exposure = None
                if normalized_symbol in missing_bot_symbols:
                    current_bot_value = None

        explicitly_reliable = raw_account.get("accountDataReliable", True)
        if explicitly_reliable is not True:
            reasons.append("gateway marked account data unreliable")
        if raw_account.get("available", True) is not True:
            reasons.append("gateway account data is unavailable")
        reliable = not reasons
        self.last_normalized = NormalizedBrokerAccount(
            **mapped.model_dump(exclude={"source_fields", "unreliable_reasons"}),
            backend_reserved_cash_tl=backend_reserved,
            effective_available_cash_tl=effective_cash,
            total_account_exposure_tl=total_exposure,
            account_data_age_seconds=age,
            account_data_reliable=reliable,
            unreliable_reasons=list(dict.fromkeys(reasons)),
            reservation_handling=self.reservation_handling,
            source_provider=provider,
            source_fields=mapped.source_fields,
            normalization_policy=normalizer.normalization_policy,
            margin_buying_enabled=self.allow_margin_buying,
        )
        return AccountSizingContext(
            account_equity_tl=mapped.account_equity_tl,
            effective_available_cash_tl=effective_cash,
            reserved_cash_tl=backend_reserved,
            current_symbol_qty=current_qty,
            current_symbol_value_tl=current_value,
            total_account_exposure_tl=total_exposure,
            current_bot_symbol_value_tl=current_bot_value,
            total_bot_exposure_tl=total_bot_exposure,
            account_data_age_seconds=age,
            account_data_reliable=reliable,
            current_symbol_reserved_cash_tl=symbol_reserved,
        )

    async def add_audit(
        self,
        session: AsyncSession,
        *,
        request_id: str | None,
        symbol: str | None,
    ) -> AccountNormalizationAudit:
        if self.last_normalized is None:
            raise RuntimeError("normalize must be called before add_audit")
        value = self.last_normalized
        row = AccountNormalizationAudit(
            request_id=request_id,
            symbol=symbol.strip().upper() if symbol else None,
            source_provider=value.source_provider,
            source_fields=value.source_fields,
            normalization_policy=value.normalization_policy,
            reservation_handling=value.reservation_handling,
            account_data_reliable=value.account_data_reliable,
            unreliable_reasons=value.unreliable_reasons,
            account_data_age_seconds=value.account_data_age_seconds,
            margin_buying_enabled=value.margin_buying_enabled,
            broker_reported_buying_power_tl=value.broker_reported_buying_power_tl,
            backend_reserved_cash_tl=value.backend_reserved_cash_tl,
            effective_available_cash_tl=value.effective_available_cash_tl,
        )
        session.add(row)
        await session.flush()
        return row


@dataclass(frozen=True)
class FreshAccountInputs:
    raw_account: dict[str, Any]
    raw_positions: list[dict[str, Any]]
    raw_open_orders: list[dict[str, Any]]
    market_prices: dict[str, Decimal]


def is_position_snapshot_complete(payload: dict[str, Any]) -> bool:
    """Accept the Matriks demo fallback only when its full contract is present."""

    if payload.get("snapshotCompleteFlag") is True:
        return True
    return bool(
        payload.get("snapshotCompleteFlag") is False
        and payload.get("snapshotNonEmpty") is True
        and str(payload.get("confidence") or "").upper() == "MEDIUM"
    )


async def fetch_fresh_account_inputs(
    gateway: Any,
    *,
    symbol: str,
    target_snapshot: dict[str, Any] | None = None,
    expected_account_ref: str | None = None,
    expected_account_session_ref: str | None = None,
    max_position_age_seconds: Decimal = Decimal("60"),
    max_quote_age_seconds: Decimal = Decimal("30"),
) -> FreshAccountInputs:
    """Fetch one non-cached account bundle and current prices for exposure.

    Any timeout, malformed wrapper or missing price raises; callers must block
    BUY rather than reusing a previous successful value.
    """
    raw_account, positions_wrapper, orders_wrapper = await asyncio.gather(
        gateway.get_account(), gateway.get_positions(), gateway.get_active_orders()
    )
    if not isinstance(raw_account, dict) or not raw_account.get("ok", True):
        raise ValueError("gateway account payload is unavailable")
    if not isinstance(positions_wrapper, dict) or not positions_wrapper.get("ok", True):
        raise ValueError("gateway positions payload is unavailable")
    if not isinstance(orders_wrapper, dict) or not orders_wrapper.get("ok", True):
        raise ValueError("gateway active-orders payload is unavailable")
    positions = positions_wrapper.get("positions")
    orders = orders_wrapper.get("orders")
    if not isinstance(positions, list) or not isinstance(orders, list):
        raise ValueError("gateway account bundle has an invalid collection")
    if (
        positions_wrapper.get("positionsLoaded") is not True
        or not is_position_snapshot_complete(positions_wrapper)
        or str(positions_wrapper.get("confidence") or "").upper()
        not in {"HIGH", "MEDIUM"}
    ):
        raise ValueError("gateway positions snapshot is incomplete or unreliable")
    position_age = decimal_from_external(
        positions_wrapper.get("snapshotAgeSeconds")
    )
    if position_age < 0 or position_age > max_position_age_seconds:
        raise ValueError("gateway positions snapshot is stale")

    account_ref = str(raw_account.get("accountRef") or "").strip()
    positions_account_ref = str(positions_wrapper.get("accountRef") or "").strip()
    account_session_ref = str(raw_account.get("accountSessionRef") or "").strip()
    positions_session_ref = str(
        positions_wrapper.get("accountSessionRef") or ""
    ).strip()
    if (
        len(account_ref) != 64
        or len(positions_account_ref) != 64
        or account_ref != positions_account_ref
        or len(account_session_ref) != 64
        or len(positions_session_ref) != 64
        or account_session_ref != positions_session_ref
    ):
        raise ValueError("account and positions identity mismatch")
    if expected_account_ref and account_ref != expected_account_ref:
        raise ValueError("fresh account identity changed before sizing")
    if (
        expected_account_session_ref
        and account_session_ref != expected_account_session_ref
    ):
        raise ValueError("fresh account session changed before sizing")

    required_symbols = {symbol.strip().upper()}
    for position in positions:
        if not isinstance(position, dict):
            raise ValueError("gateway position entry must be an object")
        try:
            qty = _position_qty(position)
        except ValueError:
            qty = 1  # force adapter to audit/fail closed instead of hiding it
        position_symbol = _position_symbol(position)
        if qty != 0 and position_symbol:
            required_symbols.add(position_symbol)

    snapshots: dict[str, dict[str, Any]] = {}
    normalized_target = symbol.strip().upper()
    if target_snapshot is not None:
        snapshots[normalized_target] = target_snapshot
    missing = sorted(required_symbols - set(snapshots))
    fetched = await asyncio.gather(*(gateway.get_snapshot(item) for item in missing))
    snapshots.update(zip(missing, fetched, strict=True))

    market_prices: dict[str, Decimal] = {}
    for item, snapshot in snapshots.items():
        if not isinstance(snapshot, dict) or not snapshot.get("ok", True):
            raise ValueError(f"fresh snapshot unavailable for {item}")
        payload = snapshot.get("payload")
        if not isinstance(payload, dict):
            raise ValueError(f"fresh snapshot payload missing for {item}")
        raw_price = payload.get("lastPrice")
        price = decimal_from_external(raw_price)
        if price <= 0:
            raise ValueError(f"fresh market price invalid for {item}")
        quote_age = decimal_from_external(payload.get("quoteAgeSeconds"))
        if payload.get("quoteReliable") is not True or quote_age > max_quote_age_seconds:
            raise ValueError(f"fresh market price is stale or unreliable for {item}")
        market_prices[item] = price
    return FreshAccountInputs(
        raw_account=raw_account,
        raw_positions=positions,
        raw_open_orders=orders,
        market_prices=market_prices,
    )
