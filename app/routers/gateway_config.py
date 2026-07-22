"""Server-authoritative runtime configuration for the Matriks gateway.

Fail-closed guarantees enforced here (the gateway re-checks its own hard
limits on top):

- v2: ``systemMode`` (OBSERVE_ONLY/AUTO_TRADE), ``realAccountArmed`` ve
  ``armedAccountRef`` gönderilir; C# CheckDispatchGates bunları + accountType
  ile emir yetkisini belirler. Eski mode/DEMO_LIVE/REAL_LIVE downgrade mantığı
  kaldırıldı. ``contractVersion=3`` uyuşmazlığında iki taraf da fail-closed.
- ``configHash`` fingerprints the full response so the gateway (and tests)
  can cheaply detect "did anything change?" across polls.
"""

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
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
from app.services.bot_ownership import load_bot_ownership
from app.services.daily_trade_count import get_today_order_count_maps
from app.services.effective_risk_config import resolve_effective_risk_config
from app.services.trade_profile import get_active_profile

router = APIRouter(tags=["Gateway"], dependencies=[Depends(verify_gateway_token)])

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


# v2: _effective_mode ve eski mod downgrade mantığı kaldırıldı. Gateway artık
# yalnızca systemMode + accountType + REAL arming kullanır.


@router.get("/gateway/config")
async def gateway_runtime_config(
    account_ref: Annotated[str | None, Query(alias="accountRef")] = None,
) -> dict:
    """Return the complete fail-closed configuration consumed by Matriks."""
    async with async_session_factory() as session:
        values = {item.key: item.value for item in await list_admin_configs(session)}
        profile = await get_active_profile(session)
        effective_limits = await resolve_effective_risk_config(session)
        portfolio = (await session.execute(select(BotPosition))).scalars().all()
        normalized_account_ref = str(account_ref or "").strip()
        ownership = (
            await load_bot_ownership(session, normalized_account_ref)
            if len(normalized_account_ref) == 64
            else None
        )
        locked = (await session.execute(select(LockedPosition))).scalars().all()
        research_candidates = (
            (
                await session.execute(
                    select(ResearchCandidate.symbol)
                    .where(
                        ResearchCandidate.status.in_(
                            ("DETECTED", "RESEARCH_PENDING", "RESEARCHED", "QUALIFIED")
                        ),
                        ResearchCandidate.expires_at >= datetime.now(UTC),
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
    # BUY yetkisi tek kaynak: aktif Trade Watchlist (research pipeline onayı)
    # eksi kara liste, ayrıca manuel allowedSymbols filtresiyle daraltılır.
    # Eski ``buyAllowedSymbols`` üçüncü kesişim kaynağıydı; güncellenmesi
    # unutulduğunda sessizce izin listesini daraltıyordu, kaldırıldı.
    effective_buy_symbols = eligible_symbols.difference(decline_symbols)
    if manually_allowed:
        effective_buy_symbols.intersection_update(manually_allowed)
    symbols.update(row.symbol.strip().upper() for row in portfolio if row.qty > 0)
    if ownership is not None:
        symbols.update(ownership.quantities)
    # Data-only subscriptions never enter ``buyAllowedSymbols`` unless a
    # separate active trade-watchlist row exists.  RiskEngine and scanner
    # independently repeat that DB-backed eligibility check.
    #   - Makro filtre endeksi (XU100)
    #   - Discovery keşif evreni (movers ranking'i genişletir)
    #   - Aktif watchlist adayları (scanner analizi için snapshot gerekir)
    if market_index_symbol:
        symbols.add(market_index_symbol)
    symbols.update(
        s.strip().upper() for s in values["scanUniverseSymbols"].split(",") if s.strip()
    )
    symbols.update(str(s).strip().upper() for s in research_candidates)
    symbols.update(eligible_symbols)
    instrument_types = {
        symbol: ("INDEX" if _is_index_symbol(symbol, market_index_symbol) else "EQUITY")
        for symbol in symbols
    }
    locked_qty: dict[str, float] = {}
    for row in locked:
        symbol = row.symbol.strip().upper()
        qty = float(row.qty)
        if not math.isfinite(qty) or qty < 0:
            raise ValueError(f"Invalid locked quantity for {symbol}")
        locked_qty[symbol] = locked_qty.get(symbol, 0.0) + qty
    bot_owned_qty = (
        {symbol: float(qty) for symbol, qty in ownership.quantities.items()}
        if ownership is not None
        else {}
    )

    config = {
        "ok": True,
        # v3 kontrat sürümü: gateway ExpectedContractVersion=3 ile
        # karşılaştırır; uyuşmazlık (alan eksikliği dahil) emir yolunu iki
        # tarafta da fail-closed kapatır. Python ve C# yalnızca atomik deploy
        # ile birlikte yükseltilir.
        "contractVersion": 3,
        "symbols": sorted(symbols),
        "subscriptionSymbols": sorted(symbols),
        "marketIndexSymbol": market_index_symbol or None,
        "instrumentTypes": instrument_types,
        "buyAllowedSymbols": sorted(effective_buy_symbols),
        "tradeEligibleSymbols": sorted(eligible_symbols),
        "sellExitAllowedSymbols": sorted(set(sell_symbols)),
        "declineSymbols": sorted(set(decline_symbols)),
        # v2: tek kill switch. Eski tradingKillSwitchActive/forceSafeMode kaldırıldı.
        "killSwitchActive": values["killSwitchEnabled"] == "true",
        "lockedLongTermQty": locked_qty,
        "botOwnedQty": bot_owned_qty,
        "botOwnershipAccountRef": normalized_account_ref
        if ownership is not None
        else None,
        "dailyCounterDate": datetime.now().date().isoformat(),
        "dailyAcceptedOrderCountsBySymbol": daily_counts.accepted_by_symbol,
        "dailyFilledOrderCountsBySymbol": daily_counts.filled_by_symbol,
        "dailyReservedOrderCountsBySymbol": daily_counts.reserved_or_sent_by_symbol,
        # v2 mod/arming kontratı (C# CheckDispatchGates bunları okur; eksik
        # gönderilirse gateway fail-closed OBSERVE_ONLY'ye düşer). Eski
        # mode/enableDemoOrders/enableRealOrders/realLive*/requireDemoAccount/
        # Eski DEMO mod alanları kaldırıldı — DEMO/REAL artık gateway'in
        # tespit ettiği accountType'tır.
        "systemMode": (
            "AUTO_TRADE"
            if values.get("systemMode", "OBSERVE_ONLY").strip().upper() == "AUTO_TRADE"
            else "OBSERVE_ONLY"
        ),
        "realAccountArmed": values.get("realAccountArmed", "false") == "true",
        "armedAccountRef": (values.get("armedAccountRef", "") or "").strip(),
        "maxOrderValueTl": float(effective_limits.max_order_value_tl),
        "maxQtyPerOrder": effective_limits.max_qty_per_order,
        "maxOrdersPerDay": profile.max_orders_per_day,
        "maxOrdersPerSymbolPerDay": profile.max_orders_per_symbol_per_day,
        "maxDepthQueueDropPctForBuy": profile.max_depth_queue_drop_pct_for_buy,
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
