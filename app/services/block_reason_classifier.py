"""Classify free-text risk and gateway blocks into stable admin categories."""

from __future__ import annotations


_RULES = (
    ("KILL_SWITCH", ("kill switch",)),
    ("CUTOFF", ("cutoff", "trading cutoff")),
    ("CONFIDENCE_LOW", ("confidence", "güven", "guven")),
    ("DAILY_LIMIT", ("daily", "günlük", "gunluk", "orders per day")),
    ("MAX_POSITION", ("max position", "position value")),
    ("MAX_ORDER_VALUE", ("max order value", "order value")),
    ("MAX_QTY", ("max qty", "quantity", "adet limiti")),
    ("MARKET_REGIME", ("market regime", "rejim")),
    ("NATR_HIGH", ("natr",)),
    ("DEPTH_QUEUE_DROP", ("depth", "queue drop", "derinlik")),
    ("ALPHA_TREND_OPPOSES", ("alpha trend",)),
    ("INDICATOR_CONSENSUS_OPPOSES", ("indicator consensus", "consensus")),
    ("PAPER_MODE", ("paper mode", "paper")),
    ("MANUAL_CONFIRMATION_REQUIRED", ("confirmation", "onay")),
    ("DEMO_NOT_ENABLED", ("demo",)),
    ("REAL_NOT_ENABLED", ("real live", "real")),
    ("POSITIONS_NOT_LOADED", ("positions not loaded", "pozisyonlar")),
    ("SELL_NO_POSITION", ("no position", "pozisyon yok")),
    ("LOCKED_LONG_TERM", ("locked long", "long term")),
    ("GATEWAY_REJECTED", ("rejected", "gateway", "matriks")),
    ("DUPLICATE_REQUEST", ("duplicate",)),
    ("COOLDOWN", ("cooldown",)),
)


def classify_block_reason(reason: str | None) -> str:
    text = (reason or "").casefold()
    for category, needles in _RULES:
        if any(needle in text for needle in needles):
            return category
    return "UNKNOWN"
