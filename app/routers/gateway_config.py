"""Server-authoritative runtime configuration for the Matriks gateway.

Fail-closed guarantees enforced here (the gateway re-checks its own hard
limits on top):

- ``mode`` is downgraded to ``PAPER`` when the active trade profile does not
  allow the configured ``botMode`` (e.g. ``REAL_LIVE`` while the profile has
  ``allow_real_live=False``). A misconfigured admin panel can never leak a
  live mode to the gateway past its profile.
- ``configHash`` fingerprints the full response so the gateway (and tests)
  can cheaply detect "did anything change?" across polls.
"""

import hashlib
import json
import math
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy import select

from app.config import settings
from app.core.auth import verify_gateway_token
from app.db.session import async_session_factory
from app.models.db import (
    BotPosition,
    LockedPosition,
    ResearchCandidate,
    TradeWatchlistSymbol,
)
from app.services.admin_config import list_admin_configs
from app.services.daily_trade_count import get_today_order_count_maps
from app.services.trade_profile import get_active_profile

router = APIRouter(tags=["Gateway"], dependencies=[Depends(verify_gateway_token)])

_LIVE_REAL_MODES = {"REAL_LIVE"}
_LIVE_DEMO_MODES = {"DEMO_LIVE"}


def _is_index_symbol(symbol: str, configured_market_index: str) -> bool:
    normalized = symbol.strip().upper()
    return bool(
        normalized
        and (
            normalized == configured_market_index
            or (
                len(normalized) >= 4
                and normalized.startswith("X")
                and normalized.isalnum()
            )
        )
    )


def _effective_mode(
    bot_mode: str, profile, *, real_live_mode_allowed: bool, real_live_armed: bool
) -> str:
    """Downgrade the configured mode to PAPER when the profile disallows it."""
    mode = (bot_mode or "PAPER").strip().upper()
    # LIVE is deliberately never an alias for REAL_LIVE.  Keeping an
    # ambiguous historical value fail-closed avoids an accidental real route.
    if mode == "LIVE":
        return "PAPER"
    if mode in _LIVE_REAL_MODES and not (
        profile.allow_real_live and real_live_mode_allowed and real_live_armed
    ):
        return "PAPER"
    if mode in _LIVE_DEMO_MODES and not profile.allow_demo_live:
        return "PAPER"
    return mode


@router.get("/gateway/config")
async def gateway_runtime_config() -> dict:
    """Return the complete fail-closed configuration consumed by Matriks."""
    async with async_session_factory() as session:
        values = {item.key: item.value for item in await list_admin_configs(session)}
        profile = await get_active_profile(session)
        portfolio = (await session.execute(select(BotPosition))).scalars().all()
        locked = (await session.execute(select(LockedPosition))).scalars().all()
        research_candidates = (
            (
                await session.execute(
                    select(ResearchCandidate.symbol)
                    .where(
                        ResearchCandidate.status.in_(
                            ("DETECTED", "RESEARCH_PENDING", "RESEARCHED", "QUALIFIED")
                        ),
                        (ResearchCandidate.expires_at.is_(None))
                        | (ResearchCandidate.expires_at >= datetime.now(UTC)),
                    )
                    .order_by(
                        ResearchCandidate.trend_pre_score.desc(),
                        ResearchCandidate.relative_volume.desc(),
                        ResearchCandidate.volume_tl.desc(),
                    )
                    .limit(max(1, int(values["maxActiveResearchSymbols"])))
                )
            )
            .scalars()
            .all()
        )
        trade_watchlist = (
            (
                await session.execute(
                    select(TradeWatchlistSymbol.symbol).where(
                        TradeWatchlistSymbol.is_active.is_(True),
                        (TradeWatchlistSymbol.expires_at.is_(None))
                        | (TradeWatchlistSymbol.expires_at >= datetime.now(UTC)),
                    )
                )
            )
            .scalars()
            .all()
        )
        daily_counts = await get_today_order_count_maps(session)

    manually_allowed = {
        value.strip().upper()
        for value in values["allowedSymbols"].split(",")
        if value.strip()
    }
    symbols = set(manually_allowed)
    market_index_symbol = settings.market_index_symbol.strip().upper()
    configured_buy_symbols = {
        s.strip().upper()
        for s in values["buyAllowedSymbols"].split(",")
        if s.strip() and not _is_index_symbol(s, market_index_symbol)
    }
    sell_symbols = [
        s.strip().upper()
        for s in values["sellExitAllowedSymbols"].split(",")
        if s.strip() and not _is_index_symbol(s, market_index_symbol)
    ]
    decline_symbols = [
        s.strip().upper()
        for s in values.get("declineSymbols", "").split(",")
        if s.strip()
    ]
    eligible_symbols = {
        str(symbol).strip().upper()
        for symbol in trade_watchlist
        if str(symbol).strip()
        and not _is_index_symbol(str(symbol), market_index_symbol)
    }
    effective_buy_symbols = eligible_symbols.difference(decline_symbols)
    if manually_allowed:
        effective_buy_symbols.intersection_update(manually_allowed)
    if configured_buy_symbols:
        effective_buy_symbols.intersection_update(configured_buy_symbols)
    symbols.update(row.symbol.strip().upper() for row in portfolio if row.qty > 0)
    # Data-only subscriptions never enter ``buyAllowedSymbols`` unless a
    # separate active trade-watchlist row exists.  RiskEngine and scanner
    # independently repeat that DB-backed eligibility check.
    #   - Makro filtre endeksi (XU100)
    #   - Discovery keşif evreni (movers ranking'i genişletir)
    #   - Aktif watchlist adayları (scanner analizi için snapshot gerekir)
    if market_index_symbol:
        symbols.add(market_index_symbol)
    symbols.update(
        s.strip().upper()
        for s in values["scanUniverseSymbols"].split(",")
        if s.strip()
    )
    symbols.update(str(s).strip().upper() for s in research_candidates)
    symbols.update(eligible_symbols)
    instrument_types = {
        symbol: (
            "INDEX"
            if _is_index_symbol(symbol, market_index_symbol)
            else "EQUITY"
        )
        for symbol in symbols
    }
    locked_qty: dict[str, float] = {}
    for row in locked:
        symbol = row.symbol.strip().upper()
        qty = float(row.qty)
        if not math.isfinite(qty) or qty < 0:
            raise ValueError(f"Invalid locked quantity for {symbol}")
        locked_qty[symbol] = locked_qty.get(symbol, 0.0) + qty
    bot_owned_qty = {
        row.symbol.strip().upper(): float(row.qty) for row in portfolio if row.qty > 0
    }

    config = {
        "ok": True,
        "symbols": sorted(symbols),
        "subscriptionSymbols": sorted(symbols),
        "marketIndexSymbol": market_index_symbol or None,
        "instrumentTypes": instrument_types,
        "buyAllowedSymbols": sorted(effective_buy_symbols),
        "tradeEligibleSymbols": sorted(eligible_symbols),
        "sellExitAllowedSymbols": sorted(set(sell_symbols)),
        "declineSymbols": sorted(set(decline_symbols)),
        "tradingKillSwitchActive": values["tradingKillSwitchActive"] == "true"
        or values["killSwitchEnabled"] == "true",
        "forceSafeMode": values["forceSafeMode"] == "true",
        "lockedLongTermQty": locked_qty,
        "botOwnedQty": bot_owned_qty,
        "dailyCounterDate": datetime.now().date().isoformat(),
        "dailyAcceptedOrderCountsBySymbol": daily_counts.accepted_by_symbol,
        "dailyFilledOrderCountsBySymbol": daily_counts.filled_by_symbol,
        "dailyReservedOrderCountsBySymbol": daily_counts.reserved_or_sent_by_symbol,
        "mode": _effective_mode(
            values["botMode"],
            profile,
            real_live_mode_allowed=values["botRealLiveModeAllowed"] == "true",
            real_live_armed=values["botRealLiveArmed"] == "true",
        ),
        "enableDemoOrders": values["botEnableDemoOrders"] == "true",
        "enableRealOrders": values["botEnableRealOrders"] == "true",
        "realLiveModeAllowed": values["botRealLiveModeAllowed"] == "true",
        "realLiveArmed": values["botRealLiveArmed"] == "true",
        "requireDemoAccount": values["botRequireDemoAccount"] == "true",
        "demoAccountConfirmed": values["botDemoAccountConfirmed"] == "true",
        "maxOrderValueTl": profile.max_order_value_tl,
        "maxQtyPerOrder": profile.max_qty_per_order,
        "maxOrdersPerDay": profile.max_orders_per_day,
        "maxOrdersPerSymbolPerDay": profile.max_orders_per_symbol_per_day,
        "orderTimeInForce": profile.order_time_in_force,
        "indicatorPeriod": profile.indicator_period,
        "marketDataDiagnosticsEnabled": (
            values["marketDataDiagnosticsEnabled"] == "true"
        ),
        "marketDataDiagnosticSampleRatePct": float(
            values["marketDataDiagnosticSampleRatePct"]
        ),
        "marketDataWarningRateLimitSeconds": int(
            values["marketDataWarningRateLimitSeconds"]
        ),
        "scanIntervalMinutes": profile.scan_interval_minutes,
        # Matriks-side news aboneliği ayarları (algo panelinde parametre değil).
        "newsKeywordsCsv": settings.news_keywords_csv,
        "newsSymbolKeywordRulesCsv": settings.news_symbol_keyword_rules_csv,
        "newsFiltersOnlyInHeaders": settings.news_filters_only_in_headers,
        "newsFiltersExactMatch": settings.news_filters_exact_match,
        "profileCode": profile.code,
        "activeTradeProfile": {
            "code": profile.code,
            "name": profile.name,
            "riskLevel": profile.risk_level,
        },
    }
    config["configHash"] = hashlib.sha256(
        json.dumps(config, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:16]
    return config
