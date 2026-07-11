"""Application configuration via environment variables.

Uses Pydantic Settings to load from .env file and environment.
Includes validation to prevent running in production without proper secrets.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppEnv(str, Enum):
    """Application environment."""

    DEVELOPMENT = "development"
    STAGING = "staging"
    PRODUCTION = "production"


class AIProvider(str, Enum):
    """Supported AI providers."""

    MOCK = "mock"
    DEEPSEEK = "deepseek"
    OPENAI = "openai"
    ANTHROPIC = "anthropic"


class Mode(str, Enum):
    """Trading mode."""

    PAPER = "paper"
    LIVE = "live"
    DEMO_LIVE = "demo_live"
    REAL_LIVE = "real_live"


class Settings(BaseSettings):
    """Application settings loaded from environment / .env file."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ── App metadata ──────────────────────────────────────────────────────

    app_name: str = "trade-ai-server"
    app_version: str = "0.1.0"
    app_env: AppEnv = AppEnv.DEVELOPMENT

    # ── Server ────────────────────────────────────────────────────────────

    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

    # ── CORS ──────────────────────────────────────────────────────────────

    cors_origins_raw: str = Field(
        default="*",
        alias="CORS_ORIGINS",
        description="Comma-separated list of allowed origins",
    )

    @property
    def cors_origins(self) -> list[str]:
        """Parse comma-separated CORS origins into a list."""
        return [o.strip() for o in self.cors_origins_raw.split(",") if o.strip()]

    # ── Security ──────────────────────────────────────────────────────────

    api_token: str = Field(
        default="dev-token-change-me",
        description="API authentication token. Default is for local dev only.",
    )
    admin_password: str = Field(
        default="admin-change-me",
        description="Admin panel password. Change for staging/production.",
    )

    # ── AI Provider ───────────────────────────────────────────────────────

    ai_provider: AIProvider = AIProvider.MOCK
    deepseek_api_key: str = ""
    deepseek_model: str = "deepseek-chat"
    deepseek_base_url: str = "https://api.deepseek.com/v1"
    deepseek_timeout: int = 30

    # ── Database ──────────────────────────────────────────────────────────

    database_url: str = ""

    # ── Telegram ──────────────────────────────────────────────────────────

    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # ── Trading ───────────────────────────────────────────────────────────

    default_mode: Mode = Mode.PAPER

    # ── Matriks gateway (full-inversion mimarisi) ─────────────────────────

    matriks_gateway_url: str = "http://127.0.0.1:8787"
    matriks_gateway_token: str = ""
    matriks_gateway_timeout: float = 10.0

    # ── Scanner (server-side tarama döngüsü) ──────────────────────────────
    # Default kapalı: testler ve API-only kurulumlar yanlışlıkla tarama
    # başlatmasın. Sunucuda .env ile açılır. Acil durumda false yapmak tüm
    # otomasyonu keser (kill switch'e ek ikinci fren).
    scanner_enabled: bool = False
    scanner_tick_seconds: float = 60.0
    # false → tüm kararlar PAPER'a sabitlenir, emir yolu tamamen kapalı
    # (Phase 1 davranışı). true → mode admin panelden gelir ve DEMO_LIVE
    # kararları gateway'e emir olarak gönderilir. REAL_LIVE scanner'da
    # Phase 2 boyunca kod seviyesinde bloklu.
    scanner_allow_orders: bool = False
    # Manual admin approval is a separate order gate and defaults closed.
    manual_approval_allow_orders: bool = False
    order_sync_enabled: bool = True
    order_sync_interval_seconds: int = 900
    order_pending_timeout_minutes: int = 15

    # Makro filtre: piyasa geneli rejim bu endeks sembolünden okunur.
    # Boş string makro filtreyi tamamen kapatır.
    market_index_symbol: str = "XU100"

    # ── Matriks-side news subscription (gateway'e /gateway/config ile iner) ──
    # Bunlar Matriks algo panelinde parametre DEĞİL; server'dan yönetilir.
    # Boş bırakmak: keyword aboneliği yok, sembol bazlı pasif haber yakalama
    # yine çalışır. (Haber tam metnini zaten Python news_service.py sağlıyor.)
    news_keywords_csv: str = ""
    news_symbol_keyword_rules_csv: str = ""
    news_filters_only_in_headers: bool = True
    news_filters_exact_match: bool = False
    news_risk_lock_enabled: bool = True
    news_risk_buy_block_enabled: bool = True
    news_risk_lookback_hours: int = 24
    news_risk_keywords_csv: str = "tedbir,brüt takas,kredili işlem yasağı,açığa satış yasağı,manipülasyon,soruşturma,faaliyet durdurma,iflas,konkordato,haciz,ceza,dava,spk inceleme,pay satışı,ortak satışı,bedelli sermaye artırımı,bilanço zararı"

    # Dinamik watchlist keşif evreni — gateway'de kayıtlı olmayan bu semboller
    # periyodik olarak taranır; BUY sinyali gelince allowedSymbols'e eklenir.
    discovery_symbols: str = (
        "GARAN,ISCTR,VAKBN,HALKB,YKBNK,FROTO,TOASO,PGSUS,TAVHL,TCELL,"
        "TTKOM,BIMAS,MGROS,ULKER,ARCLK,VESTL,OTKAR,EKGYO,PETKM,EREGL,"
        "ASELS,BRSAN,ALARK,DOAS,SAHOL,GUBRF,CIMSA,ZOREN,MAVI,ODAS"
    )
    discovery_interval_minutes: int = 30

    # Discovery agent (movers tabanlı) eleme eşikleri.
    # Tavan/taban kilidi: |değişim| bu yüzdeyi aşan adaylar elenir.
    discovery_ceiling_change_pct: float = 9.5
    # Sığ hacim: günlük hacmi (TL) bu eşiğin altında kalan adaylar elenir.
    discovery_min_volume_tl: float = 100_000_000.0
    # Satış duvarı: toplam ask/bid oranı bu değeri aşan adaylar elenir.
    discovery_max_ask_bid_ratio: float = 3.0
    watchlist_min_quality_score: float = 60.0

    # Portföy re-evaluasyon döngüsü: eldeki pozisyonlar bu aralıkla LLM'e
    # "kar al / zarar kes / tut" sorusuyla yeniden değerlendirilir.
    portfolio_scan_interval_minutes: int = 30

    # Haftalık self-reflection (review agent) — hafta sınırları bu IANA
    # timezone'a göre hesaplanır (Pazartesi 00:00 - Pazartesi 00:00).
    review_timezone: str = "Europe/Istanbul"
    # Stop-loss'a "isabet" sayılması için tolerans payı — kayma/slipaj
    # yüzünden exit fiyatı stop'un birkaç kuruş üstünde kalabilir.
    review_stop_loss_tolerance_pct: float = 2.0

    # ── Paths ─────────────────────────────────────────────────────────────

    base_dir: Path = Field(
        default_factory=lambda: Path(__file__).resolve().parent.parent,
    )

    # ── Validation ────────────────────────────────────────────────────────

    @model_validator(mode="after")
    def _validate_production_safety(self) -> "Settings":
        """Block startup in production when secrets are missing or defaults are used."""
        if self.app_env != AppEnv.PRODUCTION:
            return self

        errors: list[str] = []

        # API token must be set and not the dev default
        if not self.api_token or self.api_token == "dev-token-change-me":
            errors.append(
                "API_TOKEN is empty or still set to dev default. "
                "Set a secure token in production."
            )
        if not self.admin_password or self.admin_password == "admin-change-me":
            errors.append(
                "ADMIN_PASSWORD is empty or still set to dev default. "
                "Set a secure admin password in production."
            )

        # AI provider key required
        if self.ai_provider == AIProvider.DEEPSEEK and not self.deepseek_api_key:
            errors.append("DEEPSEEK_API_KEY is required when AI_PROVIDER=deepseek")

        # Mock provider is not allowed in production
        if self.ai_provider == AIProvider.MOCK:
            errors.append(
                "AI_PROVIDER=mock is not allowed in production. "
                "Use deepseek, openai, or anthropic for live trading."
            )

        # Database: must be set and must NOT be SQLite in production
        if not self.database_url:
            errors.append(
                "DATABASE_URL is required in production. "
                "Use PostgreSQL (e.g. postgresql+asyncpg://...)."
            )
        elif self.database_url.startswith("sqlite"):
            errors.append(
                "DATABASE_URL must use PostgreSQL in production, not SQLite. "
                f"Got: {self.database_url}"
            )

        # Wildcard CORS is not allowed once authenticated endpoints are public
        if "*" in self.cors_origins:
            errors.append(
                "CORS_ORIGINS=* is not allowed in production. "
                "Set an explicit comma-separated list of allowed origins."
            )

        if errors:
            raise ValueError(
                "Production safety check failed:\n- " + "\n- ".join(errors)
            )

        return self

    @property
    def is_production(self) -> bool:
        """Convenience check for production environment."""
        return self.app_env == AppEnv.PRODUCTION

    @property
    def is_development(self) -> bool:
        """Convenience check for development environment."""
        return self.app_env == AppEnv.DEVELOPMENT


settings = Settings()
