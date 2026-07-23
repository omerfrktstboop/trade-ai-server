"""The evaluator's orchestration pipeline: evaluate_symbol and the
runtime-control steps (kill switch, daily trade count, trade eligibility,
account sizing context) that wrap it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from app.config import settings
from app.core.logger import log_signal_evaluation
from app.core.risk_config import risk_config
from app.db.session import async_session_factory
from sqlalchemy import select

from app.models.db import TradeWatchlistSymbol
from app.models.signal import (
    EntryRange,
    OrderType,
    SignalAction,
    SignalRequest,
    SignalResponse,
)
from app.services.ai_provider import AiProvider, get_default_provider
from app.services.admin_config import (
    build_runtime_risk_config,
    is_demo_downtrend_buy_enabled,
    is_kill_switch_enabled,
)
from app.services.broker_flow_service import get_broker_flow_context
from app.services.bot_ownership import load_bot_ownership
from app.services.account_context import (
    MatriksAccountContextAdapter,
    fetch_fresh_account_inputs,
    get_account_reservation_handling,
)
from app.services.ai_call_gate import (
    compute_setup_fingerprint,
    resolve_bar_key,
    try_claim_ai_call,
)
from app.services.block_reason_classifier import classify_block_reason
from app.services.cash_reservation import calculate_backend_reserved_cash
from app.services.entry_setup import compute_entry_levels, compute_setup_score
from app.services.daily_trade_count import get_today_trade_counts
from app.services.decision_gate import (
    decision_context_fingerprint,
    preflight_wait_reason,
)
from app.services.fundamentals_service import get_fundamentals_context
from app.services.effective_risk_config import (
    EffectiveRiskConfig,
    EffectiveRiskConfigResolver,
    EnvironmentRiskLimits,
    SystemRiskConfig,
    resolve_effective_risk_config,
)
from app.services.evaluation.parsing import _safe_float
from app.services.evaluation.payload import (
    _build_position_context,
    _build_request_id,
    _snapshot_step,
    build_ai_decision_context,
    build_payload,
    snapshot_to_signal_request,
)
from app.services.evaluation.persistence import (
    _json_safe,
    dict_to_risk_decision,
    persist_evaluation,
    persist_sizing_audit,
)
from app.services.market_observation import record_market_observation_standalone
from app.services.market_regime import get_index_regime
from app.services.matriks_gateway import (
    GatewayError,
    MatriksGatewayClient,
    gateway_client,
)
from app.services.news_service import get_news_context
from app.services.kap_service import get_kap_context
from app.services.risk_engine import RiskEngine
from app.services.position_sizing import (
    AccountSizingContext,
    PositionSizingResult,
    TradeSizingContext,
)
from app.services.signal_override import consume_override, override_to_raw_decision
from app.services.trade_profile import get_active_profile, get_static_default_profile

logger = logging.getLogger(__name__)


# Related symbols whose depth is fetched while evaluating the root symbol.
# The rule remains here after removal of the old agent planner.
RELATED_SYMBOLS: dict[str, str] = {
    "ANELE": "THYAO",
    "PGSUS": "THYAO",
    "TUPRS": "KCHOL",
}


_static_effective_config = EffectiveRiskConfigResolver().resolve(
    environment_limits=EnvironmentRiskLimits.from_environment(),
    system_config=SystemRiskConfig(),
    trade_profile=get_static_default_profile(),
)
_static_risk_engine = RiskEngine(risk_config, _static_effective_config)


@dataclass(frozen=True)
class EvaluationResult:
    """Final decision after all runtime controls.

    v2: mod kavramı kaldırıldı. Emrin dispatch edilebilir olup olmadığı
    ``dispatch_eligible`` ile belirtilir (yalnızca TRADING amaçlı, research
    olmayan değerlendirmeler emre dönüşebilir). Gerçek dispatch kararı ayrıca
    scanner'da systemMode=AUTO_TRADE + account watcher + audit + risk
    kapılarından geçer.
    """

    response: SignalResponse
    dispatch_eligible: bool = False
    decision_created_utc: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    evaluation_purpose: str = "TRADING"
    research_score: float | None = None
    opportunity_score: float | None = None
    setup_score: float | None = None
    target_allocation_pct: float | None = None
    decision_entry_price: Decimal | None = None
    decision_target_price: Decimal | None = None
    sizing_binding_limits: tuple[str, ...] = ()
    sizing_account: AccountSizingContext | None = None
    sizing_trade: TradeSizingContext | None = None
    sizing_result: PositionSizingResult | None = None
    effective_limits: EffectiveRiskConfig | None = None
    rotation_eligible: bool = False
    raw_action: SignalAction | None = None
    # "llm" | "preflight-gate" | "admin-override" | "system-gate" | None.
    # Significance dedektörünün baseline'ı YALNIZCA "llm" iken güncellenir
    # (Fix #6): kapı WAIT'i veya admin override baseline oluşturmamalı.
    decision_source: str | None = None


async def with_runtime_controls(
    req: SignalRequest,
) -> tuple[SignalRequest, RiskEngine, bool, bool]:
    """Apply DB-backed runtime config controls when available."""
    try:
        async with async_session_factory() as session:
            runtime_config = await build_runtime_risk_config(session)
            kill_switch_enabled = await is_kill_switch_enabled(session)
            demo_allow_downtrend_buy = await is_demo_downtrend_buy_enabled(session)
            effective_config = await resolve_effective_risk_config(session)
    except Exception:
        logger.exception(
            "Failed to load runtime admin config request_id=%s symbol=%s",
            req.request_id,
            req.symbol,
        )
        return req, _static_risk_engine, False, False

    # v2: eski tradingMode override kaldırıldı. Emir yetkisi mod-bağımsızdır.
    return (
        req,
        RiskEngine(runtime_config, effective_config),
        kill_switch_enabled,
        demo_allow_downtrend_buy,
    )


def kill_switch_response(req: SignalRequest) -> SignalResponse:
    return SignalResponse(
        requestId=req.request_id,
        symbol=req.symbol,
        action=SignalAction.WAIT,
        qty=0.0,
        orderType=OrderType.NONE,
        price=None,
        confidenceScore=0.0,
        riskScore=0.0,
        allowOrder=False,
        reason="Kill switch enabled: trading disabled by admin",
        entryRange=None,
        stopLoss=None,
        targetPrice=None,
    )


async def with_resolved_daily_trade_count(req: SignalRequest) -> SignalRequest:
    """Fill dailyTradeCount from DB only when the caller omitted it."""
    if _has_explicit_daily_trade_count(req):
        return req

    try:
        async with async_session_factory() as session:
            counts = await get_today_trade_counts(session, req.symbol)
    except Exception:
        logger.exception(
            "Failed to resolve daily trade count from DB request_id=%s symbol=%s",
            req.request_id,
            req.symbol,
        )
        return req

    logger.info(
        "Resolved daily trade count from DB symbol=%s symbol_count=%s bot_count=%s effective=%s",
        counts.symbol,
        counts.symbol_count,
        counts.bot_count,
        counts.effective_count,
    )
    return req.model_copy(
        update={
            # Legacy risk-engine input remains the conservative de-duplicated
            # request count. Explicit v2 fields state what they actually count.
            "daily_trade_count": counts.effective_count,
            "daily_accepted_order_count": counts.symbol_accepted_order_count,
            "daily_filled_order_count": counts.symbol_filled_order_count,
        }
    )


async def with_trade_eligibility(req: SignalRequest) -> SignalRequest:
    """Resolve the DB-backed BUY gate; any DB problem remains fail-closed."""
    try:
        async with async_session_factory() as session:
            now = datetime.now(timezone.utc)
            eligible = (
                await session.execute(
                    select(TradeWatchlistSymbol.id).where(
                        TradeWatchlistSymbol.symbol == req.symbol.strip().upper(),
                        TradeWatchlistSymbol.is_active.is_(True),
                        (TradeWatchlistSymbol.expires_at.is_(None))
                        | (TradeWatchlistSymbol.expires_at >= now),
                    )
                )
            ).scalar_one_or_none()
    except Exception:
        logger.exception(
            "Trade eligibility unavailable; BUY remains blocked request_id=%s symbol=%s",
            req.request_id,
            req.symbol,
        )
        eligible = None
    return req.model_copy(update={"trade_eligible": eligible is not None})


async def with_fresh_account_sizing_context(
    req: SignalRequest,
    *,
    gateway: MatriksGatewayClient,
    snapshot: dict[str, Any],
    runtime_engine: RiskEngine,
) -> tuple[SignalRequest, str | None]:
    """Attach normalized account data for an AI BUY, otherwise fail closed."""
    req = req.model_copy(update={"account_sizing_context": None})
    effective = runtime_engine.effective_config
    if effective is None:
        return req, None
    try:
        inputs = await fetch_fresh_account_inputs(
            gateway,
            symbol=req.symbol,
            target_snapshot=snapshot,
            max_position_age_seconds=effective.max_account_data_age_seconds,
        )
        health = await gateway.health()
        account_type = health.get("accountType") if isinstance(health, dict) else None
        if (
            not isinstance(health, dict)
            or health.get("ok") is not True
            or health.get("gatewayContractVersion") != 3
            or account_type not in {"DEMO", "REAL"}
            or health.get("accountRef") != inputs.raw_account.get("accountRef")
            or health.get("accountSessionRef")
            != inputs.raw_account.get("accountSessionRef")
            or account_type != inputs.raw_account.get("accountType")
        ):
            raise ValueError("fresh gateway health/account identity mismatch")
        async with async_session_factory() as session:
            account_ref = str(inputs.raw_account.get("accountRef") or "").strip()
            reserved = await calculate_backend_reserved_cash(
                session, account_ref=account_ref
            )
            symbol_reserved = await calculate_backend_reserved_cash(
                session, account_ref=account_ref, symbol=req.symbol
            )
            ownership = await load_bot_ownership(session, account_ref)
            handling = await get_account_reservation_handling(session)
            adapter = MatriksAccountContextAdapter(
                reservation_handling=handling,
                allow_margin_buying=effective.allow_margin_buying,
                max_account_data_age_seconds=effective.max_account_data_age_seconds,
            )
            context = adapter.normalize(
                raw_account=inputs.raw_account,
                raw_positions=inputs.raw_positions,
                raw_open_orders=inputs.raw_open_orders,
                backend_reserved_cash_tl=reserved,
                symbol=req.symbol,
                market_prices=inputs.market_prices,
                bot_owned_qty_by_symbol=ownership.quantities,
                current_symbol_reserved_cash_tl=symbol_reserved,
            )
            await adapter.add_audit(
                session, request_id=req.request_id, symbol=req.symbol
            )
            await session.commit()
        return req.model_copy(update={"account_sizing_context": context}), account_type
    except Exception:
        logger.exception(
            "Fresh account verification/normalization failed; BUY remains blocked request_id=%s",
            req.request_id,
        )
        return req, None


def _has_explicit_daily_trade_count(req: SignalRequest) -> bool:
    """Return True when dailyTradeCount was present in the request payload."""
    return bool({"daily_trade_count", "dailyTradeCount"} & req.model_fields_set)


async def evaluate_symbol(
    symbol: str,
    *,
    gateway: MatriksGatewayClient | None = None,
    provider: AiProvider | None = None,
    request_id: str | None = None,
    evaluation_purpose: str = "TRADING",
    research_context: dict[str, Any] | None = None,
) -> EvaluationResult | None:
    """Bir sembolu uctan uca degerlendir; final karari dondur.

    v2: mod parametresi kaldirildi. ``evaluation_purpose=="TRADING"`` olan
    degerlendirmeler ``dispatch_eligible=True`` uretir; research kararlari
    asla emre donusmez. Gercek dispatch karari scanner'da systemMode=
    AUTO_TRADE + account watcher + audit + risk kapilarindan gecer.

    Args:
        symbol: Kok sembol (or. ``"THYAO"``).
        gateway: Matriks gateway client'i (default: paylasilan singleton).
        provider: AI provider (default: settings'ten gelen singleton).
        request_id: Verilmezse ``SYMBOL-yyyyMMdd-HHmmss-scan`` uretilir.
        evaluation_purpose: ``"TRADING"`` (default) veya ``"RESEARCH_DISCOVERY"``.

    Returns:
        ``EvaluationResult``; veri degerlendirilemeyecek kadar bozuksa
        (lastPrice<=0) ``None``.

    Raises:
        GatewayUnavailable: Gateway'e hic ulasilamiyor - cagiran (scanner)
        yakalayip turu atlar.
    """
    gateway = gateway or gateway_client
    decision_created_utc = datetime.now(timezone.utc)
    symbol = symbol.strip().upper()
    request_id = request_id or _build_request_id(symbol)
    evaluation_purpose = str(evaluation_purpose or "TRADING").strip().upper()
    research_only = evaluation_purpose == "RESEARCH_DISCOVERY"

    # == 1. Kok sembol snapshot'i =========================================
    snapshot = await gateway.get_snapshot(symbol)
    root_payload: dict[str, Any] = snapshot.get("payload") or {}

    await record_market_observation_standalone(symbol, root_payload, request_id=request_id)

    last_price = root_payload.get("lastPrice") or 0
    if last_price <= 0:
        logger.warning(
            "Snapshot has no usable price; skipping evaluation symbol=%s "
            "priceSource=%s quoteReliable=%s",
            symbol,
            root_payload.get("priceSource"),
            root_payload.get("quoteReliable"),
        )
        return None

    steps: list[dict[str, Any]] = [
        _snapshot_step(1, symbol, "OHLCV", root_payload, "Root symbol snapshot")
    ]

    # == 2. Iliskili sembol verisi ========================================
    related = RELATED_SYMBOLS.get(symbol)
    if related is not None:
        try:
            related_snapshot = await gateway.get_snapshot(related)
            steps.append(
                _snapshot_step(
                    2,
                    related,
                    "DEPTH",
                    related_snapshot.get("payload") or {},
                    f"{symbol} icin {related} derinlik verisi (iliskili hisse)",
                )
            )
        except GatewayError as exc:
            # Iliskili veri "olsa iyi olur" kategorisi - yoksa karari engellemez.
            logger.warning(
                "Related symbol snapshot failed; continuing without it "
                "root=%s related=%s error=%s",
                symbol,
                related,
                exc,
            )

    # == 3. SignalRequest koprusu =========================================
    sig_req = snapshot_to_signal_request(symbol, root_payload, request_id=request_id)

    sig_req = sig_req.model_copy(update={"evaluation_purpose": evaluation_purpose})

    # v2: research kararlari asla emre donusmez.
    dispatch_eligible = not research_only

    # == 4. Runtime kontroller ============================================
    (
        sig_req,
        runtime_engine,
        kill_switch_enabled,
        demo_allow_downtrend_buy,
    ) = await with_runtime_controls(sig_req)
    sig_req = await with_resolved_daily_trade_count(sig_req)
    sig_req = await with_trade_eligibility(sig_req)

    if kill_switch_enabled:
        response = kill_switch_response(sig_req)
        payload = build_payload(sig_req, active_config=runtime_engine.config)
        raw = {
            "action": "WAIT",
            "confidence": 0.0,
            "risk_score": 0.0,
            "reason": response.reason,
        }
        await _log_evaluation(sig_req, response)
        await persist_evaluation(sig_req, payload, raw, response)
        return EvaluationResult(
            response=response,
            dispatch_eligible=dispatch_eligible,
            decision_created_utc=decision_created_utc,
            evaluation_purpose=evaluation_purpose,
            raw_action=SignalAction.WAIT,
        )

    # == 5. Dis baglam (haber + akilli para + admin fundamentals) =========
    runtime_config_hash = decision_context_fingerprint(
        runtime_engine.config.model_dump(mode="json")
    )
    async with async_session_factory() as profile_session:
        active_profile_code = (await get_active_profile(profile_session)).code
    try:
        (
            news_context,
            kap_context,
            broker_flow_context,
            fundamentals_context,
            market_regime,
        ) = await asyncio.wait_for(
            asyncio.gather(
                get_news_context([sig_req.symbol]),
                get_kap_context([sig_req.symbol]),
                get_broker_flow_context(
                    [sig_req.symbol], config_version=runtime_config_hash
                ),
                get_fundamentals_context([sig_req.symbol]),
                get_index_regime(gateway),
            ),
            timeout=12.0,
        )
    except asyncio.TimeoutError:
        logger.warning("Context budget exceeded symbol=%s", sig_req.symbol)
        news_context, kap_context, fundamentals_context, market_regime = (
            {},
            {},
            {},
            None,
        )
        broker_flow_context = {
            sig_req.symbol: {"available": False, "smartMoneyFlow": "UNKNOWN"}
        }

    payload = build_payload(
        sig_req,
        news_context=news_context,
        broker_flow_context=broker_flow_context,
        fundamentals_context=fundamentals_context,
        kap_context=kap_context,
        active_config=runtime_engine.config,
    )
    payload["agenticSteps"] = steps
    payload["macroMarketRegime"] = market_regime
    payload["macroMarketRegimeSymbol"] = settings.market_index_symbol.strip().upper()
    payload["symbolTrendRegime"] = sig_req.symbol_trend_regime
    sig_req = sig_req.model_copy(
        update={
            "macro_market_regime": market_regime,
            "macro_market_regime_symbol": settings.market_index_symbol.strip().upper(),
        }
    )
    payload["configHash"] = runtime_config_hash
    payload["profileCode"] = active_profile_code
    if research_context:
        payload.update(_json_safe(research_context))
    payload["evaluationPurpose"] = evaluation_purpose
    if research_only:
        payload["allowOrder"] = False

    # == 5.5. Pozisyon baglami (portfolio yonetimi) =======================
    # Acik bot pozisyonu varken LLM'in gorevi yeni alim aramak degil eldeki
    # pozisyonu yonetmektir: maliyet + anlik K/Z payload'a eklenir ve prompt
    # kural 16 devreye girer (kar al / zarar kes / tut).
    position_context = await _build_position_context(sig_req)
    if position_context:
        payload["positionContext"] = position_context

    # == 6. Admin test override VEYA AI karari ============================
    ai_context = build_ai_decision_context(
        sig_req,
        news_context=news_context,
        broker_flow_context=broker_flow_context,
        kap_context=kap_context,
        profile=active_profile_code,
        macro_market_regime=market_regime,
        position_context=position_context,
        research_context=research_context,
    )
    raw: dict[str, Any] | None = None

    # Hard data-quality WAIT outranks every decision source, including an admin
    # test override. Reliable research remains analytical; reliable active
    # TRADING watchlist entries bypass only the neutral cost shortcut.
    gate_reason = preflight_wait_reason(
        symbol=sig_req.symbol,
        indicator_consensus=sig_req.indicator_consensus,
        bot_position_qty=sig_req.bot_position_qty,
        news_context=news_context,
        evaluation_purpose=evaluation_purpose,
        trade_eligible=sig_req.trade_eligible,
        quote_reliable=sig_req.quote_reliable,
        ohlc_reliable=sig_req.ohlc_reliable,
    )
    gate_category = (
        classify_block_reason(gate_reason) if gate_reason is not None else None
    )
    if gate_category == "DATA_QUALITY_UNRELIABLE":
        raw = {
            "action": "WAIT",
            "confidence": 0.0,
            "risk_score": 0.0,
            "reason": gate_reason,
            "block_category": gate_category,
        }
        payload["decisionSource"] = "preflight-gate"
        payload["preflightBlockCategory"] = gate_category

    # v2: admin test override yalnizca research olmayan degerlendirmelerde
    # uygulanir (mod-bagimsiz). Override kararlari da systemMode gate'inden
    # gecmeden emre donusmez.
    if raw is None and not research_only:
        try:
            async with async_session_factory() as ov_session:
                override = await consume_override(ov_session, sig_req.symbol)
            if override is not None:
                raw = override_to_raw_decision(override)
                payload["decisionSource"] = "admin-override"
        except Exception:
            logger.exception("Failed to check signal override for %s", sig_req.symbol)

    # == 6.5. Token-cost kapilari (LLM'e gitmeden karar) ==================
    # Sira: hard data quality > admin override > neutral cost gate > LLM.
    if raw is None and gate_reason is not None:
        raw = {
            "action": "WAIT",
            "confidence": 0.0,
            "risk_score": 0.0,
            "reason": gate_reason,
            "block_category": gate_category,
        }
        payload["decisionSource"] = "preflight-gate"
        payload["preflightBlockCategory"] = gate_category

    # == 6.6. Deterministik entry kapısı (Plan Faz 1.3) ==================
    # Flag açıkken: zayıf ya da yetersiz-veri setup'ları LLM'e gitmeden elenir;
    # güçlü setup'lar bar/setup bazında kalıcı AI-call claim ile kapılanır
    # (aynı bar içinde aynı setup tekrar LLM'e sorulmaz, restart sonrası dahil).
    setup_score_value: float | None = None
    if settings.deterministic_entry_enabled and raw is None and not research_only:
        setup = compute_setup_score(sig_req)
        setup_score_value = setup.total
        payload["setupScore"] = setup.total
        payload["setupComponents"] = setup.components
        threshold = settings.deterministic_entry_min_setup_score
        if not setup.data_sufficient or setup.total < threshold:
            raw = {
                "action": "WAIT",
                "confidence": 0.0,
                "risk_score": 0.0,
                "reason": (
                    f"Deterministic setup gate: score={setup.total} "
                    f"threshold={threshold} dataSufficient={setup.data_sufficient}"
                ),
                "block_category": "SETUP_SCORE_LOW",
            }
            payload["decisionSource"] = "deterministic-setup-gate"
        else:
            levels_preview = compute_entry_levels(sig_req)
            fingerprint = compute_setup_fingerprint(
                action="BUY",
                setup_score=setup.total,
                entry=levels_preview.entry if levels_preview else None,
                stop_loss=levels_preview.stop_loss if levels_preview else None,
                target=levels_preview.target if levels_preview else None,
            )
            try:
                async with async_session_factory() as claim_session:
                    claimed = await try_claim_ai_call(
                        claim_session,
                        symbol=sig_req.symbol,
                        bar_key=resolve_bar_key(sig_req),
                        setup_fingerprint=fingerprint,
                        evaluation_purpose=evaluation_purpose,
                    )
                    await claim_session.commit()
            except Exception:
                # Claim altyapısı hatası LLM'i engellememeli (fail-open).
                logger.exception("AI call claim failed symbol=%s", sig_req.symbol)
                claimed = True
            if not claimed:
                raw = {
                    "action": "WAIT",
                    "confidence": 0.0,
                    "risk_score": 0.0,
                    "reason": "AI already asked for this bar/setup; skip re-ask",
                    "block_category": "AI_CALL_ALREADY_CLAIMED",
                }
                payload["decisionSource"] = "ai-call-gate"
            else:
                # Güçlü, yeni setup: AI'ya deterministik bağlamı ver (veto için).
                ai_context["deterministicSetup"] = {
                    "setupScore": setup.total,
                    "components": setup.components,
                    "entry": float(levels_preview.entry) if levels_preview else None,
                    "stopLoss": float(levels_preview.stop_loss)
                    if levels_preview
                    else None,
                    "target": float(levels_preview.target) if levels_preview else None,
                    "rewardRisk": levels_preview.reward_risk if levels_preview else None,
                }

    # v2 Faz 5: 15 sn'lik DecisionCache devre dışı bırakıldı — portföy
    # taramasındaki önem dedektörü (app/services/significance.py) LLM
    # çağrısını zaten baseline'a göre kapılıyor; kısa-TTL cache'in kalan
    # değeri yoktu. Sınıf kullanım dışıdır ve Faz 8 cutover'ında silinecek.
    if raw is None:
        provider = provider or get_default_provider()
        # Veto akışı (Plan Faz 1.4) yalnızca yeni girişte: açık pozisyon
        # yönetimi (SELL/HOLD) tam prompt'a ihtiyaç duyar, deterministik exit
        # (Faz 2) henüz yok. Research değerlendirmeleri de analitiktir.
        veto_only = bool(
            settings.deterministic_entry_enabled
            and not research_only
            and sig_req.bot_position_qty == 0
        )
        raw = await provider.decide(
            ai_context, request_id=sig_req.request_id, veto_only=veto_only
        )
        payload["decisionSource"] = "llm-veto" if veto_only else "llm"

    # == 7. RiskEngine (makro rejim filtresiyle) ==========================
    decision = dict_to_risk_decision(raw, sig_req)

    # == 6.9. Deterministik fiyat seviyeleri (Plan Faz 1.5) ==============
    # Flag açıkken model-üretimli entry/stop/target emir kararında
    # KULLANILMAZ; seviyeler best-ask + ATR'den deterministik hesaplanır. AI
    # yalnızca yön onayı (BUY/WAIT) verir. Güvenli bir setup üretilemiyorsa
    # (aşırı oynak / veri eksik) BUY, WAIT'e indirilir.
    if (
        settings.deterministic_entry_enabled
        and decision.action == SignalAction.BUY
        and not research_only
    ):
        levels = compute_entry_levels(sig_req)
        if levels is None:
            decision.action = SignalAction.WAIT
            decision.entry_range = None
            decision.stop_loss = None
            decision.target_price = None
            decision.reason = (
                (decision.reason or "")
                + " | Deterministik seviye üretilemedi (oynaklık/veri); WAIT"
            )
            payload["decisionSource"] = "deterministic-levels-unavailable"
        else:
            decision.entry_range = EntryRange(min=levels.entry, max=levels.entry)
            decision.stop_loss = levels.stop_loss
            decision.target_price = levels.target
            payload["deterministicLevels"] = {
                "entry": float(levels.entry),
                "stopLoss": float(levels.stop_loss),
                "target": float(levels.target),
                "rewardRisk": levels.reward_risk,
                "stopDistancePct": levels.stop_distance_pct,
            }

    if decision.action == SignalAction.BUY and not research_only:
        sig_req, verified_account_type = await with_fresh_account_sizing_context(
            sig_req,
            gateway=gateway,
            snapshot=snapshot,
            runtime_engine=runtime_engine,
        )
        response = runtime_engine.evaluate(
            sig_req,
            decision,
            market_regime=market_regime,
            account_type=verified_account_type,
            allow_demo_downtrend_buy=demo_allow_downtrend_buy,
        )
    else:
        response = runtime_engine.evaluate(
            sig_req, decision, market_regime=market_regime
        )
    await persist_sizing_audit(sig_req, runtime_engine)
    from app.services.news_risk_lock import apply_news_risk_lock

    response = await apply_news_risk_lock(response, sig_req.symbol)

    # v2 günlük zarar limiti (Faz 5): sadece emre dönüşebilecek BUY'ları
    # keser; SELL/WAIT ve (pipeline dışı) stop-loss guard asla etkilenmez.
    from app.services.daily_pnl import apply_daily_loss_limit

    response = await apply_daily_loss_limit(response, gateway=gateway)
    rotation_eligible = bool(
        decision.action == SignalAction.BUY
        and runtime_engine.last_buy_viability_passed
        and response.action == SignalAction.BUY
        and response.allow_order
    )
    if (
        decision.action == SignalAction.BUY
        and runtime_engine.last_buy_viability_passed
        and runtime_engine.last_sizing_result is not None
        and not runtime_engine.last_sizing_result.allowed
        and decision.entry_range is not None
        and decision.stop_loss is not None
        and decision.target_price is not None
    ):
        viability_probe = SignalResponse(
            requestId=sig_req.request_id,
            symbol=sig_req.symbol,
            action=SignalAction.BUY,
            qty=1,
            orderType=OrderType.LIMIT,
            price=decision.entry_range.max,
            confidenceScore=decision.confidence,
            riskScore=decision.risk_score,
            allowOrder=True,
            reason=decision.reason or "Rotation viability probe",
            entryRange=decision.entry_range,
            stopLoss=decision.stop_loss,
            targetPrice=decision.target_price,
            targetAllocationPct=decision.target_allocation_pct,
        )
        viability_probe = await apply_news_risk_lock(
            viability_probe, sig_req.symbol
        )
        viability_probe = await apply_daily_loss_limit(
            viability_probe, gateway=gateway
        )
        rotation_eligible = bool(
            viability_probe.action == SignalAction.BUY
            and viability_probe.allow_order
        )

    # == 8. Log + persist =================================================
    await _log_evaluation(sig_req, response)
    await persist_evaluation(sig_req, payload, raw, response)
    try:
        from app.services.position_management import record_position_management

        await record_position_management(sig_req, raw, response)
    except Exception:
        logger.exception(
            "Position management persistence failed symbol=%s", sig_req.symbol
        )

    return EvaluationResult(
        response=response,
        dispatch_eligible=dispatch_eligible,
        decision_created_utc=decision_created_utc,
        evaluation_purpose=evaluation_purpose,
        research_score=(
            _safe_float(raw.get("research_score")) if "research_score" in raw else None
        ),
        opportunity_score=(
            _safe_float(raw.get("opportunity_score"))
            if "opportunity_score" in raw
            else None
        ),
        setup_score=setup_score_value,
        target_allocation_pct=(
            _safe_float(raw.get("target_allocation_pct"))
            if "target_allocation_pct" in raw
            else None
        ),
        decision_entry_price=(
            decision.entry_range.max if decision.entry_range is not None else None
        ),
        decision_target_price=decision.target_price,
        sizing_binding_limits=(
            tuple(runtime_engine.last_sizing_result.binding_limits)
            if runtime_engine.last_sizing_result is not None
            else ()
        ),
        sizing_account=sig_req.account_sizing_context,
        sizing_trade=runtime_engine.last_sizing_trade,
        sizing_result=runtime_engine.last_sizing_result,
        effective_limits=runtime_engine.effective_config,
        rotation_eligible=rotation_eligible,
        raw_action=decision.action,
        decision_source=payload.get("decisionSource"),
    )


async def _log_evaluation(req: SignalRequest, response: SignalResponse) -> None:
    await log_signal_evaluation(
        request_id=req.request_id,
        symbol=req.symbol,
        request=req.model_dump(by_alias=True, mode="json"),
        response=response.model_dump(by_alias=True, mode="json"),
    )
