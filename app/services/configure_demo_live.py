"""Idempotent DEMO_LIVE configuration CLI (Task 2-7 of the readiness brief).

Writes every SystemConfig value and the NORMAL trade profile needed for a
fully-automated DEMO_LIVE scan -> research -> Trade Watchlist -> BUY/SELL
session, then asks the gateway to reload. Re-running this command must never
create duplicate config rows or duplicate trade profiles, and it must never
open REAL_LIVE, enable margin buying, or send an order itself - it only
writes configuration and, at the very end, makes one read-only gateway
health call to confirm the reload landed.

Callable as:
    python -m app.services.configure_demo_live
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.config import settings
from app.db.session import async_session_factory
from app.services.admin_config import get_admin_config_value, set_admin_config_values
from app.services.matriks_gateway import GatewayError, GatewayUnavailable, gateway_client
from app.services.trade_profile import (
    activate_profile,
    ensure_builtin_profiles_seeded,
    get_profile,
    update_profile,
)

logger = logging.getLogger(__name__)

CHANGED_BY = "configure_demo_live"
CONFIRMATION = "CONFIRM"

# ── Task 2: trading gates ───────────────────────────────────────────────────
TRADING_GATE_VALUES: dict[str, str] = {
    "tradingMode": "DEMO_LIVE",
    "botMode": "DEMO_LIVE",
    "scannerEnabled": "true",
    "scannerAllowOrders": "true",
    "killSwitchEnabled": "false",
    "tradingKillSwitchActive": "false",
    "forceSafeMode": "false",
    "botEnableDemoOrders": "true",
    "botEnableRealOrders": "false",
    "botRealLiveModeAllowed": "false",
    "botRealLiveArmed": "false",
    "botRequireDemoAccount": "true",
    "manualApprovalAllowOrders": "false",
    "sizingAllowMarginBuying": "false",
}

# ── Task 2: symbol filters (empty manual lists - Trade Watchlist still gates) ──
SYMBOL_FILTER_VALUES: dict[str, str] = {
    "allowedSymbols": "",
    "buyAllowedSymbols": "",
    "sellExitAllowedSymbols": "",
    "declineSymbols": "",
    "lockedLongTermSymbols": "",
}

# ── Task 4: global sizing, matched to the NORMAL trade profile ─────────────
SIZING_VALUES: dict[str, str] = {
    "sizingRiskPerTradePct": "0.50",
    "sizingMaxCashUtilizationPct": "25",
    "sizingMaxAccountExposurePct": "50",
    "sizingMaxPositionValuePerSymbol": "3000",
    "sizingMaxOrderValueTl": "1000",
    "sizingMaxQtyPerOrder": "3",
    "sizingMinOrderValueTl": "1",
    "sizingMinStopDistancePct": "0.10",
    "sizingMaxStopDistancePct": "10",
    "sizingMinimumStopSlippagePct": "0.05",
    "sizingMaximumStopSlippagePct": "1",
    "sizingProfileStopSlippagePct": "0.20",
    "sizingMaxAccountDataAgeSeconds": "60",
    "sizingMinimumBuyConfidence": "75",
    "sizingMinimumSellConfidence": "70",
    "sizingDailyOrderLimit": "3",
    "sizingPerSymbolDailyOrderLimit": "1",
    "sizingAllowMarginBuying": "false",
}

# ── Task 5: discovery / research / promotion ────────────────────────────────
DISCOVERY_VALUES: dict[str, str] = {
    "discoveryIntervalMinutes": "5",
    "maxResearchCandidatesPerCycle": "10",
    "maxActiveResearchSymbols": "10",
    "maxConcurrentResearchEvaluations": "2",
    "candidateCooldownMinutes": "15",
    "maxTradeWatchlistSize": "20",
    "minimumTrendPreScore": "60",
    "minimumResearchScore": "75",
    "researchMinimumConfidence": "75",
    "researchMaximumRiskScore": "35",
    "promotionConsecutivePasses": "2",
    "promotionMinIntervalMinutes": "10",
    "researchCandidateTtlHours": "24",
    "tradeWatchlistTtlHours": "24",
    "discoveryMinimumVolumeTl": "100000000",
    "discoveryMaximumSpreadPct": "0.50",
    "portfolioScanIntervalMinutes": "30",
}

# ── Task 6: time / measurement config (disableTradingAfter != marketSessionCloseTime) ──
TIME_MEASUREMENT_VALUES: dict[str, str] = {
    "timezone": "Europe/Istanbul",
    "disableTradingAfter": "17:30",
    "marketSessionCloseTime": "18:00",
    "stopGuardMaximumQuoteAgeSeconds": "30",
    "outcomeMaximumObservationDelaySeconds": "120",
}

# ── Task 3: NORMAL trade profile target values ──────────────────────────────
NORMAL_PROFILE_TARGET: dict[str, Any] = {
    "max_order_value_tl": Decimal("1000"),
    "max_qty_per_order": 3,
    "max_position_value_per_symbol": Decimal("3000"),
    "risk_per_trade_pct": Decimal("0.50"),
    "max_cash_utilization_pct": Decimal("25"),
    "max_account_exposure_pct": Decimal("50"),
    "max_orders_per_day": 3,
    "max_orders_per_symbol_per_day": 1,
    "min_confidence_for_buy": 75.0,
    "min_confidence_for_sell": 70.0,
    "allow_demo_live": True,
    "allow_real_live": True,
    "allow_margin_buying": False,
    "allow_short_selling": False,
    "scan_interval_minutes": 30,
    "indicator_period": "Min5",
    "order_time_in_force": "Day",
}

# commissionBps/exchangeFeeBps/otherFeeBps/minimumCommissionTl are Task 7 -
# deliberately not written here at all; see verify_commission_config().
COMMISSION_KEYS = (
    "commissionBps",
    "exchangeFeeBps",
    "otherFeeBps",
    "minimumCommissionTl",
)


def _values_equal(current: Decimal | int | float | bool | str, target: Any) -> bool:
    if isinstance(target, Decimal):
        try:
            return Decimal(str(current)) == target
        except Exception:
            return False
    if isinstance(target, bool):
        return bool(current) == target
    if isinstance(target, (int, float)):
        try:
            return float(current) == float(target)
        except (TypeError, ValueError):
            return False
    return str(current) == str(target)


@dataclass
class ConfigureDemoLiveReport:
    applied_config_keys: list[str] = field(default_factory=list)
    scan_universe_action: str = ""
    market_index_symbol_ok: bool = True
    demo_account_confirmed: bool = False
    demo_account_note: str | None = None
    normal_profile_created: bool = False
    normal_profile_fields_corrected: list[str] = field(default_factory=list)
    normal_profile_activated: bool = False
    commission_config_present: bool = False
    commission_warning: str | None = None
    gateway_reload_ok: bool = False
    gateway_reload_error: str | None = None
    gateway_health: dict[str, Any] | None = None
    errors: list[str] = field(default_factory=list)


async def _verify_demo_account(session) -> tuple[bool, str | None]:
    """Read-only check against the gateway's own live account signal
    (testAutoOrderEnabled), never trusted blindly (Task 2)."""
    try:
        health = await gateway_client.health()
    except (GatewayUnavailable, GatewayError) as exc:
        return False, f"DEMO_ACCOUNT_NOT_CONFIRMED: gateway health unavailable ({exc})"
    except Exception:
        logger.exception("DEMO_ACCOUNT_VERIFICATION_FAILED")
        return False, "DEMO_ACCOUNT_NOT_CONFIRMED: gateway health check raised an error"

    if health.get("testAutoOrderEnabled") is True:
        return True, None
    return (
        False,
        "DEMO_ACCOUNT_NOT_CONFIRMED: gateway reports testAutoOrderEnabled="
        f"{health.get('testAutoOrderEnabled')!r} (account may not be a demo/test-auto-order account)",
    )


async def _apply_scan_universe(session, report: ConfigureDemoLiveReport) -> None:
    current = (await get_admin_config_value(session, "scanUniverseSymbols")).strip()
    if current:
        report.scan_universe_action = "PRESERVED_EXISTING"
        return
    await set_admin_config_values(
        session,
        {"scanUniverseSymbols": settings.discovery_symbols},
        changed_by=CHANGED_BY,
        reason="configure_demo_live: seed empty scan universe from DISCOVERY_SYMBOLS",
        confirmation=CONFIRMATION,
    )
    report.scan_universe_action = "SEEDED_FROM_ENV"
    report.applied_config_keys.append("scanUniverseSymbols")


async def _apply_normal_profile(session, report: ConfigureDemoLiveReport) -> None:
    await ensure_builtin_profiles_seeded(session)
    profile = await get_profile(session, "NORMAL")
    if profile is None:
        # Should not happen (NORMAL is a BUILTIN_PROFILES entry), but never
        # create a second profile under the same code if it somehow races.
        report.errors.append("NORMAL profile missing after seed - not created twice")
        return

    diffs = {
        key: target
        for key, target in NORMAL_PROFILE_TARGET.items()
        if not _values_equal(getattr(profile, key), target)
    }
    if diffs:
        await update_profile(
            session,
            "NORMAL",
            diffs,
            changed_by=CHANGED_BY,
            reason="configure_demo_live: restore expected NORMAL profile values",
            confirmation=CONFIRMATION,
        )
        report.normal_profile_fields_corrected = sorted(diffs)

    await activate_profile(
        session,
        "NORMAL",
        changed_by=CHANGED_BY,
        reason="configure_demo_live: activate NORMAL as the system-wide profile",
        confirmation=CONFIRMATION,
    )
    report.normal_profile_activated = True


async def verify_commission_config(session) -> tuple[bool, str | None]:
    """Never fabricates a commission rate (Task 7) - only reports whether the
    operator has entered one yet."""
    values = {key: await get_admin_config_value(session, key) for key in COMMISSION_KEYS}
    any_nonzero = any(Decimal(str(v)) != 0 for v in values.values())
    if any_nonzero:
        return True, None
    return (
        False,
        "TRANSACTION_COST_CONFIG_REQUIRED: commissionBps/exchangeFeeBps/otherFeeBps/"
        "minimumCommissionTl are all 0 - enter the real Matriks/broker commission "
        "rate in the admin panel before treating P&L as net-of-cost.",
    )


async def run_configure_demo_live() -> ConfigureDemoLiveReport:
    report = ConfigureDemoLiveReport()
    async with async_session_factory() as session:
        try:
            demo_confirmed, demo_note = await _verify_demo_account(session)
            report.demo_account_confirmed = demo_confirmed
            report.demo_account_note = demo_note

            batch: dict[str, str] = {}
            batch.update(TRADING_GATE_VALUES)
            batch.update(SYMBOL_FILTER_VALUES)
            batch.update(SIZING_VALUES)
            batch.update(DISCOVERY_VALUES)
            batch.update(TIME_MEASUREMENT_VALUES)
            # botDemoAccountConfirmed is never blindly forced true - only set
            # when the live gateway signal actually confirmed it this run.
            batch["botDemoAccountConfirmed"] = "true" if demo_confirmed else "false"

            await set_admin_config_values(
                session,
                batch,
                changed_by=CHANGED_BY,
                reason="configure_demo_live: DEMO_LIVE readiness configuration",
                confirmation=CONFIRMATION,
            )
            report.applied_config_keys.extend(sorted(batch))

            await _apply_scan_universe(session, report)
            await _apply_normal_profile(session, report)

            report.market_index_symbol_ok = (
                settings.market_index_symbol.strip().upper() == "XU100"
            )

            commission_present, commission_warning = await verify_commission_config(session)
            report.commission_config_present = commission_present
            report.commission_warning = commission_warning
        except Exception as exc:
            logger.exception("CONFIGURE_DEMO_LIVE_FAILED")
            report.errors.append(f"{type(exc).__name__}: {exc}")
            return report

    # ── Task 8: gateway config reload (best-effort, never shuts anything down) ──
    try:
        await gateway_client.reload_config()
        report.gateway_reload_ok = True
    except (GatewayUnavailable, GatewayError) as exc:
        report.gateway_reload_error = str(exc)
    except Exception as exc:
        logger.exception("GATEWAY_CONFIG_RELOAD_FAILED")
        report.gateway_reload_error = f"{type(exc).__name__}: {exc}"

    try:
        report.gateway_health = await gateway_client.health()
    except (GatewayUnavailable, GatewayError) as exc:
        report.errors.append(f"post-reload gateway health check failed: {exc}")
    except Exception as exc:
        logger.exception("POST_RELOAD_HEALTH_CHECK_FAILED")
        report.errors.append(f"post-reload gateway health check raised: {exc}")

    return report


def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    report = asyncio.run(run_configure_demo_live())
    logger.info(
        "CONFIGURE_DEMO_LIVE_COMPLETE appliedKeys=%s scanUniverse=%s "
        "demoAccountConfirmed=%s normalProfileCorrected=%s normalProfileActivated=%s "
        "commissionConfigPresent=%s gatewayReloadOk=%s errors=%s",
        len(report.applied_config_keys),
        report.scan_universe_action,
        report.demo_account_confirmed,
        report.normal_profile_fields_corrected,
        report.normal_profile_activated,
        report.commission_config_present,
        report.gateway_reload_ok,
        report.errors,
    )
    if report.demo_account_note:
        logger.warning(report.demo_account_note)
    if report.commission_warning:
        logger.warning(report.commission_warning)
    if report.gateway_reload_error:
        logger.warning(
            "GATEWAY_CONFIG_RELOAD_FAILED - the Matriks algorithm should be "
            "restarted manually: %s",
            report.gateway_reload_error,
        )


if __name__ == "__main__":
    _main()
