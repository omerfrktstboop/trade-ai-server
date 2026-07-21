"""Admin config definitions: the ConfigDefinition table, section grouping
for the HTML admin panel, and the AdminConfigItem/AdminConfigSection
display shapes.

No DB access here - see store.py for reads/writes and validation.py for
value-serialization rules.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from app.config import settings
from app.core.risk_config import risk_config


SECRET_CONFIG_KEYS = {"API_TOKEN", "DEEPSEEK_API_KEY", "DATABASE_URL"}


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
    "killSwitchEnabled": ConfigDefinition(
        "killSwitchEnabled",
        "bool",
        "false",
        "Acil durdurma: açıkken tüm sinyal değerlendirmeleri WAIT döner, hiçbir emir gönderilmez.",
    ),
    # ── v2 mod katmanı: tek çalışma modu anahtarı systemMode. Eski
    # tradingMode/botMode/botEnableDemoOrders/botEnableRealOrders/
    # tradingKillSwitchActive/forceSafeMode kaldırıldı.
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
        "canlı hesap kimliğini doğrulayarak yazar; hesap/oturum değişince "
        "account watcher otomatik düşürür.",
    ),
    "armedAccountRef": ConfigDefinition(
        "armedAccountRef",
        "string",
        "",
        "Arm edilen hesabın sha256 referansı — gateway'in verdiği değer "
        "DOĞRUDAN saklanır (yeniden hash yok). Sadece arming akışı yazar.",
    ),
    "armedAccountSessionRef": ConfigDefinition(
        "armedAccountSessionRef",
        "string",
        "",
        "Arm anındaki hesap oturum referansı (sha256). Oturum değişirse "
        "watcher otomatik disarm eder. Sadece arming akışı yazar.",
    ),
    "armedAccountType": ConfigDefinition(
        "armedAccountType",
        "string",
        "",
        "Arm anındaki hesap türü (DEMO|REAL). Sadece arming akışı yazar.",
    ),
    "aiToolCallingEnabled": ConfigDefinition(
        "aiToolCallingEnabled",
        "bool",
        str(settings.ai_tools_enabled).lower(),
        "DeepSeek'in değerlendirme sırasında read-only veri araçlarını "
        "çağırmasına izin verir (panel > env; .env AI_TOOLS_ENABLED fallback).",
    ),
    "dailyMaxLossPct": ConfigDefinition(
        "dailyMaxLossPct",
        "decimal",
        "3",
        "Günlük maksimum zarar yüzdesi. Doğrulanmış hesap özkaynağı ile pozitif "
        "bot sermaye bütçesinin daha küçüğüne uygulanır; özkaynak yoksa pozitif "
        "bot bütçesi kullanılır. 0 = yüzde sınırı devre dışı.",
    ),
    "dailyMaxLossTl": ConfigDefinition(
        "dailyMaxLossTl",
        "decimal",
        "0",
        "İsteğe bağlı mutlak günlük maksimum zarar (TL). Yüzde sınırıyla birlikte "
        "pozitifse daha sıkı olan uygulanır. 0 = mutlak sınır devre dışı; SELL ve "
        "stop-loss bekçisi etkilenmez.",
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
        "bekçisi dahil tüm otomasyon durur.",
    ),
    "portfolioScanIntervalMinutes": ConfigDefinition(
        "portfolioScanIntervalMinutes",
        "int",
        str(settings.portfolio_scan_interval_minutes),
        "Eldeki pozisyonların AI ile yeniden değerlendirilme aralığı (dakika, en az 5).",
    ),
    "portfolioRotationEnabled": ConfigDefinition(
        "portfolioRotationEnabled",
        "bool",
        "false",
        "Daha güçlü fırsat için yalnızca kanıtlanmış bot pozisyonlarında "
        "dolum ve nakit teyitli otomatik SELL -> BUY rotasyonunu açar.",
    ),
    "rotationMinimumOpportunityScoreAdvantage": ConfigDefinition(
        "rotationMinimumOpportunityScoreAdvantage",
        "decimal",
        "15",
        "Hedef fırsat puanının satılacak pozisyondan en az kaç puan üstün olması gerektiği.",
    ),
    "rotationMinimumExpectedReturnAdvantagePct": ConfigDefinition(
        "rotationMinimumExpectedReturnAdvantagePct",
        "decimal",
        "2",
        "Hedef beklenen getirisinin kaynak pozisyondan en az kaç yüzde puan üstün olması gerektiği.",
    ),
    "rotationReviewIntervalMinutes": ConfigDefinition(
        "rotationReviewIntervalMinutes",
        "int",
        "10",
        "Rotasyon için gereken iki BUY değerlendirmesi arasındaki en kısa süre (dakika).",
    ),
    "rotationAssessmentMaxAgeMinutes": ConfigDefinition(
        "rotationAssessmentMaxAgeMinutes",
        "int",
        "30",
        "Rotasyon karşılaştırmasında kullanılabilecek AI değerlendirmesinin azami yaşı (dakika).",
    ),
    "rotationMinimumHoldingMinutes": ConfigDefinition(
        "rotationMinimumHoldingMinutes",
        "int",
        "60",
        "Yeni alınan bot pozisyonunun rotasyonla satılmadan önce tutulacağı en kısa süre.",
    ),
    "rotationPlanExpiryMinutes": ConfigDefinition(
        "rotationPlanExpiryMinutes",
        "int",
        "120",
        "Tamamlanmayan rotasyon planının güvenli biçimde sona ereceği süre.",
    ),
    "rotationMaxPerDay": ConfigDefinition(
        "rotationMaxPerDay",
        "int",
        "2",
        "Bir hesapta bir günde başlatılabilecek en fazla otomatik rotasyon.",
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
    # v2: eski REAL/DEMO mod bayrakları kaldırıldı. REAL emir yetkisi artık
    # realAccountArmed + armedAccountRef + oturum eşleşmesiyle verilir;
    # DEMO/REAL gateway'in bildirdiği accountType'tır.
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
    "sizingTotalBotCapitalBudgetTl": ConfigDefinition(
        "sizingTotalBotCapitalBudgetTl",
        "decimal",
        "0",
        "Botun tüm açık pozisyonları ve bekleyen BUY emirleri için aşamayacağı "
        "toplam sermaye bütçesi (TL). 0 yeni BUY işlemlerini kapatır.",
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
    "autoPromotionEnabled": ConfigDefinition(
        "autoPromotionEnabled",
        "bool",
        "false",
        "Otomatik 2-pass AI terfi sistemini aktif eder. Kapalıyken (varsayılan) "
        "adaylar 2 nitelikli cevaptan sonra sadece 'terfiye hazır' işaretlenir; "
        "Trade Watchlist'e giriş admin onayı gerektirir.",
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
    "systemMode",
    "realAccountArmed",
    "dailyMaxLossPct",
    "dailyMaxLossTl",
    "killSwitchEnabled",
    "scannerEnabled",
    "sizingRiskPerTradePct",
    "sizingTotalBotCapitalBudgetTl",
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
    "portfolioRotationEnabled",
    "rotationMinimumOpportunityScoreAdvantage",
    "rotationMinimumExpectedReturnAdvantagePct",
    "rotationReviewIntervalMinutes",
    "rotationAssessmentMaxAgeMinutes",
    "rotationMinimumHoldingMinutes",
    "rotationPlanExpiryMinutes",
    "rotationMaxPerDay",
    "accountReservationHandling",
    "maxTradeWatchlistSize",
    "minimumTrendPreScore",
    "minimumResearchScore",
    "researchMinimumConfidence",
    "researchMaximumRiskScore",
    "promotionConsecutivePasses",
    "promotionMinIntervalMinutes",
    "autoPromotionEnabled",
    "tradeWatchlistTtlHours",
    "discoveryMinimumVolumeTl",
    "discoveryMaximumSpreadPct",
}

# realAccountArmed/armedAccountRef panelin genel edit formundan yazılamaz —
# tek yazma yolu canlı hesap doğrulaması yapan arming endpoint'leridir.
# botAllowMarketOrders kod kilididir.
READ_ONLY_CONFIG_KEYS = frozenset(
    {
        "botAllowMarketOrders",
        "realAccountArmed",
        "armedAccountRef",
        "armedAccountSessionRef",
        "armedAccountType",
    }
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
            "kapıları."
        ),
        keys=(
            "systemMode",
            "killSwitchEnabled",
            "scannerEnabled",
            "aiToolCallingEnabled",
            "dailyMaxLossPct",
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
        title="Matriks gateway ve REAL hesap arming",
        description=(
            "REAL hesap emir yolu arming durumu ve HTTP timeout. REAL emir "
            "yalnızca canlı hesap kimliği doğrulanan arming endpoint'iyle açılır; DEMO/REAL "
            "gateway'in accountType'ıdır. MARKET emri kod seviyesinde salt okunur."
        ),
        keys=(
            "realAccountArmed",
            "armedAccountRef",
            "armedAccountSessionRef",
            "armedAccountType",
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
        title="AI portföy rotasyonu",
        description=(
            "Yalnız botun doğrulanmış lotlarında çalışır. SELL dolmadan ve "
            "gateway pozisyonu ile kullanılabilir nakit yenilenmeden BUY göndermez."
        ),
        keys=(
            "portfolioRotationEnabled",
            "rotationMinimumOpportunityScoreAdvantage",
            "rotationMinimumExpectedReturnAdvantagePct",
            "rotationReviewIntervalMinutes",
            "rotationAssessmentMaxAgeMinutes",
            "rotationMinimumHoldingMinutes",
            "rotationPlanExpiryMinutes",
            "rotationMaxPerDay",
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
            "sizingTotalBotCapitalBudgetTl",
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
    "systemMode": "Çalışma Modu (OBSERVE_ONLY / AUTO_TRADE)",
    "killSwitchEnabled": "Acil Durdurma (Kill Switch)",
    "scannerEnabled": "Tarayıcı Aktif",
    "aiToolCallingEnabled": "AI Tool-Calling Aktif",
    "dailyMaxLossPct": "Günlük Maks. Zarar (%)",
    "dailyMaxLossTl": "Günlük Maks. Zarar (TL)",
    "significancePriceMovePct": "Önem Fiyat Eşiği (%)",
    "portfolioScanIntervalMinutes": "Portföy Tarama Aralığı (dk)",
    "portfolioRotationEnabled": "Otomatik Portföy Rotasyonu",
    "rotationMinimumOpportunityScoreAdvantage": "Rotasyon Min. Fırsat Puanı Farkı",
    "rotationMinimumExpectedReturnAdvantagePct": "Rotasyon Min. Getiri Farkı (%)",
    "rotationReviewIntervalMinutes": "Rotasyon İki İnceleme Aralığı (dk)",
    "rotationAssessmentMaxAgeMinutes": "Rotasyon Değerlendirme Azami Yaşı (dk)",
    "rotationMinimumHoldingMinutes": "Rotasyon Min. Elde Tutma Süresi (dk)",
    "rotationPlanExpiryMinutes": "Rotasyon Planı Geçerliliği (dk)",
    "rotationMaxPerDay": "Günlük En Fazla Rotasyon",
    "buyAllowedSymbols": "BUY İzinli Semboller (gateway)",
    "sellExitAllowedSymbols": "SELL Çıkış İzinli Semboller",
    "realAccountArmed": "REAL Hesap Arming",
    "armedAccountRef": "Arm Edilen Hesap Ref (sha256)",
    "armedAccountSessionRef": "Arm Edilen Oturum Ref (sha256)",
    "armedAccountType": "Arm Edilen Hesap Türü",
    "botAllowMarketOrders": "MARKET Emirlere İzin (kilitli)",
    "botHttpTimeoutSeconds": "Bot HTTP Zaman Aşımı (sn)",
    "sizingRiskPerTradePct": "İşlem Başına Risk (%)",
    "sizingTotalBotCapitalBudgetTl": "Toplam Bot Sermaye Bütçesi (TL)",
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
    "autoPromotionEnabled": "Otomatik Terfi Sistemi Aktif",
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
