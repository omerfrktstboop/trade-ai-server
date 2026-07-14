"""Runtime admin configuration backed by ``system_configs``."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.risk_config import RiskConfig, risk_config
from app.models.db import ConfigAuditLog, SystemConfig
from app.models.signal import SignalMode
from app.services.trade_profile import get_active_profile


SECRET_CONFIG_KEYS = {"API_TOKEN", "DEEPSEEK_API_KEY", "DATABASE_URL"}
RISKY_CONFIRMATION = "CONFIRM"


@dataclass(frozen=True)
class ConfigDefinition:
    key: str
    value_type: str
    default: str
    description: str
    is_sensitive: bool = False


@dataclass(frozen=True)
class ConfigSectionDefinition:
    title: str
    description: str
    keys: tuple[str, ...]


def _settings_default_mode() -> str:
    return str(settings.default_mode.value).upper()


CONFIG_DEFINITIONS: dict[str, ConfigDefinition] = {
    "allowedSymbols": ConfigDefinition(
        "allowedSymbols",
        "string",
        risk_config.allowed_symbols,
        "Manuel BUY beyaz listesi. Boşsa bu manuel filtre kalkar; aktif Trade "
        "Watchlist zorunluluğu devam eder.",
    ),
    "declineSymbols": ConfigDefinition(
        "declineSymbols",
        "string",
        risk_config.decline_symbols,
        "Kara liste: buradaki sembollere yeni BUY asla verilmez; mevcut "
        "pozisyondan güvenli SELL çıkışı engellenmez.",
    ),
    "lockedLongTermSymbols": ConfigDefinition(
        "lockedLongTermSymbols",
        "string",
        risk_config.locked_long_term_symbols,
        "Uzun vadeli kilitli semboller. Bu semboller otomatik SELL kararlarından korunur.",
    ),
    "disableTradingAfter": ConfigDefinition(
        "disableTradingAfter",
        "time",
        risk_config.disable_trading_after,
        "Bu saatten sonra BUY/SELL kararları engellenir. Format HH:MM.",
    ),
    "timezone": ConfigDefinition(
        "timezone",
        "timezone",
        risk_config.timezone,
        "İşlem kesim saati kontrollerinde kullanılan IANA timezone değeri.",
    ),
    "tradingMode": ConfigDefinition(
        "tradingMode",
        "mode",
        _settings_default_mode(),
        "Gelen request mode değerini sistem genelinde override eden çalışma modu.",
    ),
    "killSwitchEnabled": ConfigDefinition(
        "killSwitchEnabled",
        "bool",
        "false",
        "true olduğunda tüm sinyal değerlendirmeleri WAIT ve allowOrder=false döner.",
    ),
    "botMode": ConfigDefinition(
        "botMode",
        "mode",
        "PAPER",
        "Matriks bot runtime modu. Riskli modlar confirmation ister.",
    ),
    "botEnableDemoOrders": ConfigDefinition(
        "botEnableDemoOrders",
        "bool",
        "false",
        "Matriks botun demo hesaba emir gondermesine izin verir.",
    ),
    "botEnableRealOrders": ConfigDefinition(
        "botEnableRealOrders",
        "bool",
        "false",
        "Matriks botun real hesaba emir gondermesine izin verir.",
    ),
    "tradingKillSwitchActive": ConfigDefinition(
        "tradingKillSwitchActive",
        "bool",
        "false",
        "true iken tum order dispatch yollarini kapatir.",
    ),
    "forceSafeMode": ConfigDefinition(
        "forceSafeMode",
        "bool",
        "false",
        "true iken analiz surer, order dispatch kapanir.",
    ),
    "buyAllowedSymbols": ConfigDefinition(
        "buyAllowedSymbols",
        "string",
        risk_config.allowed_symbols,
        "Gateway için ek manuel BUY filtresi; Trade Watchlist yetkisi yerine geçmez.",
    ),
    "sellExitAllowedSymbols": ConfigDefinition(
        "sellExitAllowedSymbols",
        "string",
        risk_config.allowed_symbols,
        "Mevcut pozisyonlardan SELL_EXIT izinli semboller.",
    ),
    "botRealLiveModeAllowed": ConfigDefinition(
        "botRealLiveModeAllowed",
        "bool",
        "false",
        "REAL_LIVE modunun profile disinda backend tarafindan kullanilabilmesini belirler.",
    ),
    "botRealLiveArmed": ConfigDefinition(
        "botRealLiveArmed",
        "bool",
        "false",
        "REAL_LIVE emir yolunu kasitli olarak kurar; tek basina emir yetkisi vermez.",
    ),
    "botRequireDemoAccount": ConfigDefinition(
        "botRequireDemoAccount",
        "bool",
        "true",
        "Demo hesap onayi zorunlulugunu belirler.",
    ),
    "botDemoAccountConfirmed": ConfigDefinition(
        "botDemoAccountConfirmed",
        "bool",
        "false",
        "Matriks demo hesap kullanildigini onaylar.",
    ),
    "botAllowMarketOrders": ConfigDefinition(
        "botAllowMarketOrders",
        "bool",
        "false",
        "MARKET emirleri sistem genelinde yasaktir; true kabul edilmez.",
    ),
    "botHttpTimeoutSeconds": ConfigDefinition(
        "botHttpTimeoutSeconds",
        "int",
        "15",
        "Matriks bot HTTP timeout suresi, saniye.",
    ),
    "sizingRiskPerTradePct": ConfigDefinition(
        "sizingRiskPerTradePct", "decimal", "0.50", "Per-trade equity risk percent."
    ),
    "sizingMaxCashUtilizationPct": ConfigDefinition(
        "sizingMaxCashUtilizationPct",
        "decimal",
        "25",
        "Maximum cash utilization percent.",
    ),
    "sizingMaxAccountExposurePct": ConfigDefinition(
        "sizingMaxAccountExposurePct",
        "decimal",
        "50",
        "Maximum account exposure percent.",
    ),
    "sizingMaxPositionValuePerSymbol": ConfigDefinition(
        "sizingMaxPositionValuePerSymbol",
        "decimal",
        "3000",
        "Maximum symbol position value.",
    ),
    "sizingMaxOrderValueTl": ConfigDefinition(
        "sizingMaxOrderValueTl", "decimal", "1000", "Maximum order value in TL."
    ),
    "sizingMaxQtyPerOrder": ConfigDefinition(
        "sizingMaxQtyPerOrder", "int", "3", "Maximum integer lot quantity per order."
    ),
    "sizingMinOrderValueTl": ConfigDefinition(
        "sizingMinOrderValueTl", "decimal", "1", "Minimum order value in TL."
    ),
    "sizingMinStopDistancePct": ConfigDefinition(
        "sizingMinStopDistancePct", "decimal", "0.10", "Minimum stop distance percent."
    ),
    "sizingMaxStopDistancePct": ConfigDefinition(
        "sizingMaxStopDistancePct", "decimal", "10", "Maximum stop distance percent."
    ),
    "sizingMinimumStopSlippagePct": ConfigDefinition(
        "sizingMinimumStopSlippagePct",
        "decimal",
        "0.05",
        "Minimum stop slippage buffer percent.",
    ),
    "sizingMaximumStopSlippagePct": ConfigDefinition(
        "sizingMaximumStopSlippagePct",
        "decimal",
        "1",
        "Maximum stop slippage buffer percent.",
    ),
    "sizingProfileStopSlippagePct": ConfigDefinition(
        "sizingProfileStopSlippagePct",
        "decimal",
        "0.20",
        "System stop slippage preference percent.",
    ),
    "sizingMaxAccountDataAgeSeconds": ConfigDefinition(
        "sizingMaxAccountDataAgeSeconds",
        "decimal",
        "60",
        "Maximum account sizing data age.",
    ),
    "sizingMinimumBuyConfidence": ConfigDefinition(
        "sizingMinimumBuyConfidence", "decimal", "75", "Minimum BUY confidence."
    ),
    "sizingMinimumSellConfidence": ConfigDefinition(
        "sizingMinimumSellConfidence", "decimal", "70", "Minimum SELL confidence."
    ),
    "sizingDailyOrderLimit": ConfigDefinition(
        "sizingDailyOrderLimit", "int", "3", "Global daily order limit."
    ),
    "sizingPerSymbolDailyOrderLimit": ConfigDefinition(
        "sizingPerSymbolDailyOrderLimit", "int", "1", "Per-symbol daily order limit."
    ),
    "sizingAllowMarginBuying": ConfigDefinition(
        "sizingAllowMarginBuying",
        "bool",
        "false",
        "Permit margin buying only when environment, system and profile all allow it.",
    ),
    "accountReservationHandling": ConfigDefinition(
        "accountReservationHandling",
        "reservation_handling",
        "UNKNOWN",
        "Whether broker buying power already deducts open BUY orders.",
    ),
    "marketDataDiagnosticsEnabled": ConfigDefinition(
        "marketDataDiagnosticsEnabled",
        "bool",
        "false",
        "Hacim/periyot semantik diagnostik loglarini etkinlestirir.",
    ),
    "marketDataDiagnosticSampleRatePct": ConfigDefinition(
        "marketDataDiagnosticSampleRatePct",
        "decimal",
        "10",
        "Diagnostik loglarda deterministik sembol sampling yuzdesi (0-100).",
    ),
    "marketDataWarningRateLimitSeconds": ConfigDefinition(
        "marketDataWarningRateLimitSeconds",
        "int",
        "60",
        "Ayni sembol/alan uyarilari arasindaki minimum sure.",
    ),
    "scanUniverseSymbols": ConfigDefinition(
        "scanUniverseSymbols",
        "string",
        settings.discovery_symbols,
        "Arastirma icin taranan genis BIST pay evreni; emir izni vermez.",
    ),
    "discoveryIntervalMinutes": ConfigDefinition(
        "discoveryIntervalMinutes",
        "int",
        str(settings.discovery_interval_minutes),
        "Kural tabanli piyasa kesif turlari arasindaki dakika.",
    ),
    "maxResearchCandidatesPerCycle": ConfigDefinition(
        "maxResearchCandidatesPerCycle",
        "int",
        str(settings.max_research_candidates_per_cycle),
        "Bir turda AI arastirmasina alinabilecek en fazla aday.",
    ),
    "maxActiveResearchSymbols": ConfigDefinition(
        "maxActiveResearchSymbols",
        "int",
        str(settings.max_active_research_symbols),
        "Gateway market-data aboneligindeki en fazla aktif research sembolu.",
    ),
    "maxConcurrentResearchEvaluations": ConfigDefinition(
        "maxConcurrentResearchEvaluations",
        "int",
        str(settings.max_concurrent_research_evaluations),
        "Eszamanli en fazla AI arastirma degerlendirmesi.",
    ),
    "candidateCooldownMinutes": ConfigDefinition(
        "candidateCooldownMinutes",
        "int",
        str(settings.candidate_cooldown_minutes),
        "Ayni adayin AI degerlendirmeleri arasindaki minimum sure.",
    ),
    "maxTradeWatchlistSize": ConfigDefinition(
        "maxTradeWatchlistSize",
        "int",
        str(settings.max_trade_watchlist_size),
        "Otomatik BUY degerlendirmesine acik en fazla sembol sayisi.",
    ),
    "minimumTrendPreScore": ConfigDefinition(
        "minimumTrendPreScore", "decimal", "60", "Research Candidate on eleme puani."
    ),
    "minimumResearchScore": ConfigDefinition(
        "minimumResearchScore", "decimal", "75", "Trade Watchlist AI arastirma puani."
    ),
    "researchMinimumConfidence": ConfigDefinition(
        "researchMinimumConfidence", "decimal", "75", "Promotion minimum AI guveni."
    ),
    "researchMaximumRiskScore": ConfigDefinition(
        "researchMaximumRiskScore", "decimal", "35", "Promotion maksimum AI risk puani."
    ),
    "promotionConsecutivePasses": ConfigDefinition(
        "promotionConsecutivePasses",
        "int",
        str(settings.promotion_consecutive_passes),
        "Trade Watchlist icin gereken ardisik basarili arastirma sayisi.",
    ),
    "promotionMinIntervalMinutes": ConfigDefinition(
        "promotionMinIntervalMinutes",
        "int",
        str(settings.promotion_min_interval_minutes),
        "Promotion basarilari arasindaki minimum dakika.",
    ),
    "researchCandidateTtlHours": ConfigDefinition(
        "researchCandidateTtlHours",
        "int",
        str(settings.research_candidate_ttl_hours),
        "Yenilenmeyen arastirma adayinin gecerlilik suresi.",
    ),
    "tradeWatchlistTtlHours": ConfigDefinition(
        "tradeWatchlistTtlHours",
        "int",
        str(settings.trade_watchlist_ttl_hours),
        "Son basarili kontrolden sonra BUY yetkisinin gecerlilik suresi.",
    ),
    "discoveryMinimumVolumeTl": ConfigDefinition(
        "discoveryMinimumVolumeTl",
        "decimal",
        str(settings.discovery_min_volume_tl),
        "Discovery ve promotion icin minimum seans TL hacmi.",
    ),
    "discoveryMaximumSpreadPct": ConfigDefinition(
        "discoveryMaximumSpreadPct",
        "decimal",
        "0.50",
        "Aday icin maksimum spread yuzdesi.",
    ),
}

RISKY_CONFIG_KEYS = {
    "tradingMode",
    "killSwitchEnabled",
    "tradingKillSwitchActive",
    "forceSafeMode",
    "botMode",
    "botEnableRealOrders",
    "botRealLiveModeAllowed",
    "botRealLiveArmed",
    "botRequireDemoAccount",
    "botDemoAccountConfirmed",
    "sizingRiskPerTradePct",
    "sizingMaxCashUtilizationPct",
    "sizingMaxAccountExposurePct",
    "sizingMaxPositionValuePerSymbol",
    "sizingMaxOrderValueTl",
    "sizingMaxQtyPerOrder",
    "sizingMinStopDistancePct",
    "sizingMaxStopDistancePct",
    "sizingMinimumStopSlippagePct",
    "sizingMaximumStopSlippagePct",
    "sizingProfileStopSlippagePct",
    "sizingMaxAccountDataAgeSeconds",
    "sizingMinimumBuyConfidence",
    "sizingMinimumSellConfidence",
    "sizingDailyOrderLimit",
    "sizingPerSymbolDailyOrderLimit",
    "sizingAllowMarginBuying",
    "accountReservationHandling",
    "maxTradeWatchlistSize",
    "minimumTrendPreScore",
    "minimumResearchScore",
    "researchMinimumConfidence",
    "researchMaximumRiskScore",
    "promotionConsecutivePasses",
    "promotionMinIntervalMinutes",
    "tradeWatchlistTtlHours",
    "discoveryMinimumVolumeTl",
    "discoveryMaximumSpreadPct",
}


READ_ONLY_CONFIG_KEYS = frozenset({"botAllowMarketOrders"})

# Symbol allow/deny lists may be submitted empty.  Empty manual BUY lists lift
# only that manual filter: automated BUY still requires an active DB-backed
# trade-watchlist row.  Empty SELL/decline/locked lists mean no extra filter.
# These fields must not carry the HTML ``required`` attribute.
EMPTY_ALLOWED_CONFIG_KEYS = frozenset(
    {
        "allowedSymbols",
        "buyAllowedSymbols",
        "sellExitAllowedSymbols",
        "declineSymbols",
        "lockedLongTermSymbols",
        "scanUniverseSymbols",
    }
)


CONFIG_SECTION_DEFINITIONS = (
    ConfigSectionDefinition(
        title="İşlem modu ve güvenlik kapıları",
        description=(
            "Sistem modu, değerlendirme kill switch'i ve emir dispatch güvenlik "
            "kapıları. Güvenliği gevşeten değişiklikler CONFIRM onayı ister."
        ),
        keys=(
            "tradingMode",
            "killSwitchEnabled",
            "tradingKillSwitchActive",
            "forceSafeMode",
        ),
    ),
    ConfigSectionDefinition(
        title="Sembol yetkileri ve işlem zamanı",
        description=(
            "Genel izleme listesi, BUY ve SELL_EXIT izinleri, uzun vadeli "
            "kilitler ve seans kesim ayarları."
        ),
        keys=(
            "allowedSymbols",
            "buyAllowedSymbols",
            "sellExitAllowedSymbols",
            "declineSymbols",
            "lockedLongTermSymbols",
            "disableTradingAfter",
            "timezone",
        ),
    ),
    ConfigSectionDefinition(
        title="Matriks gateway ve emir izinleri",
        description=(
            "Gateway çalışma modu, demo/gerçek emir kilitleri, hesap onayı ve "
            "HTTP timeout ayarları. MARKET emri kod seviyesinde salt okunurdur."
        ),
        keys=(
            "botMode",
            "botEnableDemoOrders",
            "botEnableRealOrders",
            "botRealLiveModeAllowed",
            "botRealLiveArmed",
            "botRequireDemoAccount",
            "botDemoAccountConfirmed",
            "botHttpTimeoutSeconds",
            "botAllowMarketOrders",
        ),
    ),
    ConfigSectionDefinition(
        title="Matriks piyasa verisi sozlesmesi",
        description=(
            "Hacim ve bar periyodu semantik diagnostikleri. Mum/indikator periyodu "
            "aktif Trade Profile ekranindaki indicator_period alanindan yonetilir."
        ),
        keys=(
            "marketDataDiagnosticsEnabled",
            "marketDataDiagnosticSampleRatePct",
            "marketDataWarningRateLimitSeconds",
        ),
    ),
    ConfigSectionDefinition(
        title="Kesif, arastirma ve islem listesi",
        description=(
            "Genis data-only tarama evreni, AI arastirma butcesi ve Trade "
            "Watchlist promotion/sona erme esikleri. Research Candidate olmak "
            "tek basina emir yetkisi vermez."
        ),
        keys=(
            "scanUniverseSymbols",
            "discoveryIntervalMinutes",
            "maxResearchCandidatesPerCycle",
            "maxActiveResearchSymbols",
            "maxConcurrentResearchEvaluations",
            "candidateCooldownMinutes",
            "maxTradeWatchlistSize",
            "minimumTrendPreScore",
            "minimumResearchScore",
            "researchMinimumConfidence",
            "researchMaximumRiskScore",
            "promotionConsecutivePasses",
            "promotionMinIntervalMinutes",
            "researchCandidateTtlHours",
            "tradeWatchlistTtlHours",
            "discoveryMinimumVolumeTl",
            "discoveryMaximumSpreadPct",
        ),
    ),
    ConfigSectionDefinition(
        title="Deterministik position sizing sınırları",
        description=(
            "Environment ve aktif Trade Profile ile birlikte çözümlenen sistem "
            "geneli lot, nakit, maruziyet, stop, slippage ve günlük limitler."
        ),
        keys=(
            "sizingRiskPerTradePct",
            "sizingMaxCashUtilizationPct",
            "sizingMaxAccountExposurePct",
            "sizingMaxPositionValuePerSymbol",
            "sizingMaxOrderValueTl",
            "sizingMaxQtyPerOrder",
            "sizingMinOrderValueTl",
            "sizingMinStopDistancePct",
            "sizingMaxStopDistancePct",
            "sizingMinimumStopSlippagePct",
            "sizingMaximumStopSlippagePct",
            "sizingProfileStopSlippagePct",
            "sizingMaxAccountDataAgeSeconds",
            "sizingMinimumBuyConfidence",
            "sizingMinimumSellConfidence",
            "sizingDailyOrderLimit",
            "sizingPerSymbolDailyOrderLimit",
        ),
    ),
    ConfigSectionDefinition(
        title="Hesap ve nakit rezervasyonu",
        description=(
            "Margin izni ve broker buying-power rezervasyon semantiği. Hedef "
            "broker alanları doğrulanmadan UNKNOWN değiştirilmemelidir."
        ),
        keys=(
            "sizingAllowMarginBuying",
            "accountReservationHandling",
        ),
    ),
)


@dataclass(frozen=True)
class AdminConfigItem:
    key: str
    value: str
    value_type: str
    description: str
    is_sensitive: bool
    source: str
    updated_at: datetime | None = None

    @property
    def display_value(self) -> str:
        if self.is_sensitive:
            return "********"
        return self.value

    @property
    def requires_confirmation(self) -> bool:
        return self.key in RISKY_CONFIG_KEYS

    @property
    def is_editable(self) -> bool:
        return self.key not in READ_ONLY_CONFIG_KEYS

    @property
    def allow_empty(self) -> bool:
        """Whether this field may be submitted blank (symbol allow/deny lists)."""
        return self.key in EMPTY_ALLOWED_CONFIG_KEYS


@dataclass(frozen=True)
class AdminConfigSection:
    title: str
    description: str
    items: tuple[AdminConfigItem, ...]

    @property
    def requires_confirmation(self) -> bool:
        return any(item.requires_confirmation for item in self.items)

    @property
    def has_editable_items(self) -> bool:
        return any(item.is_editable for item in self.items)


def build_admin_config_sections(
    items: Iterable[AdminConfigItem],
) -> list[AdminConfigSection]:
    """Group every public runtime config for the HTML admin panel.

    The final catch-all section is intentional: a newly introduced
    ``CONFIG_DEFINITIONS`` key remains visible even if its preferred section
    has not been assigned yet.
    """

    by_key = {item.key: item for item in items}
    assigned: set[str] = set()
    sections: list[AdminConfigSection] = []

    for definition in CONFIG_SECTION_DEFINITIONS:
        section_items: list[AdminConfigItem] = []
        for key in definition.keys:
            if key in assigned:
                raise RuntimeError(f"Admin config key appears in two sections: {key}")
            item = by_key.get(key)
            if item is not None:
                assigned.add(key)
                section_items.append(item)
        if section_items:
            sections.append(
                AdminConfigSection(
                    title=definition.title,
                    description=definition.description,
                    items=tuple(section_items),
                )
            )

    remaining = tuple(item for key, item in by_key.items() if key not in assigned)
    if remaining:
        sections.append(
            AdminConfigSection(
                title="Diğer çalışma zamanı ayarları",
                description=(
                    "Henüz özel bir ekran bölümüne atanmamış çalışma zamanı "
                    "ayarları. Bu bölüm yeni anahtarların panelden kaybolmasını önler."
                ),
                items=remaining,
            )
        )
    return sections


def public_config_keys() -> list[str]:
    """Return non-secret config keys in stable display order."""
    return list(CONFIG_DEFINITIONS)


async def list_admin_configs(session: AsyncSession) -> list[AdminConfigItem]:
    rows = await _load_config_rows(session)
    items: list[AdminConfigItem] = []
    for key, definition in CONFIG_DEFINITIONS.items():
        row = rows.get(key)
        value = row.value if row else definition.default
        items.append(
            AdminConfigItem(
                key=key,
                value=value,
                value_type=definition.value_type,
                description=definition.description,
                is_sensitive=definition.is_sensitive,
                source="db" if row else "default",
                updated_at=row.updated_at if row else None,
            )
        )
    return items


async def get_admin_config_value(session: AsyncSession, key: str) -> str:
    _ensure_allowed_key(key)
    stmt = select(SystemConfig).where(SystemConfig.key == key)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row:
        return row.value
    return CONFIG_DEFINITIONS[key].default


async def has_admin_config_row(session: AsyncSession, key: str) -> bool:
    _ensure_allowed_key(key)
    stmt = select(SystemConfig.id).where(SystemConfig.key == key)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def set_admin_config_value(
    session: AsyncSession,
    key: str,
    raw_value: Any,
    *,
    changed_by: str,
    reason: str | None = None,
    confirmation: str | None = None,
    commit: bool = True,
) -> AdminConfigItem:
    """Validate, persist, and audit one admin config value."""
    definition = _ensure_allowed_key(key)
    new_value = _serialize_value(key, raw_value, definition.value_type)
    old_value = await get_admin_config_value(session, key)

    if _requires_confirmation(key, old_value, new_value):
        if confirmation != RISKY_CONFIRMATION:
            raise ValueError(f"{key} requires confirmation={RISKY_CONFIRMATION}")

    stmt = select(SystemConfig).where(SystemConfig.key == key)
    row = (await session.execute(stmt)).scalar_one_or_none()
    if row is None:
        row = SystemConfig(
            key=key,
            value=new_value,
            value_type=definition.value_type,
            description=definition.description,
            is_sensitive=definition.is_sensitive,
        )
        session.add(row)
    else:
        row.value = new_value
        row.value_type = definition.value_type
        row.description = definition.description
        row.is_sensitive = definition.is_sensitive

    if old_value != new_value:
        session.add(
            ConfigAuditLog(
                key=key,
                old_value=old_value,
                new_value=new_value,
                changed_by=changed_by,
                reason=reason or "Admin config update",
            )
        )

    if commit:
        await session.commit()
        if old_value != new_value:
            from app.services.decision_gate import decision_cache

            decision_cache.clear()
        await session.refresh(row)
    else:
        await session.flush()
    return AdminConfigItem(
        key=key,
        value=row.value,
        value_type=row.value_type,
        description=row.description or definition.description,
        is_sensitive=row.is_sensitive,
        source="db",
        updated_at=row.updated_at,
    )


async def set_admin_config_values(
    session: AsyncSession,
    values: dict[str, Any],
    *,
    changed_by: str,
    reason: str | None = None,
    confirmation: str | None = None,
) -> list[AdminConfigItem]:
    """Validate and persist a config snapshot in one DB transaction."""
    if not values:
        raise ValueError("At least one config value is required")
    items: list[AdminConfigItem] = []
    async with session.begin():
        for key, value in values.items():
            items.append(
                await set_admin_config_value(
                    session,
                    key,
                    value,
                    changed_by=changed_by,
                    reason=reason,
                    confirmation=confirmation,
                    commit=False,
                )
            )
    from app.services.decision_gate import decision_cache

    decision_cache.clear()
    return items


async def is_kill_switch_enabled(session: AsyncSession) -> bool:
    return (
        _parse_bool(await get_admin_config_value(session, "killSwitchEnabled"))
        or _parse_bool(await get_admin_config_value(session, "tradingKillSwitchActive"))
        or _parse_bool(await get_admin_config_value(session, "forceSafeMode"))
    )


async def get_trading_mode_override(session: AsyncSession) -> SignalMode | None:
    if not await has_admin_config_row(session, "tradingMode"):
        return None
    value = await get_admin_config_value(session, "tradingMode")
    return SignalMode(value.upper())


async def build_runtime_risk_config(session: AsyncSession) -> RiskConfig:
    """Build RiskConfig from the active trade profile + DB-backed admin
    config, falling back to code defaults where neither applies.

    Priority: active trade profile > per-field admin config override >
    static env default. Symbol lists, cutoff time, and timezone are NOT
    part of a trade profile — they stay admin-config-driven regardless.
    """
    values = {item.key: item.value for item in await list_admin_configs(session)}
    profile = await get_active_profile(session)
    bot_enable_real_orders = _parse_bool(values["botEnableRealOrders"])
    real_live_mode_allowed = _parse_bool(values["botRealLiveModeAllowed"])
    real_live_armed = _parse_bool(values["botRealLiveArmed"])
    return RiskConfig(
        allowed_symbols=values["allowedSymbols"],
        decline_symbols=values.get("declineSymbols", ""),
        locked_long_term_symbols=values["lockedLongTermSymbols"],
        max_position_value_per_symbol=profile.max_position_value_per_symbol,
        max_daily_trade_count=profile.max_orders_per_day,
        min_confidence_for_buy=profile.min_confidence_for_buy,
        min_confidence_for_sell=profile.min_confidence_for_sell,
        allow_sell_long_term=profile.allow_sell_long_term,
        allow_short_selling=profile.allow_short_selling,
        require_alpha_trend_alignment=profile.require_alpha_trend_alignment,
        require_indicator_consensus_alignment=(
            profile.require_indicator_consensus_alignment
        ),
        min_indicator_consensus_count=risk_config.min_indicator_consensus_count,
        max_natr_for_buy=profile.max_natr_for_buy,
        max_depth_queue_drop_pct_for_buy=profile.max_depth_queue_drop_pct_for_buy,
        max_spread_pct_for_buy=profile.max_spread_pct_for_buy,
        min_depth_bid_ask_ratio_top10_for_buy=profile.min_depth_bid_ask_ratio_top10_for_buy,
        max_depth_sell_pressure_score_for_buy=profile.max_depth_sell_pressure_score_for_buy,
        block_buy_on_strong_sell_pressure=profile.block_buy_on_strong_sell_pressure,
        block_buy_on_near_ask_wall=profile.block_buy_on_near_ask_wall,
        near_wall_distance_pct=profile.near_wall_distance_pct,
        real_live_mode_allowed=(
            profile.allow_real_live
            and bot_enable_real_orders
            and real_live_mode_allowed
            and real_live_armed
        ),
        demo_live_mode_allowed=profile.allow_demo_live,
        disable_trading_after=values["disableTradingAfter"],
        timezone=values["timezone"],
        _env_file="",
    )


def _ensure_allowed_key(key: str) -> ConfigDefinition:
    if key in SECRET_CONFIG_KEYS or key not in CONFIG_DEFINITIONS:
        raise ValueError(f"Unsupported admin config key: {key}")
    definition = CONFIG_DEFINITIONS[key]
    if definition.is_sensitive:
        raise ValueError(f"Sensitive admin config key cannot be exposed: {key}")
    return definition


async def _load_config_rows(session: AsyncSession) -> dict[str, SystemConfig]:
    stmt = select(SystemConfig).where(SystemConfig.key.in_(public_config_keys()))
    rows = (await session.execute(stmt)).scalars().all()
    return {row.key: row for row in rows if row.key in CONFIG_DEFINITIONS}


def _serialize_value(key: str, raw_value: Any, value_type: str) -> str:
    if value_type == "bool":
        value = _parse_bool(raw_value)
        if key == "botAllowMarketOrders" and value:
            raise ValueError(
                "botAllowMarketOrders=true is not allowed; MARKET orders are disabled"
            )
        return str(value).lower()
    if value_type == "int":
        value = int(raw_value)
        if value < 0:
            raise ValueError(f"{key} must be >= 0")
        if key == "marketDataWarningRateLimitSeconds" and not 1 <= value <= 3600:
            raise ValueError(f"{key} must be between 1 and 3600")
        return str(value)
    if value_type == "float":
        value = float(raw_value)
        if value < 0:
            raise ValueError(f"{key} must be >= 0")
        return str(value)
    if value_type == "decimal":
        try:
            value = (
                raw_value if isinstance(raw_value, Decimal) else Decimal(str(raw_value))
            )
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"{key} must be a decimal number") from exc
        if not value.is_finite() or value < 0:
            raise ValueError(f"{key} must be a finite value >= 0")
        if key == "marketDataDiagnosticSampleRatePct" and value > 100:
            raise ValueError(f"{key} must be <= 100")
        return str(value)
    if value_type == "mode":
        value = str(raw_value).upper()
        SignalMode(value)
        return value
    if value_type == "reservation_handling":
        value = str(raw_value).strip().upper()
        if value not in {
            "BROKER_ALREADY_DEDUCTED",
            "BACKEND_DEDUCTED",
            "UNKNOWN",
        }:
            raise ValueError(f"{key} has an invalid reservation handling policy")
        return value
    if value_type == "time":
        value = str(raw_value).strip()
        hour, minute = value.split(":", 1)
        if len(hour) != 2 or len(minute) != 2:
            raise ValueError(f"{key} must be HH:MM")
        hour_int = int(hour)
        minute_int = int(minute)
        if hour_int < 0 or hour_int > 23 or minute_int < 0 or minute_int > 59:
            raise ValueError(f"{key} must be HH:MM")
        return f"{hour_int:02d}:{minute_int:02d}"
    if value_type == "timezone":
        value = str(raw_value).strip()
        ZoneInfo(value)
        return value
    if value_type == "time_in_force":
        value = str(raw_value).strip()
        normalized = value.replace("_", "").replace("-", "").replace(" ", "").lower()
        if normalized in {"day", "d"}:
            return "Day"
        if normalized in {"gtc", "goodtillcancel", "goodtilcancel"}:
            return "GoodTillCancel"
        raise ValueError(f"{key} must be Day or GoodTillCancel")
    if value_type == "symbol_period":
        value = str(raw_value).strip()
        allowed = {
            "min": "Min",
            "min5": "Min5",
            "min15": "Min15",
            "min30": "Min30",
            "hour": "Hour",
            "day": "Day",
        }
        normalized = value.replace("_", "").replace("-", "").replace(" ", "").lower()
        if normalized not in allowed:
            raise ValueError(f"{key} must be one of Min, Min5, Min15, Min30, Hour, Day")
        return allowed[normalized]

    value = str(raw_value).strip()
    if key in {
        "allowedSymbols",
        "declineSymbols",
        "buyAllowedSymbols",
        "sellExitAllowedSymbols",
        "lockedLongTermSymbols",
        "scanUniverseSymbols",
    }:
        return ",".join(
            symbol.strip().upper() for symbol in value.split(",") if symbol.strip()
        )
    return value


def _parse_bool(raw_value: Any) -> bool:
    if isinstance(raw_value, bool):
        return raw_value
    value = str(raw_value).strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"Invalid boolean value: {raw_value}")


def _requires_confirmation(key: str, old_value: str, new_value: str) -> bool:
    if key not in RISKY_CONFIG_KEYS:
        return False
    if key == "tradingMode":
        return (
            new_value
            in {
                SignalMode.LIVE.value,
                SignalMode.DEMO_LIVE.value,
                SignalMode.REAL_LIVE.value,
            }
            and old_value != new_value
        )
    if key in {"killSwitchEnabled", "tradingKillSwitchActive", "forceSafeMode"}:
        return _parse_bool(old_value) is True and _parse_bool(new_value) is False
    if key == "botMode":
        return (
            new_value
            in {
                SignalMode.DEMO_LIVE.value,
                SignalMode.REAL_LIVE.value,
            }
            and old_value != new_value
        )
    if key in {
        "botEnableRealOrders",
        "botRealLiveModeAllowed",
        "botRealLiveArmed",
        "botDemoAccountConfirmed",
        "sizingAllowMarginBuying",
    }:
        return _parse_bool(new_value) is True and old_value != new_value
    if key == "botRequireDemoAccount":
        return _parse_bool(old_value) is True and _parse_bool(new_value) is False
    if key == "accountReservationHandling":
        return old_value == "UNKNOWN" and new_value != "UNKNOWN"
    increase_is_risky = {
        "sizingRiskPerTradePct",
        "sizingMaxCashUtilizationPct",
        "sizingMaxAccountExposurePct",
        "sizingMaxPositionValuePerSymbol",
        "sizingMaxOrderValueTl",
        "sizingMaxQtyPerOrder",
        "sizingMaxStopDistancePct",
        "sizingMaxAccountDataAgeSeconds",
        "sizingDailyOrderLimit",
        "sizingPerSymbolDailyOrderLimit",
    }
    decrease_is_risky = {
        "sizingMinStopDistancePct",
        "sizingMinimumStopSlippagePct",
        "sizingMaximumStopSlippagePct",
        "sizingProfileStopSlippagePct",
        "sizingMinimumBuyConfidence",
        "sizingMinimumSellConfidence",
    }
    old_decimal = Decimal(old_value)
    new_decimal = Decimal(new_value)
    if key in increase_is_risky:
        return new_decimal > old_decimal
    if key in decrease_is_risky:
        return new_decimal < old_decimal
    return False
