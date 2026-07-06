"""Broker flow service — institutional flow data for AI trading decisions.

Currently returns empty mock data. Future versions will integrate with:

- **Matriks AKD** — broker-specific net flow data (Turkish equity market)
- **Fintables** — aggregated institutional flow

The return structure is designed so real providers can be plugged in without
changing the consumer (signal endpoint).

Schema per symbol::

    {
      "symbol": "THYAO",
      "brokerFlow": "BUY" | "SELL" | "NEUTRAL" | "UNKNOWN",
      "netInstitutionalFlow": 1_250_000.0 | null,
      "topBrokers": [
        {"brokerName": "...", "netFlow": 500_000.0, "side": "BUY"},
        ...
      ],
      "comment": "...",
    }
"""

from __future__ import annotations

from typing import Any


# ── Public interface ───────────────────────────────────────────────────────────


async def get_broker_flow_context(symbols: list[str]) -> dict[str, Any]:
    """Return broker / institutional flow context for a list of symbols.

    Args:
        symbols: List of trading symbols (e.g. ``["THYAO", "AKBNK"]``).

    Returns:
        Dict keyed by symbol, each with ``brokerFlow``, ``netInstitutionalFlow``,
        ``topBrokers``, and ``comment``. Currently returns ``UNKNOWN`` / ``None`` /
        empty list / placeholder comment as a safe mock for all symbols.
    """
    context: dict[str, Any] = {}
    for symbol in symbols:
        context[symbol] = _mock_entry(symbol)
    return context


# ── Internal helpers ──────────────────────────────────────────────────────────


def _mock_entry(symbol: str) -> dict[str, Any]:
    """Return a safe default entry when no real data source is available."""
    return {
        "symbol": symbol,
        "brokerFlow": "UNKNOWN",
        "netInstitutionalFlow": None,
        "topBrokers": [],
        "comment": "Broker flow data not provided.",
    }


# ── Future: real provider interface (stub) ────────────────────────────────────


# class BrokerFlowProvider(ABC):
#     """Pluggable broker flow data source (Matriks AKD, Fintables, ...)."""
#
#     @abstractmethod
#     async def fetch(self, symbols: list[str]) -> dict[str, Any]:
#         ...
#
#
# class MatriksAKDProvider(BrokerFlowProvider):
#     async def fetch(self, symbols: list[str]) -> dict[str, Any]:
#         # POST https://matriks.io/api/akd/...
#         ...
