"""Small, dependency-free parsers for raw AI-provider output.

Pulled out of evaluator.py: safe-parsing helpers used when converting the
provider's raw dict response into typed SignalAction/float/Decimal values.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from app.models.signal import SignalAction


def _safe_action(raw_value: Any) -> SignalAction:
    """Parse action string safely - invalid values fall back to WAIT."""
    if not raw_value:
        return SignalAction.WAIT
    try:
        return SignalAction(str(raw_value).upper())
    except ValueError:
        return SignalAction.WAIT


def _safe_float(raw_value: Any, default: Any = 0.0) -> Any:
    """Parse a float safely - non-numeric values return the default."""
    if raw_value is None:
        return default
    try:
        return float(raw_value)
    except (ValueError, TypeError):
        return default


def _safe_decimal(raw_value: Any, default: Any = None) -> Decimal | Any:
    """Parse an external financial value without Decimal(float)."""
    if raw_value is None:
        return default
    try:
        value = raw_value if isinstance(raw_value, Decimal) else Decimal(str(raw_value))
    except (InvalidOperation, ValueError, TypeError):
        return default
    return value if value.is_finite() else default
