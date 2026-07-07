"""Risk management configuration.

Centralised rules that govern every trading decision. Rules are loaded from
environment variables (prefix ``RISK_``) with hard-coded defaults as fallback.

Usage::

    from app.core.risk_config import risk_config

    if risk_config.is_symbol_allowed("THYAO"):
        ...

Override via ``.env``::

    RISK_ALLOWED_SYMBOLS=THYAO,AKBNK,GARAN
    RISK_MAX_DAILY_TRADE_COUNT=5
    RISK_TIMEZONE=Europe/Istanbul
"""

from __future__ import annotations

import json
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def _to_set(raw: str) -> set[str]:
    """Parse a comma-separated string into a set of trimmed, uppercase symbols."""
    return {s.strip().upper() for s in raw.split(",") if s.strip()}


class RiskConfig(BaseSettings):
    """Risk rules — loaded from ``RISK_*`` env vars or defaults."""

    model_config = SettingsConfigDict(
        env_prefix="RISK_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── Symbol allow-lists (comma-separated strings) ───────────────────────

    allowed_symbols: str = Field(
        default="THYAO,AKBNK,SISE,KCHOL,TUPRS",
        description="Comma-separated list of symbols that may be traded",
    )

    locked_long_term_symbols: str = Field(
        default="ASELS,EREGL",
        description="Symbols held long-term — never auto-sold",
    )

    # ── Position limits ────────────────────────────────────────────────────

    max_position_value_per_symbol: float = Field(
        default=3000.0,
        ge=0,
        description="Max TL value for a single symbol position",
    )
    max_daily_trade_count: int = Field(
        default=3,
        ge=0,
        description="Max number of trades per day",
    )

    # ── Confidence thresholds ──────────────────────────────────────────────

    min_confidence_for_buy: float = Field(
        default=75.0,
        ge=0,
        le=100,
        description="Minimum AI confidence score to enter BUY",
    )
    min_confidence_for_sell: float = Field(
        default=70.0,
        ge=0,
        le=100,
        description="Minimum AI confidence score to enter SELL",
    )

    # ── Restrictions ───────────────────────────────────────────────────────

    allow_sell_long_term: bool = Field(
        default=False,
        description="Allow selling symbols marked as long-term hold",
    )
    allow_short_selling: bool = Field(
        default=False,
        description="Allow short-selling positions",
    )
    disable_trading_after: str = Field(
        default="17:30",
        description="Local time (HH:MM) after which trading is paused",
    )
    timezone: str = Field(
        default="Europe/Istanbul",
        description="IANA timezone used for trading cutoff checks",
    )

    # ── Normalisation ──────────────────────────────────────────────────────

    @field_validator("allowed_symbols", "locked_long_term_symbols", mode="before")
    @classmethod
    def _normalise_str(cls, v: Any) -> str:
        """Accept list, set, or JSON array and convert to comma-separated string."""
        if isinstance(v, (list, set)):
            return ",".join(sorted(v))
        if isinstance(v, str):
            v_stripped = v.strip()
            # Try JSON array: '["X","Y","Z"]' or "[\"X\",\"Y\",\"Z\"]"
            if v_stripped.startswith("["):
                try:
                    parsed = json.loads(v_stripped)
                    if isinstance(parsed, list):
                        return ",".join(str(x).strip() for x in parsed)
                except (json.JSONDecodeError, TypeError):
                    pass
            return v
        return str(v)

    # ── Helpers ────────────────────────────────────────────────────────────

    def is_symbol_allowed(self, symbol: str) -> bool:
        """Check whether a symbol is in the allowed trading list."""
        return symbol.strip().upper() in _to_set(self.allowed_symbols)

    def is_long_term_locked(self, symbol: str) -> bool:
        """Check whether a symbol is protected from automated sells."""
        return symbol.strip().upper() in _to_set(self.locked_long_term_symbols)

    def can_trade_now(self, now: datetime | None = None) -> bool:
        """Return True if trading is allowed at the current time."""
        timezone = ZoneInfo(self.timezone)
        if now is None:
            now = datetime.now(timezone)
        elif now.tzinfo is None:
            now = now.replace(tzinfo=timezone)
        else:
            now = now.astimezone(timezone)
        h, m = map(int, self.disable_trading_after.split(":"))
        cutoff = time(h, m)
        return now.time() < cutoff

    def get_min_confidence(self, action: str) -> float:
        """Return the confidence threshold for a given action."""
        action = action.strip().upper()
        if action == "BUY":
            return self.min_confidence_for_buy
        if action == "SELL":
            return self.min_confidence_for_sell
        return 100.0  # unknown actions require maximum confidence

    def _allowed_set(self) -> set[str]:
        """Return the allowed symbols as a set (for injection into AI prompts)."""
        return _to_set(self.allowed_symbols)

    def _locked_set(self) -> set[str]:
        """Return the locked long-term symbols as a set (for injection into AI prompts)."""
        return _to_set(self.locked_long_term_symbols)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

risk_config = RiskConfig()
