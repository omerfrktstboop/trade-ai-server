"""Deterministik Min5 giriş setup'ı (Plan Faz 1.1).

Kısa vadeli trade planının çekirdeği: fiyat seviyesi, setup kalitesi ve
uygulanabilirlik kararları **modelden bağımsız, deterministik** olmalıdır; AI
yalnızca hazır bir setup'ı bağlam açısından onaylayan bir veto katmanıdır
(bkz. plan bölüm 3-5).

Bu modül iki saf hesap sağlar:

1. ``compute_setup_score`` — ``SignalRequest``'in Matriks türevi teknik/derinlik
   alanlarından 0-100 arası deterministik bir setup skoru ve bileşen dökümü
   üretir (trend, momentum, katılım, uygulanabilirlik, tetikleyici).
2. ``compute_entry_levels`` — best-ask proxy'si, ATR ve ödül/risk oranından
   deterministik entry/stop/target üretir; stop mesafesi bant dışındaysa setup
   üretmez (plan bölüm 4).

Hiçbir şey emir göndermez, DB'ye yazmaz ya da canlı akışı değiştirmez.
Fonksiyonlar saf ve yan etkisizdir; eksik veri asla exception fırlatmaz,
ilgili bileşeni düşürür ve ``data_sufficient=False`` ile işaretlenir. Skoru
eşik/shortlist için tüketmek sonraki fazların (1.3/1.4) işidir.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.models.signal import SignalRequest

# Bileşen ağırlıkları (toplam 1.0). Hızlı intraday sistemde kötü spread/derinlik
# bir scalp'i doğrudan zarara çevirdiği için uygulanabilirlik trend/momentum
# kadar ağırlıklıdır. Katılım ve tetikleyici, payload'da bar geçmişi
# bulunmadığı için (yalnızca anlık gözlem) daha düşük ağırlıklı ve
# yaklaşıktır — bar tabanlı breakout tespiti ileride eklenecek.
_WEIGHTS = {
    "trend": 0.25,
    "momentum": 0.25,
    "participation": 0.15,
    "tradeability": 0.25,
    "trigger": 0.10,
}

_BULLISH_TOKENS = {"BUY", "BULL", "BULLISH", "UP", "STRONG_BUY", "LONG"}


def _is_bullish(token: str | None) -> bool:
    if not token:
        return False
    upper = str(token).upper()
    return any(t in upper for t in _BULLISH_TOKENS)


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


@dataclass(frozen=True)
class SetupScore:
    """Deterministik setup skoru ve bileşen dökümü."""

    total: float
    trend: float
    momentum: float
    participation: float
    tradeability: float
    trigger: float
    data_sufficient: bool
    components: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class EntryLevels:
    """Deterministik giriş/stop/hedef seviyeleri (tümü ham fiyat; BIST fiyat
    adımına yuvarlama, LIMIT emri zaten adım-zorunlu olan gateway'de uygulanır).
    """

    entry: Decimal
    stop_loss: Decimal
    target: Decimal
    risk_per_share: Decimal
    reward_per_share: Decimal
    reward_risk: float
    stop_distance_pct: float


def _score_trend(r: SignalRequest, c: dict[str, Any]) -> float:
    price, ema20, ema50 = r.last_price, r.ema20, r.ema50
    score = 0.0
    price_above_ema20 = price is not None and ema20 is not None and price > ema20
    ema_stacked = ema20 is not None and ema50 is not None and ema20 > ema50
    if price_above_ema20:
        score += 45
    if ema_stacked:
        score += 45
    if _is_bullish(r.symbol_trend_regime):
        score += 10
    c["priceAboveEma20"] = price_above_ema20
    c["emaStacked"] = ema_stacked
    c["symbolTrendRegime"] = r.symbol_trend_regime
    return _clamp(score)


def _score_momentum(r: SignalRequest, c: dict[str, Any]) -> float:
    score = 0.0
    macd_bullish = (
        r.macd is not None and r.macd_signal is not None and r.macd > r.macd_signal
    )
    if macd_bullish:
        score += 35
    rsi = r.rsi
    if rsi is not None:
        if 52 <= rsi <= 70:
            score += 30
        elif 70 < rsi <= 75:
            score += 12
        elif 45 <= rsi < 52:
            score += 8
        elif rsi > 75:
            score -= 20
    if _is_bullish(r.alpha_trend_signal):
        score += 20
    if _is_bullish(r.most_signal):
        score += 15
    c["macdBullish"] = macd_bullish
    c["rsi"] = rsi
    c["alphaTrendSignal"] = r.alpha_trend_signal
    c["mostSignal"] = r.most_signal
    return _clamp(score)


def _score_participation(r: SignalRequest, c: dict[str, Any]) -> float:
    score = 0.0
    bar_volume_ok = bool(r.bar_volume_reliable) and (r.bar_volume or 0) > 0
    obv_rising = r.obv_slope is not None and r.obv_slope > 0
    turnover_present = r.session_turnover_tl is not None and r.session_turnover_tl > 0
    if bar_volume_ok:
        score += 40
    if obv_rising:
        score += 30
    if turnover_present:
        score += 30
    c["barVolumeReliable"] = bar_volume_ok
    c["obvRising"] = obv_rising
    c["sessionTurnoverTl"] = r.session_turnover_tl
    return _clamp(score)


def _score_tradeability(r: SignalRequest, c: dict[str, Any]) -> float:
    score = 0.0
    spread = r.spread_pct
    if spread is not None:
        if spread <= 0.5:
            score += 35
        elif spread <= 1.0:
            score += 15
        else:
            score -= 15
    ratio = r.depth_bid_ask_ratio_top10
    if ratio is not None:
        if ratio >= 1.2:
            score += 30
        elif ratio >= 1.0:
            score += 15
        elif ratio < 0.8:
            score -= 15
    sell_pressure = r.depth_sell_pressure_score
    if sell_pressure is not None:
        # Yüksek satış baskısı skoru bir scalp girişini boğar.
        score += 15 if sell_pressure <= 0.3 else (-20 if sell_pressure >= 0.6 else 0)
    ask_wall = r.depth_nearest_ask_wall_distance_pct
    if ask_wall is not None and 0 <= ask_wall < 1.0:
        score -= 20
    c["spreadPct"] = spread
    c["depthBidAskRatioTop10"] = ratio
    c["depthSellPressureScore"] = sell_pressure
    c["nearestAskWallDistancePct"] = ask_wall
    return _clamp(score)


def _score_trigger(r: SignalRequest, c: dict[str, Any]) -> float:
    score = 0.0
    vwap_dist = r.vwap_distance_pct
    if vwap_dist is not None:
        # VWAP çevresinde pullback tatlı nokta; aşırı uzaklık kovalamadır.
        if -1.0 <= vwap_dist <= 1.0:
            score += 40
        elif 1.0 < vwap_dist <= 2.0:
            score += 15
    if _is_bullish(r.depth_order_book_signal):
        score += 30
    buy_pressure = r.depth_buy_pressure_score
    if buy_pressure is not None and buy_pressure >= 0.6:
        score += 30
    c["vwapDistancePct"] = vwap_dist
    c["depthOrderBookSignal"] = r.depth_order_book_signal
    c["depthBuyPressureScore"] = buy_pressure
    return _clamp(score)


def compute_setup_score(request: SignalRequest) -> SetupScore:
    """``request``'ten deterministik 0-100 setup skoru üret.

    Skor asla trade izni vermez; yalnızca bir sıralama/eşik girdisidir. Çekirdek
    teknik veri (güvenilir quote+OHLC, EMA20/50, MACD) eksikse ``data_sufficient``
    ``False`` döner ve çağıran skora göre işlem yapmamalıdır.
    """
    components: dict[str, Any] = {}
    trend = _score_trend(request, components)
    momentum = _score_momentum(request, components)
    participation = _score_participation(request, components)
    tradeability = _score_tradeability(request, components)
    trigger = _score_trigger(request, components)

    total = _clamp(
        trend * _WEIGHTS["trend"]
        + momentum * _WEIGHTS["momentum"]
        + participation * _WEIGHTS["participation"]
        + tradeability * _WEIGHTS["tradeability"]
        + trigger * _WEIGHTS["trigger"]
    )

    data_sufficient = bool(
        request.quote_reliable
        and request.ohlc_reliable
        and request.ema20 is not None
        and request.ema50 is not None
        and request.macd is not None
    )
    components["dataSufficient"] = data_sufficient

    return SetupScore(
        total=round(total, 2),
        trend=round(trend, 2),
        momentum=round(momentum, 2),
        participation=round(participation, 2),
        tradeability=round(tradeability, 2),
        trigger=round(trigger, 2),
        data_sufficient=data_sufficient,
        components=components,
    )


def compute_entry_levels(
    request: SignalRequest,
    *,
    atr_multiplier: float = 1.1,
    reward_risk: float = 1.8,
    min_stop_pct: float = 0.6,
    max_stop_pct: float = 1.5,
) -> EntryLevels | None:
    """ATR ve ödül/risk oranından deterministik entry/stop/target üret.

    - entry: son fiyattan yarım-spread yukarı (best-ask proxy'si).
    - stop:  entry − ``atr_multiplier`` × ATR. Elde edilen stop mesafesi
             ``min_stop_pct`` altında kalırsa gürültü için tabana genişletilir;
             ``max_stop_pct`` üstündeyse sembol o an fazla oynak kabul edilip
             setup üretilmez (None) — plan bölüm 4.
    - target: entry + ``reward_risk`` × risk.

    Gerekli veri (pozitif fiyat ve ATR) yoksa ``None`` döner; asla exception
    fırlatmaz. Fiyat adımına yuvarlama burada yapılmaz (gateway LIMIT emrinde
    zorunlu kılar).
    """
    price = request.last_price
    atr = request.atr
    if price is None or price <= 0 or atr is None or atr <= 0:
        return None

    price_d = Decimal(str(price))
    half_spread = Decimal("0")
    if request.spread_pct is not None and request.spread_pct > 0:
        half_spread = price_d * Decimal(str(request.spread_pct)) / Decimal("200")
    entry = price_d + half_spread

    stop_distance = Decimal(str(atr_multiplier)) * Decimal(str(atr))
    min_dist = entry * Decimal(str(min_stop_pct)) / Decimal("100")
    max_dist = entry * Decimal(str(max_stop_pct)) / Decimal("100")
    if stop_distance > max_dist:
        return None
    if stop_distance < min_dist:
        stop_distance = min_dist

    stop_loss = entry - stop_distance
    if stop_loss <= 0:
        return None
    risk = entry - stop_loss
    reward = Decimal(str(reward_risk)) * risk
    target = entry + reward
    stop_distance_pct = float(risk / entry * Decimal("100"))

    return EntryLevels(
        entry=entry,
        stop_loss=stop_loss,
        target=target,
        risk_per_share=risk,
        reward_per_share=reward,
        reward_risk=round(float(reward / risk), 4),
        stop_distance_pct=round(stop_distance_pct, 4),
    )
