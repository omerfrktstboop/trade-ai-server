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
from urllib.parse import urlparse

import pytest


def _is_isolated_test_database(url: str) -> bool:
    parsed = urlparse(url)
    if parsed.scheme.startswith("sqlite"):
        database = parsed.path.casefold()
        return database in {"", "/:memory:"} or "test" in database
    if parsed.scheme.startswith("postgresql"):
        database = parsed.path.lstrip("/").casefold()
        return (
            parsed.hostname in {"127.0.0.1", "localhost", "postgres"}
            and "test" in database
        )
    return False


explicit_test_url = os.environ.get("TEST_DATABASE_URL", "").strip()
inherited_url = os.environ.get("DATABASE_URL", "").strip()
if explicit_test_url:
    if not _is_isolated_test_database(explicit_test_url):
        raise RuntimeError("TEST_DATABASE_URL must identify an isolated test database")
    test_database_url = explicit_test_url
elif inherited_url:
    if not _is_isolated_test_database(inherited_url):
        raise RuntimeError(
            "Refusing to run tests with a non-test DATABASE_URL; use TEST_DATABASE_URL"
        )
    test_database_url = inherited_url
else:
    test_database_url = "sqlite+aiosqlite:///./test.db"

os.environ["DATABASE_URL"] = test_database_url
os.environ["APP_ENV"] = "development"
os.environ.setdefault("AI_PROVIDER", "mock")
# Makro filtre testlerde endeks snapshot'ı beklemesin (FakeGateway'ler XU100
# tanımaz; get_index_regime zaten fail-open ama boş sembol hiç denemez).
os.environ["MARKET_INDEX_SYMBOL"] = ""
# Scanner/discovery davranışı testlerde deterministik olmalı — üretim .env'i
# bunları açmış olabilir (SCANNER_ALLOW_ORDERS=true, DISCOVERY_SYMBOLS=...).
# Env var, .env dosyasına baskın geldiği için burada sabitliyoruz.
os.environ["SCANNER_ENABLED"] = "false"
os.environ["POSITION_SYNC_ENABLED"] = "false"
os.environ["DISCOVERY_SYMBOLS"] = ""
# Testler dev token'ı hard-code'lar; üretim .env'inin gerçek token'ı sızmasın.
os.environ["API_TOKEN"] = "dev-token-change-me"
os.environ["ADMIN_PASSWORD"] = "admin-change-me"
# Üretim .env'i günün erken saatine sabitlenmiş bir trading cutoff içerebilir
# (ops ihtiyacı); testler bunun yerine kod varsayılanını (17:30) bekler, yoksa
# gerçek saatten bağımsız olarak "trading cutoff passed" ile tüm emir testleri
# rastgele kırılır.
os.environ["RISK_DISABLE_TRADING_AFTER"] = "17:30"


@pytest.fixture(autouse=True)
def _reset_decision_caches():
    """Süreç içi global cache'ler (karar cache'i + endeks rejimi + hesap
    izleyici baseline'ı) test sınırlarından sızmasın."""
    from app.core.runtime_flags import clear_dispatch_block
    from app.services.account_watcher import account_watcher
    from app.services.decision_gate import decision_cache
    from app.services.market_regime import reset_cache
    from app.services.significance import significance_detector

    decision_cache.clear()
    reset_cache()
    account_watcher.reset()
    significance_detector.reset()
    clear_dispatch_block()
    yield
    decision_cache.clear()
    reset_cache()
    account_watcher.reset()
    significance_detector.reset()
    clear_dispatch_block()
