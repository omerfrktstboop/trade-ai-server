"""Fund scanner — provides fund flow context for AI trading decisions.

Currently returns empty mock data. Future versions will integrate with:

- **TEFAS** (Turkey Electronic Fund Trading Platform) — fund NAV, flows, holdings
- **KAP** (Public Disclosure Platform) — fund portfolio disclosures
- **Fintables** — aggregated fund data

The return structure is designed so real providers can be plugged in without
changing the consumer (signal endpoint).

Schema per symbol::

    {
      "fundInterest": "LOW" | "MEDIUM" | "HIGH" | "UNKNOWN",
      "topFundsHolding": [
        {"fundCode": "TCD", "fundName": "...", "weight": 8.5},
        ...
      ],
      "fundScore": 0-100,
    }
"""

from __future__ import annotations

from typing import Any


# ── Public interface ───────────────────────────────────────────────────────────


async def get_fund_context(symbols: list[str]) -> dict[str, Any]:
    """Return fund-flow context for a list of symbols.

    Args:
        symbols: List of trading symbols (e.g. ``["THYAO", "AKBNK"]``).

    Returns:
        Dict keyed by symbol, each with ``fundInterest``, ``topFundsHolding``,
        and ``fundScore``. Currently returns ``UNKNOWN`` / empty list / 0 as a
        safe mock for all symbols.
    """
    context: dict[str, Any] = {}
    for symbol in symbols:
        context[symbol] = _mock_entry()
    return context


# ── Internal helpers ──────────────────────────────────────────────────────────


def _mock_entry() -> dict[str, Any]:
    """Return a safe default entry when no real data source is available.

    ``fundInterest`` uses the enum-style values so real integrations can simply
    return one of ``LOW`` / ``MEDIUM`` / ``HIGH`` instead of ``UNKNOWN``.
    """
    return {
        "fundInterest": "UNKNOWN",
        "topFundsHolding": [],
        "fundScore": 0,
    }


# ── Future: real provider interface (stub) ────────────────────────────────────


# class FundDataProvider(ABC):
#     """Pluggable fund data source (TEFAS, KAP, Fintables, ...)."""
#
#     @abstractmethod
#     async def fetch(self, symbols: list[str]) -> dict[str, Any]:
#         ...
#
#
# class TefasProvider(FundDataProvider):
#     async def fetch(self, symbols: list[str]) -> dict[str, Any]:
#         # GET https://www.tefas.gov.tr/api/...
#         ...
#
#
# class FintablesProvider(FundDataProvider):
#     async def fetch(self, symbols: list[str]) -> dict[str, Any]:
#         # GET https://api.fintables.com/...
#         ...
