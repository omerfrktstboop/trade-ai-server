"""Pytest bootstrap — force a hermetic, SQLite-backed test environment.

The production ``.env`` now points ``DATABASE_URL`` at PostgreSQL. The test
suite creates/drops the schema through many short-lived event loops
(``asyncio.run`` per fixture), which asyncpg does not tolerate. We override the
DB to a local SQLite file BEFORE any ``app`` module imports and instantiates
its settings/engine singletons, so tests never touch PostgreSQL.

Environment variables take precedence over ``.env`` in pydantic-settings, and
this module is imported by pytest before any test module pulls in app code.
"""

from __future__ import annotations

import os

os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///./test.db")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("AI_PROVIDER", "mock")
# Makro filtre testlerde endeks snapshot'ı beklemesin (FakeGateway'ler XU100
# tanımaz; get_index_regime zaten fail-open ama boş sembol hiç denemez).
os.environ["MARKET_INDEX_SYMBOL"] = ""
# Scanner/discovery davranışı testlerde deterministik olmalı — üretim .env'i
# bunları açmış olabilir (SCANNER_ALLOW_ORDERS=true, DISCOVERY_SYMBOLS=...).
# Env var, .env dosyasına baskın geldiği için burada sabitliyoruz.
os.environ["SCANNER_ENABLED"] = "false"
os.environ["SCANNER_ALLOW_ORDERS"] = "false"
os.environ["POSITION_SYNC_ENABLED"] = "false"
os.environ["DISCOVERY_SYMBOLS"] = ""
# Testler dev token'ı hard-code'lar; üretim .env'inin gerçek token'ı sızmasın.
os.environ["API_TOKEN"] = "dev-token-change-me"
os.environ["ADMIN_PASSWORD"] = "admin-change-me"
# tradingMode'un default'u DEFAULT_MODE'dan türetilir; üretim .env'i
# demo_live'a çekmiş olabilir — testler PAPER varsayımıyla yazıldı.
os.environ["DEFAULT_MODE"] = "paper"


import pytest


@pytest.fixture(autouse=True)
def _reset_decision_caches():
    """Süreç içi global cache'ler (karar cache'i + endeks rejimi) test
    sınırlarından sızmasın."""
    from app.services.decision_gate import decision_cache
    from app.services.market_regime import reset_cache

    decision_cache.clear()
    reset_cache()
    yield
    decision_cache.clear()
    reset_cache()
