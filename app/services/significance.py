"""Önem dedektörü (v2 Faz 5) — "AI'ı sadece önemli değişiklikte çağır".

Portföydeki semboller 5 dakikada bir DETERMİNİSTİK olarak taranır (LLM'siz:
snapshot + göstergeler + DB'deki haber/KAP parmak izleri). Son GERÇEK AI
değerlendirmesinden bu yana anlamlı bir değişiklik yoksa LLM çağrısı atlanır.

Tetikleyiciler baseline'a (son AI çağrısındaki gözlem) göredir:
fiyat hareketi eşiği (admin: significancePriceMovePct, default %1.5;
pozisyonda 2/3'ü), konsensüs değişimi, haber/KAP parmak izi değişimi,
RSI 30/70 kesişimi, MACD histogram işaret değişimi, ADX 25 kesişimi, MOST
sinyal değişimi, derinlik dengesizliği bandı ([0.5, 2.0]) dışına çıkma/taraf
değişimi, fiyatın stop'a %1 yaklaşması, seans içi 4 saatten uzun AI'sız
kalma ve baseline yokluğu (restart).

Durum in-memory'dir; restart sonrası ilk tarama sembol başına bir kez AI
çağırır (kabul edilmiş maliyet). Dedektör asla exception fırlatmaz —
belirsizlikte "significant" döner (fail-open: AI çağrısı atlanmaz).
Stop-loss bekçisi bu modülden tamamen bağımsızdır.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger(__name__)

DEFAULT_PRICE_MOVE_PCT = Decimal("1.5")
#: Pozisyon varken fiyat eşiği bu çarpanla daraltılır (daha hassas izleme).
HELD_POSITION_THRESHOLD_FACTOR = Decimal("2") / Decimal("3")
RSI_LOW, RSI_HIGH = 30.0, 70.0
ADX_THRESHOLD = 25.0
DEPTH_BAND_LOW, DEPTH_BAND_HIGH = 0.5, 2.0
NEAR_STOP_PCT = Decimal("1.0")
STALENESS_BACKSTOP = timedelta(hours=4)
_EVENT_LOOKBACK = timedelta(hours=12)


@dataclass(frozen=True)
class SymbolObservation:
    symbol: str
    observed_at: datetime
    last_price: float | None = None
    consensus: str | None = None
    rsi: float | None = None
    macd_hist: float | None = None
    adx: float | None = None
    most_signal: str | None = None
    depth_imbalance: float | None = None
    news_fp: tuple[str, ...] = ()
    kap_fp: tuple[str, ...] = ()
    position_qty: float = 0.0
    active_stop: float | None = None


@dataclass(frozen=True)
class SignificanceResult:
    significant: bool
    triggers: tuple[str, ...]


def build_observation(
    symbol: str,
    payload: dict[str, Any],
    *,
    position_qty: float = 0.0,
    active_stop: float | None = None,
    news_fp: tuple[str, ...] = (),
    kap_fp: tuple[str, ...] = (),
) -> SymbolObservation:
    """Gateway snapshot payload'ından deterministik gözlem üret (LLM'siz)."""
    features = payload.get("technicalFeatures") or {}

    def _f(*keys: str) -> float | None:
        for key in keys:
            for source in (payload, features):
                value = source.get(key)
                if value is not None:
                    try:
                        return float(value)
                    except (TypeError, ValueError):
                        continue
        return None

    macd = _f("macd")
    macd_signal = _f("macdSignal")
    macd_hist = (macd - macd_signal) if macd is not None and macd_signal is not None else None

    bid_volume = _f("bidVolume")
    ask_volume = _f("askVolume")
    depth_imbalance = (
        bid_volume / ask_volume
        if bid_volume and ask_volume and ask_volume > 0
        else None
    )

    most_signal = features.get("mostSignal") or payload.get("mostSignal")
    consensus = features.get("indicatorConsensus") or payload.get("indicatorConsensus")

    return SymbolObservation(
        symbol=symbol.strip().upper(),
        observed_at=datetime.now(UTC),
        last_price=_f("lastPrice"),
        consensus=str(consensus).upper() if consensus else None,
        rsi=_f("rsi"),
        macd_hist=macd_hist,
        adx=_f("adx"),
        most_signal=str(most_signal).upper() if most_signal else None,
        depth_imbalance=depth_imbalance,
        news_fp=news_fp,
        kap_fp=kap_fp,
        position_qty=position_qty,
        active_stop=active_stop,
    )


async def load_event_fingerprints(
    session: AsyncSession, symbol: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """DB cache'inden haber/KAP parmak izleri (ucuz — gateway çağrısı yok)."""
    from app.models.db import KapEvent, NewsCache

    normalized = symbol.strip().upper()
    cutoff = datetime.now(UTC) - _EVENT_LOOKBACK
    news_rows = (
        await session.execute(
            select(NewsCache.title)
            .where(NewsCache.symbol == normalized, NewsCache.cached_at >= cutoff)
            .order_by(NewsCache.id.desc())
            .limit(20)
        )
    ).scalars()
    kap_rows = (
        await session.execute(
            select(KapEvent.title)
            .where(KapEvent.symbol == normalized, KapEvent.cached_at >= cutoff)
            .order_by(KapEvent.id.desc())
            .limit(20)
        )
    ).scalars()
    return tuple(sorted(set(news_rows))), tuple(sorted(set(kap_rows)))


class SignificanceDetector:
    """Sembol başına son-AI-çağrısı baseline'ını tutar ve yeni gözlemi ona
    göre puanlar. ``record_ai_evaluation`` YALNIZCA gerçek bir AI
    değerlendirmesinden sonra çağrılmalıdır — skip edilen taramalar
    baseline'ı değiştirmez."""

    def __init__(self) -> None:
        self._baseline: dict[str, SymbolObservation] = {}

    def reset(self) -> None:
        self._baseline.clear()

    def record_ai_evaluation(self, obs: SymbolObservation) -> None:
        self._baseline[obs.symbol] = obs

    def assess(
        self,
        obs: SymbolObservation,
        *,
        price_move_pct: Decimal | float = DEFAULT_PRICE_MOVE_PCT,
    ) -> SignificanceResult:
        try:
            return self._assess_inner(obs, Decimal(str(price_move_pct)))
        except Exception:  # noqa: BLE001 — belirsizlikte AI çağrısını atlama
            logger.exception("Significance assess failed symbol=%s", obs.symbol)
            return SignificanceResult(True, ("ASSESS_ERROR",))

    def _assess_inner(
        self, obs: SymbolObservation, threshold_pct: Decimal
    ) -> SignificanceResult:
        base = self._baseline.get(obs.symbol)
        if base is None:
            return SignificanceResult(True, ("NO_BASELINE",))

        triggers: list[str] = []

        if obs.position_qty > 0:
            threshold_pct = threshold_pct * HELD_POSITION_THRESHOLD_FACTOR

        if obs.last_price and base.last_price and base.last_price > 0:
            move_pct = (
                Decimal(str(abs(obs.last_price - base.last_price)))
                / Decimal(str(base.last_price))
                * 100
            )
            if move_pct >= threshold_pct:
                triggers.append(f"PRICE_MOVE_{move_pct:.2f}PCT")

        if (
            obs.consensus
            and base.consensus
            and obs.consensus != base.consensus
        ):
            triggers.append(f"CONSENSUS_FLIP_{base.consensus}_TO_{obs.consensus}")

        if obs.news_fp != base.news_fp:
            triggers.append("NEWS_CHANGED")
        if obs.kap_fp != base.kap_fp:
            triggers.append("KAP_CHANGED")

        if obs.rsi is not None and base.rsi is not None:
            for level, name in ((RSI_LOW, "30"), (RSI_HIGH, "70")):
                if (base.rsi < level) != (obs.rsi < level):
                    triggers.append(f"RSI_CROSS_{name}")

        if (
            obs.macd_hist is not None
            and base.macd_hist is not None
            and obs.macd_hist * base.macd_hist < 0
        ):
            triggers.append("MACD_HIST_SIGN_FLIP")

        if obs.adx is not None and base.adx is not None:
            if (base.adx < ADX_THRESHOLD) != (obs.adx < ADX_THRESHOLD):
                triggers.append("ADX_CROSS_25")

        if (
            obs.most_signal
            and base.most_signal
            and obs.most_signal != base.most_signal
        ):
            triggers.append(f"MOST_FLIP_{base.most_signal}_TO_{obs.most_signal}")

        if obs.depth_imbalance is not None and base.depth_imbalance is not None:
            base_in_band = DEPTH_BAND_LOW <= base.depth_imbalance <= DEPTH_BAND_HIGH
            now_in_band = DEPTH_BAND_LOW <= obs.depth_imbalance <= DEPTH_BAND_HIGH
            side_flip = (base.depth_imbalance - 1) * (obs.depth_imbalance - 1) < 0
            if (base_in_band and not now_in_band) or side_flip:
                triggers.append("DEPTH_IMBALANCE_SHIFT")

        if (
            obs.position_qty > 0
            and obs.active_stop
            and obs.last_price
            and obs.active_stop > 0
        ):
            distance_pct = (
                Decimal(str(obs.last_price - obs.active_stop))
                / Decimal(str(obs.active_stop))
                * 100
            )
            if distance_pct <= NEAR_STOP_PCT:
                triggers.append("NEAR_STOP")

        if obs.observed_at - base.observed_at > STALENESS_BACKSTOP:
            triggers.append("STALE_BASELINE_4H")

        return SignificanceResult(bool(triggers), tuple(triggers))


#: Modül seviyesinde paylaşılan dedektör — scanner portföy taraması kullanır.
significance_detector = SignificanceDetector()
