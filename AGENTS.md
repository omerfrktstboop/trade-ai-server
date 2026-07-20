# AGENTS.md

## Commands

- Install development tools with `pip install -r requirements-dev.txt`.
- Run lint with `ruff check .`.
- Run the suite with `pytest -q`; run a focused test with `pytest tests/test_<area>.py::<test_name> -q`.
- Pytest forces development mode, disables scanner/position sync, uses `TEST_DATABASE_URL` when supplied, and otherwise uses `sqlite+aiosqlite:///./test.db`. It rejects a non-test `DATABASE_URL`; do not bypass this guard.
- Start the API with `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`.
- Run the read-only gateway integration check with `python scripts/gateway_smoke.py`; it requires a running local Matriks gateway and obtains its URL/token from `.env` unless overridden.

## Architecture

- `app/main.py` is the FastAPI entrypoint. It wires routers and starts scanner, position sync, and order sync in the lifespan according to settings.
- The server owns evaluation, risk, and order decisions; `matriks/TradeAiGateway.cs` is the local Matriks IQ data/order gateway. Gateway source-contract tests do not compile or validate it in Matriks IQ.
- `app/services/scanner.py` is the automated path. Keep `SCANNER_ENABLED=false` and `SCANNER_ALLOW_ORDERS=false` for normal development; with order dispatch disabled, scanner decisions are forced to PAPER.
- The only implemented AI providers are `mock` and `deepseek`. `mock` is the safe local default and returns safe decisions; production validation rejects it.

## Database And Migrations

- Development startup calls `create_all` and seeds built-in trade profiles. It does not alter existing SQLite tables after model changes; recreate the disposable dev database or use a migration.
- Alembic reads `DATABASE_URL` through `app.config`; use `python -m alembic upgrade head` for schema changes. Do not rely on the SQLite URL in `alembic.ini` when a `.env` or environment value is set.
- Production uses PostgreSQL only and must be migrated explicitly before startup. Before production schema or order-flow work, close dispatch with `SCANNER_ALLOW_ORDERS=false`, activate the kill switch, back up PostgreSQL, then reconcile active gateway orders before reopening it.
- `python -m scripts.init_db_once --yes` is an explicit idempotent first-install initializer (`--dry-run` inspects without writes); it refuses an empty URL and non-PostgreSQL production targets.

## Safety Constraints

- `REAL_LIVE` is code-blocked for scanner dispatch. Do not weaken mode, kill-switch, limit-order, position-ownership, or gateway hard-cap protections without targeted tests.
- The gateway is intentionally loopback-only. In production, `MATRIKS_GATEWAY_URL` must be an HTTP(S) loopback address and `MATRIKS_GATEWAY_TOKEN` must match the gateway's `ApiToken`.
- Production startup requires distinct strong scoped tokens, explicit CORS origins, PostgreSQL, and DeepSeek credentials. Keep `.env` secrets out of code and test fixtures.
