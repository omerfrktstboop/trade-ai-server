"""Compact, provider-neutral contract for AI market decisions.

This module deliberately models only information that can help an AI make a
market decision.  Provider request builders must serialize this model instead
of forwarding the gateway's full snapshot.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, FiniteFloat


class _ContextModel(BaseModel):
    """Base settings shared by all compact decision-context models."""

    model_config = ConfigDict(extra="forbid")


class PeriodContext(_ContextModel):
    requested: str | None = None
    actual: str | None = None
    mismatch: bool | None = None


class PriceContext(_ContextModel):
    last: FiniteFloat | None = None
    open: FiniteFloat | None = None
    high: FiniteFloat | None = None
    low: FiniteFloat | None = None
    close: FiniteFloat | None = None


class MarketContext(_ContextModel):
    barVolume: FiniteFloat | None = None
    sessionTurnoverTl: FiniteFloat | None = None
    macroMarketRegime: str | None = None
    symbolTrendRegime: str | None = None


class TechnicalContext(_ContextModel):
    rsi: FiniteFloat | None = None
    ema20: FiniteFloat | None = None
    ema50: FiniteFloat | None = None
    macd: FiniteFloat | None = None
    macdSignal: FiniteFloat | None = None
    atr: FiniteFloat | None = None
    natr: FiniteFloat | None = None
    adx: FiniteFloat | None = None
    most: FiniteFloat | None = None
    mostSignal: str | None = None
    obvSlope: FiniteFloat | None = None
    vwapDistancePct: FiniteFloat | None = None
    alphaTrendSignal: str | None = None
    indicatorConsensus: str | None = None
    indicatorConsensusRatio: FiniteFloat | None = None
    indicatorBuyCount: int | None = Field(default=None, ge=0, le=1_000)
    indicatorSellCount: int | None = Field(default=None, ge=0, le=1_000)
    indicatorNeutralCount: int | None = Field(default=None, ge=0, le=1_000)


class DataQualityContext(_ContextModel):
    quoteAgeSec: FiniteFloat | None = Field(default=None, ge=0)
    ohlcvAgeSec: FiniteFloat | None = Field(default=None, ge=0)
    depthAgeSec: FiniteFloat | None = Field(default=None, ge=0)
    quoteReliable: bool | None = None
    ohlcReliable: bool | None = None
    quoteFresh: bool | None = None
    ohlcvFresh: bool | None = None
    depthFresh: bool | None = None


class DepthContext(_ContextModel):
    reliable: bool
    spreadPct: FiniteFloat | None = Field(default=None, ge=0)
    buyPressure: FiniteFloat | None = Field(default=None, ge=0, le=1)
    signal: str | None = None
    bidAskRatio: FiniteFloat | None = Field(default=None, ge=0)
    nearestBidWallDistancePct: FiniteFloat | None = Field(default=None, le=0)
    nearestAskWallDistancePct: FiniteFloat | None = Field(default=None, ge=0)
    wallConcentrationRisk: bool | None = None


class NewsItemContext(_ContextModel):
    headline: str = Field(min_length=1, max_length=180)
    summary: str | None = Field(default=None, max_length=320)
    sentiment: Literal["POSITIVE", "NEGATIVE", "NEUTRAL", "MIXED", "UNKNOWN"] | None = (
        None
    )


class NewsContext(_ContextModel):
    items: list[NewsItemContext] = Field(default_factory=list, max_length=2)
    negativeRisk: bool | None = None


class BrokerFlowContext(_ContextModel):
    smartMoneyFlow: str | None = None
    netSmartLot: FiniteFloat | None = None
    topBuyer: str | None = Field(default=None, max_length=120)
    topSeller: str | None = Field(default=None, max_length=120)


class KapContext(_ContextModel):
    blockingRisk: bool | None = None
    activeRiskCount: int | None = Field(default=None, ge=0)
    unknownDateRisk: bool | None = None
    summary: str | None = Field(default=None, max_length=1_000)


class EventsContext(_ContextModel):
    news: NewsContext | None = None
    brokerFlow: BrokerFlowContext | None = None
    kap: KapContext | None = None


_ResearchSource = Annotated[str, Field(min_length=1, max_length=48)]


class ResearchTrendContext(_ContextModel):
    changePct30m: FiniteFloat | None = None
    changePct60m: FiniteFloat | None = None
    changePctDaily: FiniteFloat | None = None
    relativeVolume: FiniteFloat | None = Field(default=None, ge=0)
    volumeTl: FiniteFloat | None = Field(default=None, ge=0)
    ema20Slope: FiniteFloat | None = None


class ResearchEvidenceContext(_ContextModel):
    trendPreScore: FiniteFloat | None = Field(default=None, ge=0, le=100)
    candidateSource: list[_ResearchSource] | None = Field(default=None, max_length=4)
    recentTrend: ResearchTrendContext | None = None


class PositionContext(_ContextModel):
    botQty: FiniteFloat = Field(ge=0)
    botAvgCost: FiniteFloat | None = Field(default=None, ge=0)
    unrealizedPnlPct: FiniteFloat | None = None
    lockedLongTerm: bool | None = None


class AiDecisionContext(_ContextModel):
    """The complete compact context accepted by every AI provider.

    It intentionally has no raw payload, URLs, timestamps, runtime config, or
    agentic workflow fields.  Optional context sections distinguish unavailable
    information (``None``) from a known zero-valued measurement.
    """

    schemaVersion: Literal["ai-decision-context-v2"] = "ai-decision-context-v2"
    symbol: str = Field(min_length=1)
    period: PeriodContext
    profile: str | None = None
    evaluationPurpose: str
    dataQuality: DataQualityContext
    price: PriceContext
    market: MarketContext
    technical: TechnicalContext
    depth: DepthContext | None = None
    position: PositionContext | None = None
    events: EventsContext | None = None
    research: ResearchEvidenceContext | None = None
    # Deterministik entry akışında (Plan Faz 1.4) sunucunun hesapladığı setup
    # skoru + seviyeler; AI bunu veto/onay için görür. Diğer akışlarda None.
    deterministicSetup: dict | None = None
