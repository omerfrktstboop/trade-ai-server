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
        "Sistem geneli çalışma modu; tüm değerlendirmelerde istek modunu ezer. PAPER=simülasyon, DEMO_LIVE=demo hesapla gerçek emir.",
    ),
    "killSwitchEnabled": ConfigDefinition(
        "killSwitchEnabled",
        "bool",
        "false",
        "Acil durdurma: açıkken tüm sinyal değerlendirmeleri WAIT döner, hiçbir emir gönderilmez.",
    ),
    "botMode": ConfigDefinition(
        "botMode",
        "mode",
        "PAPER",
        "Matriks botun çalışma modu. Riskli modlara geçiş CONFIRM onayı ister.",
    ),
    "botEnableDemoOrders": ConfigDefinition(
        "botEnableDemoOrders",
        "bool",
        "false",
        "Matriks botun demo hesaba emir göndermesine izin verir.",
    ),
    "botEnableRealOrders": ConfigDefinition(
        "botEnableRealOrders",
        "bool",
        "false",
        "Matriks botun gerçek hesaba emir göndermesine izin verir.",
    ),
    "tradingKillSwitchActive": ConfigDefinition(
        "tradingKillSwitchActive",
        "bool",
        "false",
        "Açıkken tüm emir gönderim yolları kapanır; analiz devam eder.",
    ),
    "forceSafeMode": ConfigDefinition(
        "forceSafeMode",
        "bool",
        "false",
        "Açıkken analiz devam eder ama hiçbir emir gönderilmez (güvenli mod).",
    ),
    # ── v2 mod katmanı (Faz 4) — eski mod anahtarlarıyla PARALEL çalışır;
    # geçiş döneminde dispatch için eski VE yeni kapılar birlikte açık olmalı.
    "systemMode": ConfigDefinition(
        "systemMode",
        "system_mode",
        "OBSERVE_ONLY",
        "v2 çalışma modu. OBSERVE_ONLY: analiz ve karar üretilir, hiçbir emir "
        "gönderilmez. AUTO_TRADE: Matriks'te oturum açık hesaba emir gönderilir "
        "(DEMO hesap otomatik serbest; REAL hesap ayrıca arming ister).",
    ),
    "realAccountArmed": ConfigDefinition(
        "realAccountArmed",
        "bool",
        "false",
        "REAL hesapta emir gönderimini kurar. Yalnızca arming endpoint'i "
        "'CONFIRM REAL ACCOUNT' onayıyla yazar; hesap/oturum değişince "
        "account watcher otomatik düşürür.",
    ),
    "armedAccountRef": ConfigDefinition(
        "armedAccountRef",
        "string",
        "",
        "Arm edilen hesabın sha256 referansı — gateway'in verdiği değer "
        "DOĞRUDAN saklanır (yeniden hash yok). Sadece arming akışı yazar.",
    ),
    "aiToolCallingEnabled": ConfigDefinition(
        "aiToolCallingEnabled",
        "bool",
        str(settings.ai_tools_enabled).lower(),
        "DeepSeek'in değerlendirme sırasında read-only veri araçlarını "
        "çağırmasına izin verir (panel > env; .env AI_TOOLS_ENABLED fallback).",
    ),
    "dailyMaxLossTl": ConfigDefinition(
        "dailyMaxLossTl",
        "decimal",
        "0",
        "Günlük maksimum zarar (TL). Aşılınca YENİ BUY'lar bloklanır; SELL ve "
        "stop-loss bekçisi asla etkilenmez. 0 = devre dışı. Realized zarar tek "
        "başına aşarsa veri boşluğunda bile bloklar (fail-closed).",
    ),
    "significancePriceMovePct": ConfigDefinition(
        "significancePriceMovePct",
        "decimal",
        "1.5",
        "Portföy taramasında AI'ı tetikleyen fiyat hareketi eşiği (yüzde). "
        "Pozisyon varken eşiğin 2/3'ü uygulanır.",
    ),
    "scannerEnabled": ConfigDefinition(
        "scannerEnabled",
        "bool",
        "true",
        "Tarama döngüsünü çalıştırır. Kapatılırsa AI değerlendirmesi VE stop-loss "
        "bekçisi dahil tüm otomasyon durur — kapatmak onay ister.",
    ),
    "scannerAllowOrders": ConfigDefinition(
        "scannerAllowOrders",
        "bool",
        str(settings.scanner_allow_orders).lower(),
        "Tarama kararlarının gerçek emre dönüşmesine izin verir. Kapalıyken tüm "
        "kararlar PAPER'a sabitlenir, emir yolu tamamen kapalıdır.",
    ),
    "manualApprovalAllowOrders": ConfigDefinition(
        "manualApprovalAllowOrders",
        "bool",
        str(settings.manual_approval_allow_orders).lower(),
        "Manuel onay kuyruğundan onaylanan emirlerin gönderilmesine izin verir.",
    ),
    "portfolioScanIntervalMinutes": ConfigDefinition(
        "portfolioScanIntervalMinutes",
        "int",
        str(settings.portfolio_scan_interval_minutes),
        "Eldeki pozisyonların AI ile yeniden değerlendirilme aralığı (dakika, en az 5).",
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
        "REAL_LIVE modunun backend tarafından kullanılabilmesine izin verir.",
    ),
    "botRealLiveArmed": ConfigDefinition(
        "botRealLiveArmed",
        "bool",
        "false",
        "REAL_LIVE emir yolunu bilinçli olarak kurar; tek başına emir yetkisi vermez.",
    ),
    "botRequireDemoAccount": ConfigDefinition(
        "botRequireDemoAccount",
        "bool",
        "true",
        "Emir öncesi demo hesap doğrulaması zorunluluğunu belirler.",
    ),
    "botDemoAccountConfirmed": ConfigDefinition(
        "botDemoAccountConfirmed",
        "bool",
        "false",
        "Matriks tarafında demo hesap kullanıldığını onaylar.",
    ),
    "botAllowMarketOrders": ConfigDefinition(
        "botAllowMarketOrders",
        "bool",
        "false",
        "MARKET emirleri sistem genelinde yasaktır; açılamaz (kod kilidi).",
    ),
    "botHttpTimeoutSeconds": ConfigDefinition(
        "botHttpTimeoutSeconds",
        "int",
        "15",
        "Matriks bot HTTP istek zaman aşımı (saniye).",
    ),
    "sizingRiskPerTradePct": ConfigDefinition(
        "sizingRiskPerTradePct",
        "decimal",
        "0.50",
        "İşlem başına riske edilecek özkaynak yüzdesi.",
    ),
    "sizingMaxCashUtilizationPct": ConfigDefinition(
        "sizingMaxCashUtilizationPct",
        "decimal",
        "25",
        "Tek seferde kullanılabilecek en fazla nakit yüzdesi.",
    ),
    "sizingMaxAccountExposurePct": ConfigDefinition(
        "sizingMaxAccountExposurePct",
        "decimal",
        "50",
        "Hesap genelinde izin verilen en fazla toplam pozisyon yüzdesi.",
    ),
    "sizingMaxPositionValuePerSymbol": ConfigDefinition(
        "sizingMaxPositionValuePerSymbol",
        "decimal",
        "3000",
        "Sembol başına izin verilen en fazla pozisyon değeri (TL).",
    ),
    "sizingMaxOrderValueTl": ConfigDefinition(
        "sizingMaxOrderValueTl",
        "decimal",
        "1000",
        "Tek emirde izin verilen en fazla tutar (TL).",
    ),
    "sizingMaxQtyPerOrder": ConfigDefinition(
        "sizingMaxQtyPerOrder",
        "int",
        "3",
        "Tek emirde izin verilen en fazla lot adedi (tam sayı).",
    ),
    "sizingMinOrderValueTl": ConfigDefinition(
        "sizingMinOrderValueTl",
        "decimal",
        "1",
        "Bir emrin gönderilebilmesi için gereken en düşük tutar (TL).",
    ),
    "sizingMinStopDistancePct": ConfigDefinition(
        "sizingMinStopDistancePct",
        "decimal",
        "0.10",
        "Giriş fiyatı ile stop arasındaki en düşük mesafe yüzdesi.",
    ),
    "sizingMaxStopDistancePct": ConfigDefinition(
        "sizingMaxStopDistancePct",
        "decimal",
        "10",
        "Giriş fiyatı ile stop arasındaki en yüksek mesafe yüzdesi.",
    ),
    "sizingMinimumStopSlippagePct": ConfigDefinition(
        "sizingMinimumStopSlippagePct",
        "decimal",
        "0.05",
        "Stop kayması için ayrılan en düşük tampon yüzdesi.",
    ),
    "sizingMaximumStopSlippagePct": ConfigDefinition(
        "sizingMaximumStopSlippagePct",
        "decimal",
        "1",
        "Stop kayması için ayrılan en yüksek tampon yüzdesi.",
    ),
    "sizingProfileStopSlippagePct": ConfigDefinition(
        "sizingProfileStopSlippagePct",
        "decimal",
        "0.20",
        "Sistem geneli tercih edilen stop kayma tamponu yüzdesi.",
    ),
    "sizingMaxAccountDataAgeSeconds": ConfigDefinition(
        "sizingMaxAccountDataAgeSeconds",
        "decimal",
        "60",
        "Hesap verisinin emir boyutlamada kullanılabileceği en fazla yaş (saniye).",
    ),
    "sizingMinimumBuyConfidence": ConfigDefinition(
        "sizingMinimumBuyConfidence",
        "decimal",
        "75",
        "BUY emri için gereken en düşük AI güven puanı.",
    ),
    "sizingMinimumSellConfidence": ConfigDefinition(
        "sizingMinimumSellConfidence",
        "decimal",
        "70",
        "SELL emri için gereken en düşük AI güven puanı.",
    ),
    "sizingDailyOrderLimit": ConfigDefinition(
        "sizingDailyOrderLimit",
        "int",
        "3",
        "Günlük toplam emir limiti (tüm semboller).",
    ),
    "sizingPerSymbolDailyOrderLimit": ConfigDefinition(
        "sizingPerSymbolDailyOrderLimit",
        "int",
        "1",
        "Sembol başına günlük emir limiti.",
    ),
    "sizingAllowMarginBuying": ConfigDefinition(
        "sizingAllowMarginBuying",
        "bool",
        "false",
        "Krediyle (marjin) alıma izin verir; ortam, sistem ve profil birlikte izin vermeli.",
    ),
    "accountReservationHandling": ConfigDefinition(
        "accountReservationHandling",
        "reservation_handling",
        "UNKNOWN",
        "Aracı kurumun kullanılabilir bakiyesi bekleyen BUY emirlerini düşüyor mu bilgisi.",
    ),
    "marketDataDiagnosticsEnabled": ConfigDefinition(
        "marketDataDiagnosticsEnabled",
        "bool",
        "false",
        "Hacim/periyot verisi tutarlılık loglarını açar (teşhis amaçlı).",
    ),
    "marketDataDiagnosticSampleRatePct": ConfigDefinition(
        "marketDataDiagnosticSampleRatePct",
        "decimal",
        "10",
        "Teşhis loglarında örneklenecek sembol yüzdesi (0-100).",
    ),
    "marketDataWarningRateLimitSeconds": ConfigDefinition(
        "marketDataWarningRateLimitSeconds",
        "int",
        "60",
        "Aynı sembol/alan için iki uyarı arasındaki en kısa süre (saniye).",
    ),
    "scanUniverseSymbols": ConfigDefinition(
        "scanUniverseSymbols",
        "string",
        settings.discovery_symbols,
        "Araştırma için taranan geniş BIST pay evreni; emir izni vermez.",
    ),
    "discoveryIntervalMinutes": ConfigDefinition(
        "discoveryIntervalMinutes",
        "int",
        str(settings.discovery_interval_minutes),
        "Kural tabanlı piyasa keşif turları arasındaki süre (dakika).",
    ),
    "maxResearchCandidatesPerCycle": ConfigDefinition(
        "maxResearchCandidatesPerCycle",
        "int",
        str(settings.max_research_candidates_per_cycle),
        "Bir turda AI araştırmasına alınabilecek en fazla aday sayısı.",
    ),
    "maxActiveResearchSymbols": ConfigDefinition(
        "maxActiveResearchSymbols",
        "int",
        str(settings.max_active_research_symbols),
        "Gateway veri aboneliğindeki en fazla aktif araştırma sembolü.",
    ),
    "maxConcurrentResearchEvaluations": ConfigDefinition(
        "maxConcurrentResearchEvaluations",
        "int",
        str(settings.max_concurrent_research_evaluations),
        "Aynı anda çalışabilecek en fazla AI araştırma değerlendirmesi.",
    ),
    "candidateCooldownMinutes": ConfigDefinition(
        "candidateCooldownMinutes",
        "int",
        str(settings.candidate_cooldown_minutes),
        "Aynı adayın iki AI değerlendirmesi arasındaki en kısa süre (dakika).",
    ),
    "maxTradeWatchlistSize": ConfigDefinition(
        "maxTradeWatchlistSize",
        "int",
        str(settings.max_trade_watchlist_size),
        "Otomatik BUY değerlendirmesine açık en fazla sembol sayısı.",
    ),
    "minimumTrendPreScore": ConfigDefinition(
        "minimumTrendPreScore",
        "decimal",
        "60",
        "Araştırma adayı ön eleme puanı (trend skoru alt sınırı).",
    ),
    "minimumResearchScore": ConfigDefinition(
        "minimumResearchScore",
        "decimal",
        "75",
        "Trade Watchlist'e terfi için gereken AI araştırma puanı.",
    ),
    "researchMinimumConfidence": ConfigDefinition(
        "researchMinimumConfidence",
        "decimal",
        "75",
        "Terfi için gereken en düşük AI güven puanı.",
    ),
    "researchMaximumRiskScore": ConfigDefinition(
        "researchMaximumRiskScore",
        "decimal",
        "35",
        "Terfi için izin verilen en yüksek AI risk puanı.",
    ),
    "promotionConsecutivePasses": ConfigDefinition(
        "promotionConsecutivePasses",
        "int",
        str(settings.promotion_consecutive_passes),
        "Terfi için gereken ardışık başarılı araştırma sayısı.",
    ),
    "promotionMinIntervalMinutes": ConfigDefinition(
        "promotionMinIntervalMinutes",
        "int",
        str(settings.promotion_min_interval_minutes),
        "İki başarılı araştırma arasında geçmesi gereken en kısa süre (dakika).",
    ),
    "researchCandidateTtlHours": ConfigDefinition(
        "researchCandidateTtlHours",
        "int",
        str(settings.research_candidate_ttl_hours),
        "Yenilenmeyen araştırma adayının geçerlilik süresi (saat).",
    ),
    "tradeWatchlistTtlHours": ConfigDefinition(
        "tradeWatchlistTtlHours",
        "int",
        str(settings.trade_watchlist_ttl_hours),
        "Son başarılı kontrolden sonra BUY yetkisinin geçerlilik süresi (saat).",
    ),
    "discoveryMinimumVolumeTl": ConfigDefinition(
        "discoveryMinimumVolumeTl",
        "decimal",
        str(settings.discovery_min_volume_tl),
        "Keşif ve terfi için gereken en düşük seans hacmi (TL).",
    ),
    "discoveryMaximumSpreadPct": ConfigDefinition(
        "discoveryMaximumSpreadPct",
        "decimal",
        "0.50",
        "Aday için izin verilen en yüksek alış-satış makası yüzdesi.",
    ),
    "commissionBps": ConfigDefinition(
        "commissionBps",
        "decimal",
        "0",
        "Fill başına komisyon oranı (baz puan, 1bps = %0.01). 0 = komisyon hesaba katılmaz.",
    ),
    "exchangeFeeBps": ConfigDefinition(
        "exchangeFeeBps",
        "decimal",
        "0",
        "Fill başına borsa payı oranı (baz puan). 0 = ücret hesaba katılmaz.",
    ),
    "otherFeeBps": ConfigDefinition(
        "otherFeeBps",
        "decimal",
        "0",
        "Fill başına diğer işlem ücretleri oranı (baz puan). 0 = ücret hesaba katılmaz.",
    ),
    "minimumCommissionTl": ConfigDefinition(
        "minimumCommissionTl",
        "decimal",
        "0",
        "Komisyon oranı sıfırdan büyükken uygulanan en düşük komisyon tutarı (TL).",
    ),
    "outcomeMaximumObservationDelaySeconds": ConfigDefinition(
        "outcomeMaximumObservationDelaySeconds",
        "int",
        "120",
        "Bir horizon hedef zamanından sonra kabul edilen en fazla gözlem gecikmesi (sn).",
    ),
    "stopGuardMaximumQuoteAgeSeconds": ConfigDefinition(
        "stopGuardMaximumQuoteAgeSeconds",
        "int",
        "30",
        "Stop-loss bekçisinin bir fiyatı tetikleyici kabul edebileceği en fazla yaş (sn).",
    ),
    "depthTimestampFixConfirmed": ConfigDefinition(
        "depthTimestampFixConfirmed",
        "bool",
        "false",
        "matriks/TradeAiGateway.cs'teki ReadDepthSnapshot derinlik-tazelik "
        "kaynak düzeltmesi Matriks IQ içinde algoritma yeniden yüklendikten "
        "SONRA operatör tarafından elle onaylanmalı. Onaylanmadan "
        "demo_live_readiness bunu hâlâ bir engel olarak raporlar.",
    ),
    "marketSessionCloseTime": ConfigDefinition(
        "marketSessionCloseTime",
        "time",
        "18:00",
        "Seans kapanış saati (HH:MM). EOD getirisi bu saatteki doğrulanmış son "
        "seans fiyatından hesaplanır; işlem kesim saatinden (disableTradingAfter) "
        "bağımsızdır.",
    ),
}

RISKY_CONFIG_KEYS = {
    "tradingMode",
    "systemMode",
    "realAccountArmed",
    "dailyMaxLossTl",
    "killSwitchEnabled",
    "tradingKillSwitchActive",
    "forceSafeMode",
    "scannerEnabled",
    "scannerAllowOrders",
    "manualApprovalAllowOrders",
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

# realAccountArmed/armedAccountRef panelin genel edit formundan yazılamaz —
# tek yazma yolu arming endpoint'leridir (CONFIRM REAL ACCOUNT + canlı hesap
# doğrulaması). botAllowMarketOrders kod kilididir.
READ_ONLY_CONFIG_KEYS = frozenset(
    {"botAllowMarketOrders", "realAccountArmed", "armedAccountRef"}
)

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
            "Sistem modu, acil durdurma anahtarları ve emir gönderim güvenlik "
            "kapıları. Güvenliği gevşeten değişiklikler CONFIRM onayı ister."
        ),
        keys=(
            "systemMode",
            "tradingMode",
            "killSwitchEnabled",
            "tradingKillSwitchActive",
            "forceSafeMode",
            "scannerEnabled",
            "scannerAllowOrders",
            "manualApprovalAllowOrders",
            "aiToolCallingEnabled",
            "dailyMaxLossTl",
            "significancePriceMovePct",
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
            "realAccountArmed",
            "armedAccountRef",
            "botHttpTimeoutSeconds",
            "botAllowMarketOrders",
        ),
    ),
    ConfigSectionDefinition(
        title="Matriks piyasa verisi teşhisleri",
        description=(
            "Hacim ve bar periyodu tutarlılık teşhis logları. Mum/indikatör periyodu "
            "aktif Trade Profile ekranındaki indicator_period alanından yönetilir."
        ),
        keys=(
            "marketDataDiagnosticsEnabled",
            "marketDataDiagnosticSampleRatePct",
            "marketDataWarningRateLimitSeconds",
        ),
    ),
    ConfigSectionDefinition(
        title="Keşif, araştırma ve işlem listesi",
        description=(
            "Geniş data-only tarama evreni, AI araştırma bütçesi ve Trade "
            "Watchlist terfi/sona erme eşikleri. Araştırma adayı olmak "
            "tek başına emir yetkisi vermez."
        ),
        keys=(
            "scanUniverseSymbols",
            "discoveryIntervalMinutes",
            "portfolioScanIntervalMinutes",
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
        title="Deterministik emir boyutlama sınırları",
        description=(
            "Ortam değişkenleri ve aktif Trade Profile ile birlikte çözümlenen "
            "sistem geneli lot, nakit, maruziyet, stop, kayma ve günlük limitler. "
            "Her alanda üç kaynaktan en sıkısı geçerli olur."
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
            "Marjin izni ve aracı kurum bakiye/rezervasyon davranışı. Aracı kurum "
            "alanları doğrulanmadan UNKNOWN değeri değiştirilmemelidir."
        ),
        keys=(
            "sizingAllowMarginBuying",
            "accountReservationHandling",
        ),
    ),
    ConfigSectionDefinition(
        title="Maliyet ve komisyon parametreleri",
        description=(
            "Gerçekleşen fill'lerden net kâr/zarar hesaplanırken kullanılan komisyon "
            "ve ücret oranları. Varsayılan 0 - hiçbiri girilmezse maliyet hesaba "
            "katılmaz ve mevcut davranış değişmez."
        ),
        keys=(
            "commissionBps",
            "exchangeFeeBps",
            "otherFeeBps",
            "minimumCommissionTl",
        ),
    ),
    ConfigSectionDefinition(
        title="Ölçüm güvenilirliği",
        description=(
            "Outcome labeler'ın gözlem penceresi ve stop-loss bekçisinin kabul "
            "ettiği en fazla fiyat yaşı. Ölçüm/tetikleme kalitesini etkiler, "
            "emir gönderme mantığını gevşetmez."
        ),
        keys=(
            "outcomeMaximumObservationDelaySeconds",
            "stopGuardMaximumQuoteAgeSeconds",
            "marketSessionCloseTime",
            "depthTimestampFixConfirmed",
        ),
    ),
)


# Admin panelde anahtar adı yerine gösterilen Türkçe, insan-okur etiketler.
# Sözlükte olmayan bir anahtar panelden kaybolmaz — etiketi anahtarın kendisi olur.
CONFIG_LABELS: dict[str, str] = {
    "allowedSymbols": "İzinli Semboller (BUY beyaz liste)",
    "declineSymbols": "Yasaklı Semboller (kara liste)",
    "lockedLongTermSymbols": "Uzun Vade Kilitli Semboller",
    "disableTradingAfter": "İşlem Kesim Saati",
    "timezone": "Saat Dilimi",
    "tradingMode": "İşlem Modu",
    "killSwitchEnabled": "Acil Durdurma (Kill Switch)",
    "botMode": "Matriks Bot Modu",
    "botEnableDemoOrders": "Demo Emirlere İzin",
    "botEnableRealOrders": "Gerçek Emirlere İzin",
    "tradingKillSwitchActive": "Emir Yolu Kill Switch",
    "forceSafeMode": "Güvenli Mod Zorla",
    "scannerEnabled": "Tarayıcı Aktif",
    "scannerAllowOrders": "Tarayıcı Emir Gönderebilir",
    "manualApprovalAllowOrders": "Manuel Onaylı Emirlere İzin",
    "portfolioScanIntervalMinutes": "Portföy Tarama Aralığı (dk)",
    "buyAllowedSymbols": "BUY İzinli Semboller (gateway)",
    "sellExitAllowedSymbols": "SELL Çıkış İzinli Semboller",
    "botRealLiveModeAllowed": "REAL_LIVE Moduna İzin",
    "botRealLiveArmed": "REAL_LIVE Emir Yolu Kurulu",
    "botRequireDemoAccount": "Demo Hesap Zorunlu",
    "botDemoAccountConfirmed": "Demo Hesap Onaylandı",
    "botAllowMarketOrders": "MARKET Emirlere İzin (kilitli)",
    "botHttpTimeoutSeconds": "Bot HTTP Zaman Aşımı (sn)",
    "sizingRiskPerTradePct": "İşlem Başına Risk (%)",
    "sizingMaxCashUtilizationPct": "En Fazla Nakit Kullanımı (%)",
    "sizingMaxAccountExposurePct": "En Fazla Hesap Maruziyeti (%)",
    "sizingMaxPositionValuePerSymbol": "Sembol Başına En Fazla Pozisyon (TL)",
    "sizingMaxOrderValueTl": "Emir Başına En Fazla Tutar (TL)",
    "sizingMaxQtyPerOrder": "Emir Başına En Fazla Lot",
    "sizingMinOrderValueTl": "En Düşük Emir Tutarı (TL)",
    "sizingMinStopDistancePct": "En Düşük Stop Mesafesi (%)",
    "sizingMaxStopDistancePct": "En Yüksek Stop Mesafesi (%)",
    "sizingMinimumStopSlippagePct": "En Düşük Stop Kayma Tamponu (%)",
    "sizingMaximumStopSlippagePct": "En Yüksek Stop Kayma Tamponu (%)",
    "sizingProfileStopSlippagePct": "Tercih Edilen Stop Kayma Tamponu (%)",
    "sizingMaxAccountDataAgeSeconds": "Hesap Verisi Azami Yaşı (sn)",
    "sizingMinimumBuyConfidence": "BUY İçin En Düşük AI Güveni",
    "sizingMinimumSellConfidence": "SELL İçin En Düşük AI Güveni",
    "sizingDailyOrderLimit": "Günlük Toplam Emir Limiti",
    "sizingPerSymbolDailyOrderLimit": "Sembol Başına Günlük Emir Limiti",
    "sizingAllowMarginBuying": "Krediyle (Marjin) Alıma İzin",
    "accountReservationHandling": "Bakiye Rezervasyon Davranışı",
    "marketDataDiagnosticsEnabled": "Veri Teşhis Logları",
    "marketDataDiagnosticSampleRatePct": "Teşhis Örnekleme Oranı (%)",
    "marketDataWarningRateLimitSeconds": "Uyarı Sıklık Sınırı (sn)",
    "scanUniverseSymbols": "Tarama Evreni Sembolleri",
    "discoveryIntervalMinutes": "Keşif Turu Aralığı (dk)",
    "maxResearchCandidatesPerCycle": "Tur Başına En Fazla Araştırma Adayı",
    "maxActiveResearchSymbols": "En Fazla Aktif Araştırma Sembolü",
    "maxConcurrentResearchEvaluations": "Eşzamanlı AI Araştırma Sayısı",
    "candidateCooldownMinutes": "Aday Bekleme Süresi (dk)",
    "maxTradeWatchlistSize": "İşlem Listesi Azami Boyutu",
    "minimumTrendPreScore": "Ön Eleme Trend Puanı (alt sınır)",
    "minimumResearchScore": "Terfi İçin Araştırma Puanı",
    "researchMinimumConfidence": "Terfi İçin En Düşük AI Güveni",
    "researchMaximumRiskScore": "Terfi İçin En Yüksek Risk Puanı",
    "promotionConsecutivePasses": "Terfi İçin Ardışık Başarı Sayısı",
    "promotionMinIntervalMinutes": "Terfi Başarıları Arası Süre (dk)",
    "researchCandidateTtlHours": "Araştırma Adayı Geçerliliği (saat)",
    "tradeWatchlistTtlHours": "BUY Yetkisi Geçerliliği (saat)",
    "discoveryMinimumVolumeTl": "En Düşük Seans Hacmi (TL)",
    "discoveryMaximumSpreadPct": "En Yüksek Alış-Satış Makası (%)",
    "commissionBps": "Komisyon Oranı (bps)",
    "exchangeFeeBps": "Borsa Payı Oranı (bps)",
    "otherFeeBps": "Diğer Ücret Oranı (bps)",
    "minimumCommissionTl": "En Düşük Komisyon Tutarı (TL)",
    "outcomeMaximumObservationDelaySeconds": "Outcome Gözlem Azami Gecikmesi (sn)",
    "stopGuardMaximumQuoteAgeSeconds": "Stop Guard Azami Fiyat Yaşı (sn)",
    "marketSessionCloseTime": "Seans Kapanış Saati (EOD)",
    "depthTimestampFixConfirmed": "Depth Zaman Damgası Düzeltmesi Onaylandı (Matriks IQ reload sonrası)",
}


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
    def label(self) -> str:
        """Panelde gösterilen Türkçe etiket; tanımsızsa anahtarın kendisi."""
        return CONFIG_LABELS.get(self.key, self.key)

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
