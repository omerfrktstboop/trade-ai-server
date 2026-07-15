"""Admin config definitions: the ConfigDefinition table, section grouping
for the HTML admin panel, and the AdminConfigItem/AdminConfigSection
display shapes.

No DB access here — see store.py for reads/writes and validation.py for
the CONFIRM-required-change / value-serialization rules.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.config import settings
from app.core.risk_config import risk_config


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
