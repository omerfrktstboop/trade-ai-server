"""Classify free-text risk and gateway blocks into stable admin categories."""

from __future__ import annotations

import re


_RULES = (
    (
        "DATA_QUALITY_UNRELIABLE",
        ("pre-flight data-quality gate", "data quality unreliable"),
    ),
    (
        "PREFLIGHT_NEUTRAL",
        ("pre-flight cost gate", "pre-flight gate: indicator consensus neutral"),
    ),
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
    (
        "INDICATOR_CONSENSUS_OPPOSES",
        ("indicatorconsensus=", "indicator consensus opposes", "consensus opposes"),
    ),
    ("PAPER_MODE", ("paper mode", "paper")),
    ("DEMO_NOT_ENABLED", ("demo",)),
    ("REAL_NOT_ENABLED", ("real live", "real")),
    ("POSITIONS_NOT_LOADED", ("positions not loaded", "pozisyonlar")),
    ("SELL_NO_POSITION", ("no position", "pozisyon yok")),
    ("LOCKED_LONG_TERM", ("locked long", "long term")),
    ("GATEWAY_REJECTED", ("rejected", "gateway", "matriks")),
    ("DUPLICATE_REQUEST", ("duplicate",)),
    ("COOLDOWN", ("cooldown",)),
)

_STRUCTURED_CATEGORY_RE = re.compile(r"^\[([A-Z][A-Z0-9_]*)\]")
_STABLE_CATEGORIES = frozenset(category for category, _needles in _RULES)


def format_block_reason(category: str, reason: str) -> str:
    """Prefix a human-readable reason with a classifier-stable category."""
    normalized = category.strip().upper()
    if normalized not in _STABLE_CATEGORIES:
        raise ValueError(f"Unknown block category: {category!r}")
    return f"[{normalized}] {reason.strip()}"


def classify_block_reason(reason: str | None) -> str:
    raw = (reason or "").strip()
    structured = _STRUCTURED_CATEGORY_RE.match(raw)
    if structured and structured.group(1) in _STABLE_CATEGORIES:
        return structured.group(1)

    text = raw.casefold()
    for category, needles in _RULES:
        if any(needle in text for needle in needles):
            return category
    return "UNKNOWN"
