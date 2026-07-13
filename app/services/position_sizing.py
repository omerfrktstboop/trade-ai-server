"""Deterministic, broker-independent BUY position sizing."""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.services.effective_risk_config import (
    EffectiveRiskConfig,
    decimal_from_external,
)


def floor_decimal(value: Decimal) -> int:
    return int(value.to_integral_value(rounding=ROUND_DOWN))


class _DecimalModel(BaseModel):
    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def _safe_external_numbers(cls, value: Any, info: Any) -> Any:
        annotation = cls.model_fields[info.field_name].annotation
        if annotation is Decimal or "Decimal" in str(annotation):
            if value is None:
                return None
            return decimal_from_external(value)
        return value


class AccountSizingContext(_DecimalModel):
    account_equity_tl: Decimal | None
    effective_available_cash_tl: Decimal | None
    reserved_cash_tl: Decimal
    current_symbol_qty: int
    current_symbol_value_tl: Decimal | None
    total_account_exposure_tl: Decimal | None
    account_data_age_seconds: Decimal | None
    account_data_reliable: bool

    @field_validator("current_symbol_qty")
    @classmethod
    def _integer_qty(cls, value: int) -> int:
        if isinstance(value, bool) or value < 0:
            raise ValueError("current_symbol_qty must be a non-negative integer")
        return value


class TradeSizingContext(_DecimalModel):
    symbol: str
    entry_price: Decimal
    stop_loss: Decimal
    target_price: Decimal
    confidence: Decimal
    current_price: Decimal


class PositionSizingResult(_DecimalModel):
    allowed: bool
    qty: int
    order_value_tl: Decimal
    risk_budget_tl: Decimal
    raw_stop_distance_tl: Decimal
    slippage_buffer_tl: Decimal
    effective_stop_distance_tl: Decimal
    estimated_loss_at_stop_tl: Decimal
    binding_limits: list[str]
    reason: str
    calculation_details: dict[str, Any]


class PositionSizingService:
    """Calculate a BUY quantity with deterministic Decimal arithmetic."""

    def calculate_buy_size(
        self,
        *,
        account: AccountSizingContext,
        trade: TradeSizingContext,
        limits: EffectiveRiskConfig,
    ) -> PositionSizingResult:
        zero = Decimal("0")

        def blocked(reason: str, **values: Decimal) -> PositionSizingResult:
            return PositionSizingResult(
                allowed=False,
                qty=0,
                order_value_tl=zero,
                risk_budget_tl=values.get("risk_budget_tl", zero),
                raw_stop_distance_tl=values.get("raw_stop_distance_tl", zero),
                slippage_buffer_tl=values.get("slippage_buffer_tl", zero),
                effective_stop_distance_tl=values.get(
                    "effective_stop_distance_tl", zero
                ),
                estimated_loss_at_stop_tl=zero,
                binding_limits=[],
                reason=reason,
                calculation_details={},
            )

        if not limits.sizing_enabled:
            return blocked("BUY blocked: deterministic sizing is disabled")
        if not account.account_data_reliable:
            return blocked("BUY blocked: account sizing data is unreliable")
        if account.account_data_age_seconds is None:
            return blocked("BUY blocked: account sizing data age is unknown")
        if account.account_data_age_seconds > limits.max_account_data_age_seconds:
            return blocked("BUY blocked: account sizing data is stale")
        if account.account_equity_tl is None:
            return blocked("BUY blocked: account equity is unknown")
        if account.account_equity_tl <= zero:
            return blocked("BUY blocked: account equity must be positive")
        if account.effective_available_cash_tl is None:
            return blocked("BUY blocked: available cash is unknown")
        if account.effective_available_cash_tl <= zero:
            return blocked("BUY blocked: available cash must be positive")
        if account.reserved_cash_tl < zero:
            return blocked("BUY blocked: reserved cash cannot be negative")
        if account.current_symbol_value_tl is None:
            return blocked("BUY blocked: current symbol value is unknown")
        if account.current_symbol_value_tl < zero:
            return blocked("BUY blocked: current symbol value cannot be negative")
        if account.total_account_exposure_tl is None:
            return blocked("BUY blocked: total account exposure is unknown")
        if account.total_account_exposure_tl < zero:
            return blocked("BUY blocked: total account exposure is invalid")
        if min(trade.entry_price, trade.stop_loss, trade.target_price) <= zero:
            return blocked("BUY blocked: entry, stop and target must be positive")
        if trade.current_price <= zero:
            return blocked("BUY blocked: current price must be positive")
        if trade.stop_loss >= trade.entry_price:
            return blocked("BUY blocked: stop loss must be below entry price")
        if trade.target_price <= trade.entry_price:
            return blocked("BUY blocked: target price must be above entry price")
        if trade.confidence < limits.minimum_buy_confidence:
            return blocked("BUY blocked: confidence is below the effective minimum")

        risk_budget = (
            account.account_equity_tl * limits.risk_per_trade_pct / Decimal("100")
        )
        raw_stop_distance = trade.entry_price - trade.stop_loss
        raw_stop_pct = raw_stop_distance * Decimal("100") / trade.entry_price
        if raw_stop_pct < limits.min_stop_distance_pct:
            return blocked(
                "BUY blocked: stop distance is below the effective minimum",
                risk_budget_tl=risk_budget,
                raw_stop_distance_tl=raw_stop_distance,
            )
        if raw_stop_pct > limits.max_stop_distance_pct:
            return blocked(
                "BUY blocked: stop distance exceeds the effective maximum",
                risk_budget_tl=risk_budget,
                raw_stop_distance_tl=raw_stop_distance,
            )

        slippage_pct = min(
            limits.maximum_stop_slippage_pct,
            max(
                limits.minimum_stop_slippage_pct,
                limits.profile_stop_slippage_pct,
            ),
        )
        slippage_buffer = trade.entry_price * slippage_pct / Decimal("100")
        effective_stop_distance = raw_stop_distance + slippage_buffer
        if effective_stop_distance <= zero:
            return blocked("BUY blocked: effective stop distance is invalid")

        cash_budget = (
            account.effective_available_cash_tl
            * limits.max_cash_utilization_pct
            / Decimal("100")
        )
        max_account_exposure = (
            account.account_equity_tl * limits.max_account_exposure_pct / Decimal("100")
        )
        remaining_account_capacity = max(
            zero, max_account_exposure - account.total_account_exposure_tl
        )
        remaining_symbol_capacity = max(
            zero,
            limits.max_position_value_per_symbol - account.current_symbol_value_tl,
        )

        candidates = {
            "risk_budget": floor_decimal(risk_budget / effective_stop_distance),
            "cash_budget": floor_decimal(cash_budget / trade.entry_price),
            "account_exposure": floor_decimal(
                remaining_account_capacity / trade.entry_price
            ),
            "symbol_position": floor_decimal(
                remaining_symbol_capacity / trade.entry_price
            ),
            "order_value": floor_decimal(limits.max_order_value_tl / trade.entry_price),
            "profile_max_qty": limits.max_qty_per_order,
        }
        final_qty = min(candidates.values())
        binding_limits = [
            name for name, candidate in candidates.items() if candidate == final_qty
        ]
        details: dict[str, Any] = {
            "effective_slippage_pct": slippage_pct,
            "raw_stop_distance_pct": raw_stop_pct,
            "cash_budget_tl": cash_budget,
            "maximum_account_exposure_tl": max_account_exposure,
            "remaining_account_capacity_tl": remaining_account_capacity,
            "remaining_symbol_capacity_tl": remaining_symbol_capacity,
            "qty_by_risk": candidates["risk_budget"],
            "qty_by_cash": candidates["cash_budget"],
            "qty_by_account_exposure": candidates["account_exposure"],
            "qty_by_symbol_position": candidates["symbol_position"],
            "qty_by_order_value": candidates["order_value"],
            "qty_by_profile_max": candidates["profile_max_qty"],
        }
        if final_qty <= 0:
            result = blocked(
                "BUY blocked: deterministic sizing produced zero quantity",
                risk_budget_tl=risk_budget,
                raw_stop_distance_tl=raw_stop_distance,
                slippage_buffer_tl=slippage_buffer,
                effective_stop_distance_tl=effective_stop_distance,
            )
            return result.model_copy(
                update={
                    "binding_limits": binding_limits,
                    "calculation_details": details,
                }
            )

        order_value = Decimal(final_qty) * trade.entry_price
        estimated_loss = Decimal(final_qty) * effective_stop_distance
        if order_value < limits.min_order_value_tl:
            result = blocked(
                "BUY blocked: order value is below the effective minimum",
                risk_budget_tl=risk_budget,
                raw_stop_distance_tl=raw_stop_distance,
                slippage_buffer_tl=slippage_buffer,
                effective_stop_distance_tl=effective_stop_distance,
            )
            return result.model_copy(
                update={
                    "binding_limits": binding_limits,
                    "calculation_details": details,
                }
            )
        if estimated_loss > risk_budget:
            return blocked(
                "BUY blocked: estimated stop loss exceeds risk budget invariant",
                risk_budget_tl=risk_budget,
                raw_stop_distance_tl=raw_stop_distance,
                slippage_buffer_tl=slippage_buffer,
                effective_stop_distance_tl=effective_stop_distance,
            )

        return PositionSizingResult(
            allowed=True,
            qty=final_qty,
            order_value_tl=order_value,
            risk_budget_tl=risk_budget,
            raw_stop_distance_tl=raw_stop_distance,
            slippage_buffer_tl=slippage_buffer,
            effective_stop_distance_tl=effective_stop_distance,
            estimated_loss_at_stop_tl=estimated_loss,
            binding_limits=binding_limits,
            reason="BUY size calculated deterministically",
            calculation_details=details,
        )
