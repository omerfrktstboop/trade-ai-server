"""Broker flow service — institutional (AKD) flow for AI trading decisions.

Reads the Matriks gateway's ``GET /institutions?symbol=X`` endpoint, which
returns the daily net-buyer and net-seller broker/institution rankings::

    {
      "ok": true, "available": true, "symbol": "THYAO", "period": "DAILY",
      "buyers":  [{"id":.., "name":"Yatırım Fonları", "rank":1, "value": 500000.0}, ...],
      "sellers": [{"id":.., "name":"...",             "rank":1, "value": 300000.0}, ...]
    }

``value`` is already a *net* lot figure (NetBuyerLot / NetSellerLot), so an
institution that churned both sides is netted out on the exchange feed. We add
a second guard on top of that: we compute smart money's net across BOTH lists
(``smartBuyLot - smartSellLot``) so a fund that is big on the buy ranking but
also unloading on the sell ranking cannot fake a STRONG_BUY.

"Smart money" = institutions that move on research, not retail flow:
investment funds, pension funds, and the large foreign custody desk. When they
dominate the net-buy side (≥ 40% of ranked net buying) *and* are net positive
overall, that is an asymmetric long signal (``SmartMoneyFlow = STRONG_BUY``).
The mirror case (dominant net sellers, net negative) is ``STRONG_SELL``.

Fail-closed: any gateway error, missing AKD license, or empty data degrades to
an ``UNKNOWN`` entry — broker flow is a decision INPUT, never a blocker.
"""

from __future__ import annotations

import logging
import time
import asyncio
import unicodedata
from datetime import UTC, datetime
from typing import Any

from app.services.matriks_gateway import (
    GatewayError,
    GatewayUnavailable,
    MatriksGatewayClient,
    gateway_client,
)
from app.services.decision_gate import decision_cache, decision_context_fingerprint

logger = logging.getLogger(__name__)

# Bir kurumu "akıllı para" sayan isim parçaları (normalize edilmiş, küçük harf,
# Türkçe aksan/İ sadeleştirmesi sonrası substring eşleşmesi).
_SMART_MONEY_KEYWORDS: tuple[str, ...] = (
    "yatirim fonlari",  # Yatırım Fonları
    "emeklilik",  # Emeklilik Fonları
    "citibank",  # Citibank Yabancı (yabancı takas)
    "portfoy",  # Portföy yönetim şirketleri
)

# Akıllı paranın net-alış tarafında baskın sayılması için gereken pay.
_DOMINANCE_THRESHOLD = 0.40
AKD_CACHE_TTL_SECONDS = 300
_akd_cache: dict[tuple[int, str, str, bool, str], tuple[float, dict[str, Any]]] = {}
_akd_inflight: dict[tuple[int, str, str, bool, str], asyncio.Task[dict[str, Any]]] = {}


# ── Public interface ───────────────────────────────────────────────────────────


async def get_broker_flow_context(
    symbols: list[str],
    gateway: MatriksGatewayClient | None = None,
    *,
    period: str = "Daily",
    include_reported_orders: bool = True,
    config_version: str = "",
) -> dict[str, Any]:
    """Return broker / institutional (AKD) flow context for a list of symbols.

    Args:
        symbols: Trading symbols (e.g. ``["THYAO", "AKBNK"]``).
        gateway: Optional gateway client (defaults to the shared singleton;
            injectable for tests).

    Returns:
        Dict keyed by symbol, each with ``smartMoneyFlow``
        (``STRONG_BUY`` / ``STRONG_SELL`` / ``NEUTRAL`` / ``UNKNOWN``),
        ``brokerFlow`` (legacy BUY/SELL/NEUTRAL/UNKNOWN view), the ratio and
        net-lot math behind the call, ``topBrokers``, and a human ``comment``.
        Any unavailable symbol degrades to a safe ``UNKNOWN`` entry.
    """
    gw = gateway or gateway_client
    context: dict[str, Any] = {}
    for symbol in symbols:
        normalized = symbol.strip().upper()
        normalized_period = period.strip().upper()
        cache_key = (
            id(gw),
            normalized,
            normalized_period,
            include_reported_orders,
            config_version,
        )
        cached = _akd_cache.get(cache_key)
        if cached and time.monotonic() - cached[0] < AKD_CACHE_TTL_SECONDS:
            entry = dict(cached[1])
            entry["dataAgeSeconds"] = _data_age_seconds(entry.get("asOf"))
            context[normalized] = entry
            continue
        try:
            try:
                task = _akd_inflight.get(cache_key)
                if task is None:
                    task = asyncio.create_task(
                        gw.get_institutions(
                            normalized,
                            limit=10,
                            period=period,
                            include_reported_orders=include_reported_orders,
                        )
                    )
                    _akd_inflight[cache_key] = task
                try:
                    raw = await task
                finally:
                    if _akd_inflight.get(cache_key) is task:
                        _akd_inflight.pop(cache_key, None)
            except TypeError:  # backwards-compatible injected/test gateways
                raw = await gw.get_institutions(normalized, limit=10)
        except (GatewayUnavailable, GatewayError) as exc:
            logger.debug(
                "Institutions fetch failed symbol=%s error=%s", normalized, exc
            )
            context[normalized] = _unknown_entry(normalized, "Broker flow unavailable.")
            continue
        except Exception:
            logger.exception("Institutions fetch crashed symbol=%s", normalized)
            context[normalized] = _unknown_entry(normalized, "Broker flow unavailable.")
            continue

        entry = _analyze(normalized, raw)
        entry["period"] = str(raw.get("period") or "DAILY")
        entry["available"] = (
            bool(raw.get("available")) and entry.get("smartMoneyFlow") != "UNKNOWN"
        )
        entry["asOf"] = raw.get("asOf") or raw.get("marketDate") or raw.get("date")
        entry["marketDate"] = raw.get("marketDate") or raw.get("date")
        entry["retrievedAt"] = datetime.now(UTC).isoformat()
        entry["dataAgeSeconds"] = _data_age_seconds(entry["asOf"])
        previous = _akd_cache.get(cache_key)
        if previous and decision_context_fingerprint(
            previous[1]
        ) != decision_context_fingerprint(entry):
            decision_cache.clear(normalized)
        _akd_cache[cache_key] = (time.monotonic(), entry)
        context[normalized] = entry
    return context


# ── Analysis ────────────────────────────────────────────────────────────────────


def _analyze(symbol: str, raw: dict[str, Any]) -> dict[str, Any]:
    """Turn a raw /institutions response into the smart-money context entry."""
    if not raw.get("available"):
        return _unknown_entry(symbol, "AKD data not available (license or empty).")

    buyers = _clean_side(raw.get("buyers"))
    sellers = _clean_side(raw.get("sellers"))
    if not buyers and not sellers:
        return _unknown_entry(symbol, "No ranked institutions returned.")

    total_buy = sum(b["value"] for b in buyers)
    total_sell = sum(s["value"] for s in sellers)
    smart_buy = sum(b["value"] for b in buyers if _is_smart_money(b["name"]))
    smart_sell = sum(s["value"] for s in sellers if _is_smart_money(s["name"]))

    buy_ratio = (smart_buy / total_buy) if total_buy > 0 else 0.0
    sell_ratio = (smart_sell / total_sell) if total_sell > 0 else 0.0
    # Wash-trade / iki-taraflı fon tuzağı guard'ı: akıllı paranın iki liste
    # boyunca NET lotu. Alışta baskın ama satışta da yüklüyse net sıfırlanır.
    net_smart_lot = smart_buy - smart_sell

    flow = _classify(buy_ratio, sell_ratio, net_smart_lot)

    top_brokers = [
        {"brokerName": b["name"], "netFlow": b["value"], "side": "BUY"}
        for b in buyers[:3]
    ] + [
        {"brokerName": s["name"], "netFlow": s["value"], "side": "SELL"}
        for s in sellers[:3]
    ]

    return {
        "symbol": symbol,
        "smartMoneyFlow": flow,
        # Geriye dönük uyumlu sade görünüm (eski tüketiciler/testler için).
        "brokerFlow": _legacy_flow(flow),
        "netInstitutionalFlow": round(total_buy - total_sell, 2),
        "totalRankedBuyLot": round(total_buy, 2),
        "totalRankedSellLot": round(total_sell, 2),
        "topBuyers": buyers[:5],
        "topSellers": sellers[:5],
        "smartBuyRatio": round(buy_ratio, 3),
        "smartSellRatio": round(sell_ratio, 3),
        "netSmartLot": round(net_smart_lot, 2),
        "topBrokers": top_brokers,
        "comment": _comment(flow, buy_ratio, sell_ratio, net_smart_lot),
    }


def _classify(buy_ratio: float, sell_ratio: float, net_smart_lot: float) -> str:
    """Map the smart-money math onto a flow label.

    STRONG_BUY  → funds dominate ranked net buying (≥40%) AND are net positive.
    STRONG_SELL → funds dominate ranked net selling (≥40%) AND are net negative.
    NEUTRAL     → present but not dominant, or two-sided (net ~ 0).
    """
    if buy_ratio >= _DOMINANCE_THRESHOLD and net_smart_lot > 0:
        return "STRONG_BUY"
    if sell_ratio >= _DOMINANCE_THRESHOLD and net_smart_lot < 0:
        return "STRONG_SELL"
    return "NEUTRAL"


def _legacy_flow(smart_flow: str) -> str:
    return {
        "STRONG_BUY": "BUY",
        "STRONG_SELL": "SELL",
        "NEUTRAL": "NEUTRAL",
    }.get(smart_flow, "UNKNOWN")


def _comment(
    flow: str, buy_ratio: float, sell_ratio: float, net_smart_lot: float
) -> str:
    if flow == "STRONG_BUY":
        return (
            f"Akıllı para net alışta baskın (%{buy_ratio * 100:.0f}), "
            f"net {net_smart_lot:+,.0f} lot pozitif."
        )
    if flow == "STRONG_SELL":
        return (
            f"Akıllı para net satışta baskın (%{sell_ratio * 100:.0f}), "
            f"net {net_smart_lot:+,.0f} lot negatif."
        )
    return (
        f"Akıllı para baskın değil (alış %{buy_ratio * 100:.0f} / "
        f"satış %{sell_ratio * 100:.0f}, net {net_smart_lot:+,.0f} lot)."
    )


# ── Helpers ──────────────────────────────────────────────────────────────────────


def _clean_side(rows: Any) -> list[dict[str, Any]]:
    """Normalize a buyers/sellers list into ``[{name, value>0}]`` entries."""
    if not isinstance(rows, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("name") or "").strip()
        try:
            value = abs(float(row.get("value") or 0.0))
        except (TypeError, ValueError):
            continue
        if name and value > 0:
            cleaned.append({"name": name, "value": value})
    return cleaned


def _is_smart_money(name: str) -> bool:
    normalized = _normalize(name)
    return any(keyword in normalized for keyword in _SMART_MONEY_KEYWORDS)


_TURKISH_MAP = str.maketrans(
    {
        "İ": "i",
        "I": "i",
        "ı": "i",
        "Ş": "s",
        "ş": "s",
        "Ğ": "g",
        "ğ": "g",
        "Ü": "u",
        "ü": "u",
        "Ö": "o",
        "ö": "o",
        "Ç": "c",
        "ç": "c",
    }
)


def _normalize(text: str) -> str:
    """Lowercase + fold Turkish characters for robust substring matching.

    Turkish letters like dotless ``ı``, ``ş``, ``ğ`` do NOT decompose under
    Unicode NFKD, so they are mapped explicitly before a final accent strip
    handles any remaining diacritics.
    """
    folded = text.translate(_TURKISH_MAP).lower()
    decomposed = unicodedata.normalize("NFKD", folded)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _unknown_entry(symbol: str, comment: str) -> dict[str, Any]:
    """Safe default when no real AKD data is available for a symbol."""
    return {
        "symbol": symbol,
        "smartMoneyFlow": "UNKNOWN",
        "brokerFlow": "UNKNOWN",
        "netInstitutionalFlow": None,
        "smartBuyRatio": None,
        "smartSellRatio": None,
        "netSmartLot": None,
        "topBrokers": [],
        "comment": comment,
        "available": False,
        "period": "DAILY",
        "dataAgeSeconds": None,
        "asOf": None,
        "marketDate": None,
        "retrievedAt": datetime.now(UTC).isoformat(),
    }


def _data_age_seconds(raw: Any) -> float | None:
    if not raw:
        return None
    try:
        parsed = (
            raw
            if isinstance(raw, datetime)
            else datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        )
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return round(
            max(0.0, (datetime.now(UTC) - parsed.astimezone(UTC)).total_seconds()), 1
        )
    except (TypeError, ValueError):
        return None
