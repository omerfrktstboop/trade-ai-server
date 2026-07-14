"""Application configuration via environment variables.

Uses Pydantic Settings to load from .env file and environment.
Includes validation to prevent running in production without proper secrets.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
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


SUPPORTED_AI_PROVIDERS = frozenset(provider.value for provider in AIProvider)
_SECRET_PLACEHOLDERS = {
    "",
    "change-me",
    "changeme",
    "dev-token-change-me",
    "admin-change-me",
    "buraya_server_token",
    "buraya_gateway_token",
}


def _strong_secret(value: str, *, min_length: int = 32) -> bool:
    normalized = (value or "").strip()
    return (
        len(normalized) >= min_length
        and len(set(normalized)) >= 12
        and normalized.casefold() not in _SECRET_PLACEHOLDERS
        and "change-me" not in normalized.casefold()
    )


def is_supported_ai_provider(value: object) -> bool:
    """Return whether *value* names an implemented AI provider."""
    if isinstance(value, AIProvider):
        value = value.value
    return isinstance(value, str) and value.lower() in SUPPORTED_AI_PROVIDERS


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
    evaluation_api_token: str = ""
    gateway_api_token: str = ""
    admin_api_token: str = ""
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
    # Operational data refresh; it never evaluates symbols or sends orders.
    position_sync_enabled: bool = True
    position_sync_interval_seconds: int = 60
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

    # Data-only discovery universe. Subscription/research does not grant BUY;
    # only two-pass research promotion creates a trade-watchlist row.
    discovery_symbols: str = (
        "GARAN,ISCTR,VAKBN,HALKB,YKBNK,FROTO,TOASO,PGSUS,TAVHL,TCELL,"
        "TTKOM,BIMAS,MGROS,ULKER,ARCLK,VESTL,OTKAR,EKGYO,PETKM,EREGL,"
        "ASELS,BRSAN,ALARK,DOAS,SAHOL,GUBRF,CIMSA,ZOREN,MAVI,ODAS"
    )
    discovery_interval_minutes: int = 5
    max_research_candidates_per_cycle: int = 10
    max_active_research_symbols: int = 10
    max_concurrent_research_evaluations: int = 2
    candidate_cooldown_minutes: int = 15
    max_trade_watchlist_size: int = 20
    research_candidate_ttl_hours: int = 24
    trade_watchlist_ttl_hours: int = 24
    promotion_min_interval_minutes: int = 10
    promotion_consecutive_passes: int = 2

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

    @field_validator("ai_provider", mode="before")
    @classmethod
    def _validate_ai_provider(cls, value: object) -> object:
        """Reject configuration values with no provider implementation."""
        candidate = value.value if isinstance(value, AIProvider) else value
        if not is_supported_ai_provider(candidate):
            raise ValueError(
                f"Unsupported AI_PROVIDER={candidate!r}. "
                "Supported providers: mock, deepseek"
            )
        return value

    @model_validator(mode="after")
    def _validate_production_safety(self) -> "Settings":
        """Block startup in production when secrets are missing or defaults are used."""
        if self.app_env != AppEnv.PRODUCTION:
            return self

        errors: list[str] = []

        scoped_tokens = {
            "EVALUATION_API_TOKEN": self.evaluation_api_token,
            "GATEWAY_API_TOKEN": self.gateway_api_token,
            "ADMIN_API_TOKEN": self.admin_api_token,
        }
        for name, secret in scoped_tokens.items():
            if not _strong_secret(secret):
                errors.append(
                    f"{name} must be non-placeholder, >=32 chars, and contain >=12 unique characters."
                )
        if len(set(scoped_tokens.values())) != len(scoped_tokens):
            errors.append("Evaluation, gateway, and admin API tokens must be distinct.")
        if not _strong_secret(self.admin_password, min_length=16):
            errors.append(
                "ADMIN_PASSWORD is empty or still set to dev default. "
                "Set a secure admin password in production."
            )

        if not _strong_secret(self.matriks_gateway_token):
            errors.append(
                "MATRIKS_GATEWAY_TOKEN is required in production. "
                "Set a strong shared secret matching the gateway ApiToken."
            )

        gateway_url = urlparse(self.matriks_gateway_url)
        if gateway_url.scheme not in {"http", "https"} or gateway_url.hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            errors.append(
                "MATRIKS_GATEWAY_URL must target localhost/127.0.0.1 in production."
            )

        # AI provider key required
        if self.ai_provider == AIProvider.DEEPSEEK and not self.deepseek_api_key:
            errors.append("DEEPSEEK_API_KEY is required when AI_PROVIDER=deepseek")

        # Mock provider is not allowed in production
        if self.ai_provider == AIProvider.MOCK:
            errors.append(
                "AI_PROVIDER=mock is not allowed in production. "
                "Use AI_PROVIDER=deepseek. Supported providers: mock, deepseek"
            )

        # Database: must be set and must NOT be SQLite in production
        if not self.database_url:
            errors.append(
                "DATABASE_URL is required in production. "
                "Use PostgreSQL (e.g. postgresql+asyncpg://...)."
            )
        elif not self.database_url.lower().startswith("postgresql"):
            errors.append(
                "DATABASE_URL must use PostgreSQL in production "
                "(e.g. postgresql+asyncpg://...)."
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
    def effective_evaluation_token(self) -> str:
        return self.evaluation_api_token or self.api_token

    @property
    def effective_gateway_api_token(self) -> str:
        return self.gateway_api_token or self.api_token

    @property
    def effective_admin_api_token(self) -> str:
        return self.admin_api_token or self.api_token

    @property
    def is_production(self) -> bool:
        """Convenience check for production environment."""
        return self.app_env == AppEnv.PRODUCTION

    @property
    def is_development(self) -> bool:
        """Convenience check for development environment."""
        return self.app_env == AppEnv.DEVELOPMENT


settings = Settings()
