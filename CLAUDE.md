# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

- Install dev tools: `pip install -r requirements-dev.txt`
- Lint: `ruff check .`
- Run the full suite: `pytest -q`
- Run a single test: `pytest tests/test_<area>.py::<test_name> -q`
- Start the API: `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`
- Gateway integration smoke test (needs a running local Matriks gateway; reads URL/token from `.env`): `python scripts/gateway_smoke.py`
- One-time idempotent DB initializer: `python -m scripts.init_db_once --yes` (`--dry-run` to inspect only; refuses an empty URL and non-PostgreSQL production targets)
- Schema migrations: `python -m alembic upgrade head` (reads `DATABASE_URL` through `app.config`, not `alembic.ini`'s SQLite default)

Pytest forces a hermetic environment via `tests/conftest.py`: development mode, scanner/position-sync/order-sync disabled, mock AI provider, PAPER default mode, fixed 17:30 trading cutoff — and it uses `TEST_DATABASE_URL` when set, otherwise `sqlite+aiosqlite:///./test.db`. It **rejects a non-test `DATABASE_URL`** (raises at import) — don't try to bypass this guard.

## Architecture

The server is the brain; a local Matriks IQ C# algo (`matriks/TradeAiGateway.cs`) is a thin data-and-order gateway. Both run as native processes on the same Windows machine (no Docker — the production target is Windows Server 2019, and everything talks over loopback):

```
FastAPI server (this repo)                Matriks IQ (matriks/TradeAiGateway.cs)
  scanner.py  ── every N min ──┐
    │                          ├─→ GET  127.0.0.1:8787/health, /snapshot, /positions, /depth, /indicators
    ├─ evaluator.py            │
    │    AI provider           │
    │    RiskEngine            │
    └─ order decision ─────────┴─→ POST /order   (LIMIT only, gateway has no MARKET concept)
                                        │
       /api/order-result ←──────────────┘  OnOrderUpdate reports fills
```

All trading decisions live in Python (evaluated in-process against gateway snapshots) — there is no multi-turn session protocol with the gateway. Safety is **layered in two independent processes**: the scanner decides *whether* to send an order; the gateway enforces hard caps (LIMIT-only, max qty/value per order, daily order caps, locked long-term lots, account identity and REAL arming) that the server cannot raise. `matriks/TradeAiGateway.cs` is copy-pasted into Matriks IQ's algo editor — it is not compiled or exercised by this repo's test suite.

Two independent env switches gate automation, both default off: `SCANNER_ENABLED` (starts the scan loop) and `SCANNER_ALLOW_ORDERS` (lets decisions become orders — with this off, every decision is forced to PAPER regardless of admin panel trading mode). `REAL_LIVE` is **code-blocked** for scanner dispatch — don't weaken this without targeted tests.

### Request flow (`app/main.py` → routers → services)

- `app/main.py` wires routers and starts scanner/position-sync/order-sync in the FastAPI lifespan per settings.
- `app/routers/` — thin route handlers. `admin/` is split by domain (`auth`, `config_routes`, `dashboard`, `orders`, `positions`, `research`, `trade_profiles`) and assembled into one `APIRouter` in `admin/__init__.py`; URL surface is `/admin` (UI) and `/api/admin` (JSON). `signal.py` (evaluate endpoint) and `signal_history.py` (historical query endpoint) are deliberately separate — don't confuse the two.
- `app/services/evaluator.py` is the brain's entry point but the actual logic lives in `app/services/evaluation/`: `payload.py` (build request payload/context), `pipeline.py` (`evaluate_symbol` and its steps), `persistence.py` (persist evaluation + sizing audit), `parsing.py` (`_safe_*` parsing helpers). `evaluator.py` re-exports these for backward-compatible imports.
- `app/services/admin_config/` — DB-backed runtime config: `definitions.py` (the `ConfigDefinition` table: default, type and description), `store.py` (get/set + audit log), `validation.py` (value serialization and validation).
- `app/services/risk_engine.py` — the safety gate `evaluator` calls before allowing an order.
- `app/services/stop_loss_guard.py` — runs every scanner tick **independent of the AI**; compares each open `BotPosition`'s stop-loss against a fresh snapshot price and force-exits regardless of what the AI says next, then still goes through the normal order path (kill switch, cutoff, preflight all still apply).
- `app/services/matriks_gateway.py` — HTTP client for the gateway.
- `app/services/position_sync.py` / `order_sync.py` — reconcile gateway positions/orders into `bot_positions`/`order_logs`.

### Config precedence — four overlapping systems (see `docs/CONFIG_PRECEDENCE.md` for the full map)

Don't guess which layer a config key comes from — grep `docs/CONFIG_PRECEDENCE.md` first; it documents known duplicate-read sites and divergences (e.g. BUY confidence is checked by two different gates in `risk_engine.py` that use different resolvers and can genuinely disagree) that are intentionally left as-is, not oversights.

1. `app/core/risk_config.py` — `risk_config` singleton, loaded once from `RISK_*` env vars. Mostly supplies defaults that other layers reference.
2. `app/services/admin_config/` — DB-backed store (`SystemConfig` rows, created lazily; missing row falls back to `CONFIG_DEFINITIONS[key].default`). `build_runtime_risk_config(session)`: active `TradeProfile` field > per-field admin_config DB override > static default. `is_kill_switch_enabled(session)` is the **only correct way** to ask "is trading halted?" (ORs `killSwitchEnabled`, `tradingKillSwitchActive`, `forceSafeMode` — several call sites still read these flags individually for *display only*, which is fine, but never gate on a single flag directly).
3. `app/services/effective_risk_config.py` — separate resolver, used only for position-sizing numeric limits. `resolve_effective_risk_config(session)` takes the **strictest** value across env / admin_config / active profile on every field, so nothing can loosen a global safety boundary.
4. `app/services/trade_profile.py` — `TradeProfile` DB model + `get_active_profile(session)`. Deliberately does not import `admin_config` (would cycle); `activeTradeProfileCode` is read/written directly against `SystemConfig`.

A newer "panel-over-env" pattern layers on top for a few runtime flags (`scannerAllowOrders`, `portfolioScanIntervalMinutes`, `scannerEnabled`): a saved DB row permanently overrides `.env`; no row means the live `.env` value applies. `SCANNER_ENABLED` itself still gates whether the background task starts at process boot (needs a restart) — `scannerEnabled` the DB flag is a runtime pause on an already-running loop.

Order limits, confidence thresholds, signal filters, and scan interval are **not** standalone admin config keys — they're owned by the active Trade Profile (`/admin/trade-profiles`).

### Database

- Dev: SQLite (`dev.db`), auto-created via `create_all` on first request; **does not migrate** existing tables when models change (no ALTER) — delete `dev.db` and restart to pick up new columns, or write a migration.
- Production: PostgreSQL only, via Alembic (`python -m alembic upgrade head`), applied explicitly — never automatically at startup.
- Before production schema or order-flow changes: set `SCANNER_ALLOW_ORDERS=false`, activate the kill switch, back up PostgreSQL, reconcile active gateway orders, then reopen.

### AI providers

Only `mock` and `deepseek` are implemented. `mock` is the safe local default (always returns `WAIT`) and is **rejected by production startup validation**. `openai`/`anthropic` are accepted by config validation but raise `ValueError` at first signal evaluation — not implemented.

## Safety constraints (do not weaken without targeted tests)

- `REAL_LIVE` is code-blocked for scanner dispatch — this stays until a deliberate, separate decision. `LIVE` is not a real-order alias; the gateway resolves it to `PAPER`.
- The gateway is loopback-only by design. In production, `MATRIKS_GATEWAY_URL` must be an HTTP(S) loopback address and `MATRIKS_GATEWAY_TOKEN` must match the gateway's `ApiToken`.
- Production startup refuses to boot with: weak/placeholder/duplicate scoped tokens, `AI_PROVIDER=mock`, missing DeepSeek key when `AI_PROVIDER=deepseek`, missing/SQLite `DATABASE_URL`, or a non-loopback gateway URL.
- MARKET orders are never produced — `orderType` is always `LIMIT` or `NONE`.
- Keep real secrets out of test fixtures; `tests/conftest.py` hard-codes dev-only tokens for a reason.
