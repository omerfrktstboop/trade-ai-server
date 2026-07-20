# trade-ai-server

Modular FastAPI backend for AI-powered trading. Clean, typed, and extensible.

## Quick Start

Runs natively — no Docker. The production target is a Windows machine
running Matriks IQ, so the server, PostgreSQL, and the Matriks gateway all
run as native processes on the same box (see [Architecture](#architecture)).

```bash
# 1. Clone and enter
git clone https://github.com/YOUR_USER/trade-ai-server.git
cd trade-ai-server

# 2. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and configure environment
cp .env.example .env
# Edit .env and fill in your keys

# 5. Run the server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

For a permanent Windows deployment, run uvicorn as a service (e.g. via NSSM
or Task Scheduler) so it survives reboots and RDP disconnects.

## Environment Variables

Copy `.env.example` → `.env` and fill in the required values:

| Variable            | Required | Default                 | Description                              |
|---------------------|----------|-------------------------|------------------------------------------|
| `APP_ENV`           | Yes      | `development`           | `development` / `staging` / `production` |
| `EVALUATION_API_TOKEN` | Prod  | —                       | Signal evaluation API token              |
| `GATEWAY_API_TOKEN` | Prod     | —                       | Gateway callback/config API token         |
| `ADMIN_API_TOKEN`   | Prod     | —                       | Admin API bearer token                    |
| `AI_PROVIDER`       | Yes      | `mock`                  | `mock` (dev) / `deepseek`. `openai` / `anthropic` are accepted by config validation but **not implemented yet** — selecting them raises `ValueError` at first signal evaluation. |
| `DEEPSEEK_API_KEY`  | *        | —                       | Required when `AI_PROVIDER=deepseek`     |
| `DEEPSEEK_MODEL`    | No       | `deepseek-chat`         | Model name                               |
| `DATABASE_URL`      | Prod     | `sqlite+aiosqlite:///./dev.db` (dev) | PostgreSQL for production, SQLite auto for dev |
| `MATRIKS_GATEWAY_URL` | Yes    | `http://127.0.0.1:8787` | Matriks gateway address — always loopback  |
| `MATRIKS_GATEWAY_TOKEN` | Prod | —                     | Must match the gateway's `ApiToken` parameter |
| `SCANNER_ENABLED`   | No       | `false`                 | Starts the background scan loop            |
| `SCANNER_ALLOW_ORDERS` | No    | `false`                 | Lets scanner decisions become DEMO_LIVE orders |
| `TELEGRAM_BOT_TOKEN`| No       | —                       | Telegram bot token                       |
| `TELEGRAM_CHAT_ID`  | No       | —                       | Default chat ID                          |
| `DEFAULT_MODE`      | No       | `paper`                 | `paper` / `live` / `demo_live` / `real_live` — fallback mode used when a request doesn't specify one. `manual` is only valid as a **per-request** `mode` field, not as `DEFAULT_MODE`. |

\* `DEEPSEEK_API_KEY` is only required in production or when `AI_PROVIDER=deepseek`.

**Production safety:** When `APP_ENV=production`, the server will refuse to start if:
- scoped evaluation, gateway, or admin tokens are weak, placeholders, or equal to each other
- `AI_PROVIDER=mock` (mock is not allowed in production)
- `AI_PROVIDER=deepseek` but `DEEPSEEK_API_KEY` is empty
- `DATABASE_URL` is missing or uses SQLite
- `MATRIKS_GATEWAY_URL` is not an HTTP(S) loopback URL

**Database:** In development, `DATABASE_URL` can be left empty — the server
auto-creates a SQLite database (`dev.db`) on first request. No PostgreSQL
setup needed for local development. In production, PostgreSQL is required:
`DATABASE_URL` must be set and must not be SQLite. Install PostgreSQL
natively on Windows (no Docker) and point `DATABASE_URL` at it, e.g.
`postgresql+asyncpg://trade_ai:***@localhost:5432/trade_ai`.

## Running Modes

### Safe local test (zero setup)

Default config works out of the box with `AI_PROVIDER=mock`:

```bash
cp .env.example .env        # AI_PROVIDER=mock zaten, değişiklik gerekmez
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

- No external API calls — mock returns safe `WAIT` decisions.
- SQLite `dev.db` auto-created on first request.
- Perfect for integration testing and UI development.

### Live trading with DeepSeek

Switch to real AI by editing `.env`:

```bash
#.env
AI_PROVIDER=deepseek
DEEPSEEK_API_KEY=sk-your-real-key
```

> **Note:** `AI_PROVIDER=mock` is **blocked in production**. Trying to deploy with
> mock will fail at startup. This ensures you never accidentally ship a dead AI.

## ⚠️ Canlı Öncesi Güvenlik Kontrol Listesi

Aşağıdaki adımları **LIVE** moda geçmeden önce mutlaka tamamlayın:

| # | Kontrol | Nasıl | Beklenen |
|---|---|---|---|
| 1 | **Default mod PAPER** | `.env` → `DEFAULT_MODE=paper` | İsteklerde `mode` belirtilmezse PAPER çalışır |
| 2 | **Mock AI test** | `AI_PROVIDER=mock` + `uvicorn` ile test | Tüm istekler `WAIT` döner, emir oluşmaz |
| 3 | **DeepSeek test (PAPER)** | `AI_PROVIDER=deepseek`, `mode=PAPER` | AI karar üretir ama `allowOrder=false` |
| 4 | **OBSERVE_ONLY test** | Admin panelde `systemMode=OBSERVE_ONLY` | Kararlar izlenir, emir otomatik gönderilmez |
| 5 | **LIVE test (gerçek AI)** | `mode=LIVE` test istekleri | `allowOrder=true` olabilir, emirler gerçek gönderilir |
| 6 | **Uzun vade lotlar** | `RISK_LOCKED_LONG_TERM_SYMBOLS=ASELS,EREGL` ayarlandı mı? | Kilitli semboller asla satılmaz |
| 7 | **Günlük limit** | `RISK_MAX_DAILY_TRADE_COUNT=3` doğru mu? | Aşılınca BUY/SELL bloklanır |
| 8 | **Cutoff saati** | `RISK_DISABLE_TRADING_AFTER=17:30`, `RISK_TIMEZONE=Europe/Istanbul` doğru mu? | Sonrası sadece WAIT |
| 9 | **PostgreSQL** | `DATABASE_URL=postgresql+asyncpg://...` ayarlandı mı? | Production'da SQLite çalışmaz |
| 10 | **Gateway sağlığı** | `python scripts/gateway_smoke.py` | Tüm kontroller geçer |
| 11 | **Scanner önce PAPER'da** | `SCANNER_ENABLED=true`, `SCANNER_ALLOW_ORDERS=false` ile birkaç gün çalıştır | Kararlar `ai_decisions`'a düşer, emir gitmez |
| 12 | **Emir yolu DEMO_LIVE'da** | `SCANNER_ALLOW_ORDERS=true` + admin panelden `tradingMode=DEMO_LIVE` | Gateway `order_logs`'a `SENT_PENDING` yazar |

> **⚠️ LIVE mod gerçek emir gönderir.** Sadece PAPER → OBSERVE_ONLY → LIVE sırasıyla
> test ettikten ve tüm kontrolleri tamamladıktan sonra aktif edin.

## API Endpoints

| Method | Path                    | Auth     | Description              |
|--------|-------------------------|----------|--------------------------|
| GET    | `/`                     | —        | Root (docs links)        |
| GET    | `/api/health/live`      | —        | Process liveness         |
| GET    | `/api/health/ready`     | —        | Dependency/order readiness |
| POST   | `/api/signal/evaluate`  | Bearer   | Evaluate trading signal (caller supplies market data; manual testing / debugging) |
| POST   | `/api/order-result`     | Bearer   | Receive order result from the Matriks gateway |
| GET    | `/admin`                | Admin    | Admin dashboard          |
| GET    | `/admin/config`         | Admin    | Runtime risk config UI   |
| GET    | `/admin/emergency`      | Admin    | Kill switch controls     |
| GET    | `/api/admin/config`     | Bearer   | Runtime config API       |
| GET    | `/docs`                 | —        | Swagger UI               |
| GET    | `/redoc`                | —        | ReDoc                    |

> The live trading path does **not** go through these endpoints. The server's
> background scanner pulls market data from the Matriks gateway and evaluates
> in-process — see [Architecture](#architecture) below.

### Admin Panel MVP

- UI routes use `/admin`; JSON routes use `/api/admin`.
- Browser login uses `ADMIN_PASSWORD`; admin API calls accept `ADMIN_API_TOKEN`.
- Secrets are not exposed by admin config endpoints: scoped API tokens, `DEEPSEEK_API_KEY`, and `DATABASE_URL` are not returned.
- Config edits are stored in `system_configs`; every changed value writes `config_audit_logs`.
- Authenticated admin changes are applied directly; audit reason and actor are retained.
- `killSwitchEnabled=true` makes `/api/signal/evaluate` return `WAIT` with `allowOrder=false`.
- If `tradingMode` exists in DB, it overrides the incoming request mode; otherwise request mode defaults remain unchanged.
- Order limits, confidence thresholds, signal filters, and scan interval are managed by the active Trade Profile (`/admin/trade-profiles`).

### Signal Evaluate

```bash
# PAPER mode — always returns allowOrder: false
curl -X POST http://localhost:8000/api/signal/evaluate \
  -H "Authorization: Bearer *** \
  -H "Content-Type: application/json" \
  -d '{
    "requestId": "sig-001",
    "symbol": "BTCUSDT",
    "timeframe": "1h",
    "lastPrice": 67500.0,
    "open": 67200.0,
    "high": 67800.0,
    "low": 67000.0,
    "volume": 1234.5,
    "rsi": 65.2,
    "mode": "PAPER"
  }'
```

**Response (200) — PAPER mode:**
```json
{
  "requestId": "sig-001",
  "symbol": "BTCUSDT",
  "action": "WAIT",
  "qty": 0.0,
  "orderType": "NONE",
  "price": null,
  "confidenceScore": 0.0,
  "riskScore": 0.0,
  "allowOrder": false,
  "reason": "Safe default: PAPER mode or no decision.",
  "entryRange": null,
  "stopLoss": null,
  "targetPrice": null
}
```

**Response (200) — LIVE mode, BUY signal:**
```json
{
  "requestId": "sig-002",
  "symbol": "THYAO",
  "action": "BUY",
  "qty": 42,
  "orderType": "LIMIT",
  "price": 71.50,
  "confidenceScore": 82,
  "riskScore": 15,
  "allowOrder": true,
  "reason": "RSI oversold bounce with MACD golden cross.",
  "entryRange": {"min": 70.80, "max": 71.50},
  "stopLoss": 68.90,
  "targetPrice": 76.00
}
```

**Key request fields:**

| Field | Type | Description |
|---|---|---|
| `mode` | string | `"PAPER"` / `"MANUAL"` / `"LIVE"` / `"DEMO_LIVE"` / `"REAL_LIVE"` (default: `"PAPER"`) |
| `dailyTradeCount` | int | Opsiyonel günlük işlem sayısı. Gönderilmezse sunucu RiskEngine öncesinde bugünkü sayıyı `order_logs` / `risk_decisions` üzerinden hesaplar. |
| `botPositionQty` | float | Bot'un mevcut pozisyonu — SELL clamp üst sınırı |
| `totalAccountQty` | float | Hesaptaki toplam lot |
| `lockedLongTermQty` | float | Uzun vade kilitli lot — asla satılmaz |
| `technicalFeatures` | object | Opsiyonel Matriks teknik feature bloğu. `alphaTrendSignal`, `indicatorConsensus`, `natr`, `depthQueueDropPct` gibi alanlar AI payload'una eklenir ve RiskEngine tarafında sadece geldiğinde koruyucu filtre olarak kullanılır. |

**Key response fields:**

| Field | Type | Description |
|---|---|---|
| `action` | enum | `"BUY"` / `"SELL"` / `"WAIT"` |
| `orderType` | enum | `"LIMIT"` / `"NONE"` — **MARKET order asla üretilmez** |
| `price` | float\|null | BUY → `entryRange.max`, SELL → `lastPrice`, WAIT → `null` |
| `allowOrder` | bool | `true` ise emri gönder (live modes), `false` ise emir yok |
| `entryRange` | object\|null | `{"min": ..., "max": ...}` — limit emir için fiyat aralığı |
| `stopLoss` | float\|null | Önerilen zarar-kes |
| `targetPrice` | float\|null | Önerilen hedef fiyat |

## Architecture

The server is the brain; Matriks IQ is a data-and-order gateway. Both run on
the same Windows machine, and the gateway listens on loopback only.

```
FastAPI server (this repo)                Matriks IQ (matriks/TradeAiGateway.cs)
  scanner.py  ── every N min ──┐
    │                          ├─→ GET  127.0.0.1:8787/health
    │                          ├─→ GET  /snapshot?symbol=THYAO
    │                          ├─→ GET  /positions
    ├─ evaluator.py            │
    │    AI provider           │
    │    RiskEngine            │
    └─ order decision ─────────┴─→ POST /order   (LIMIT only)
                                        │
       /api/order-result ←──────────────┘  OnOrderUpdate reports fills
```

**Why this shape.** Matriks can expose a local HTTP port, so the server can
ask for data instead of waiting to be told. That removed the whole
multi-turn `FETCH_DATA` session protocol the old bot needed, along with
~1000 lines of session/planner code, and moved every trading decision into
Python where it can be tested.

**Safety is layered.** The scanner decides *whether* to send an order; the
gateway decides *whether it is allowed to*. The gateway's limits are fixed
in its algo parameters and cannot be raised by the server: LIMIT orders
only (it has no notion of MARKET), max qty and value per order, daily order
caps, locked long-term lots that can never be sold, and demo/real account
identity and REAL-account arming gates. Two independent brakes, one in each process.

Two environment switches gate the automation, both defaulting to off:
`SCANNER_ENABLED` starts the scan loop at all, and `SCANNER_ALLOW_ORDERS`
lets decisions become orders. With orders disabled every decision is forced
to PAPER regardless of the admin panel's trading mode.

The Matriks-side gateway is configured through its algo parameters
(`Port`, `ApiToken`, `SymbolsCsv`, `LockedLongTermCsv`, `ServerBaseUrl`,
`ServerApiToken`, plus the order limits). `ApiToken` must match the
server's `MATRIKS_GATEWAY_TOKEN`.

Gateway `SafeDebug` events remain visible in Matriks IQ and are also sent to
the authenticated `POST /api/gateway-log` endpoint. The server stores them as
JSON lines in `logs/matriks.log`; signal evaluations remain in
`logs/signal.log`.

Verify the gateway end-to-end with:

```bash
python scripts/gateway_smoke.py
```

Additional authenticated gateway data endpoints:

- `GET /capabilities` — supported/licensed data surface metadata
- `GET /depth?symbol=THYAO&levels=25` — up to 25 bid/ask levels and imbalance
- `GET /indicators?symbol=THYAO` — RSI, EMA20/50, MACD and technical features
- `GET /news?symbol=THYAO&limit=50` — live news cached after gateway startup
- `GET /institutions?symbol=THYAO&limit=5` — ranked daily AKD buyers/sellers
- `GET /mkk` — explicit MKK/Takas capability status

AKD data requires the relevant Matriks licence. Matriks documents MKK/Takas
as a terminal analysis screen but does not currently publish an AlgoTrader C#
access method, so `/mkk` reports `supported=false` instead of fabricating data.

## Windows Server Deployment

The production target is a single Windows machine that already runs
Matriks IQ. Everything runs natively — **no Docker** (Docker Desktop
doesn't support Windows Server 2019 without WSL2, and there's no benefit
to it here: server, database, and gateway are all local processes talking
over loopback).

### 1. Prerequisites

- Matriks IQ installed, licensed, and able to log in unattended (set up
  autologon so the server survives a reboot without a human present).
- [Python 3.11 or 3.12](https://www.python.org/downloads/) (not 3.13+ —
  some pinned dependencies don't ship wheels for it yet). Check
  "Add python.exe to PATH" during install.
- [PostgreSQL](https://www.postgresql.org/download/windows/) — native
  Windows installer, not Docker. Note the password you set for the
  `postgres` superuser.
- [Git for Windows](https://git-scm.com/download/win).
- [NSSM](https://nssm.cc/download) (or use Task Scheduler) to run uvicorn
  as a Windows service so it starts on boot and survives RDP disconnects.

### 2. Clone and install

```powershell
git clone https://github.com/<you>/trade-ai-server.git C:\trade-ai-server
cd C:\trade-ai-server
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Create the database

In `psql` (or pgAdmin):

```sql
CREATE USER trade_ai WITH PASSWORD 'change-me';
CREATE DATABASE trade_ai OWNER trade_ai;
```

### 4. Configure `.env`

```powershell
copy .env.example .env
notepad .env
```

Key values for a production install:

```ini
APP_ENV=production
EVALUATION_API_TOKEN=<unique long random string>
GATEWAY_API_TOKEN=<different long random string>
ADMIN_API_TOKEN=<different long random string>
ADMIN_PASSWORD=<strong password>
AI_PROVIDER=deepseek
DEEPSEEK_API_KEY=<your key>
DATABASE_URL=postgresql+asyncpg://trade_ai:change-me@localhost:5432/trade_ai

MATRIKS_GATEWAY_URL=http://127.0.0.1:8787
MATRIKS_GATEWAY_TOKEN=<shared secret — must match the gateway's ApiToken>

SCANNER_ENABLED=true
SCANNER_ALLOW_ORDERS=false   # keep false until the parallel-run checklist below is done
```

`APP_ENV=production` makes the server refuse to start with a mock AI
provider, a SQLite database, or default tokens — see
[Environment Variables](#environment-variables) above for the full list
of checks.

### Production / VDS first-install order

Use this order on a new Windows server. The schema command is intentionally
manual; server startup never runs it in production.

1. Create `.env`.
2. Set `DATABASE_URL` to PostgreSQL.
3. Set strong, distinct `EVALUATION_API_TOKEN`, `GATEWAY_API_TOKEN`, and `ADMIN_API_TOKEN` values.
4. Set a strong `ADMIN_PASSWORD`.
5. Set a strong `MATRIKS_GATEWAY_TOKEN`.
6. Back up PostgreSQL, then run `python -m alembic upgrade head`.
7. Start `uvicorn app.main:app --host 127.0.0.1 --port 8000` (or the Windows service below).
8. Compile `matriks/TradeAiGateway.cs` in Matriks.
9. Run the gateway smoke test: `python scripts/gateway_smoke.py`.
10. Run a PAPER test.
11. Run one-lot `DEMO_LIVE` test.

Safe initial `.env` values:

```ini
APP_ENV=development
AI_PROVIDER=mock
DEFAULT_MODE=paper
SCANNER_ENABLED=false
SCANNER_ALLOW_ORDERS=false
```

Immediately before a controlled DEMO_LIVE test, use only after the PAPER
checks and gateway smoke test pass:

```ini
APP_ENV=development
AI_PROVIDER=deepseek
SCANNER_ENABLED=true
SCANNER_ALLOW_ORDERS=true
```

In the admin panel verify all of the following: `killSwitchEnabled=false`,
`tradingMode=DEMO_LIVE` (or `botMode=DEMO_LIVE`),
the gateway account type is `DEMO`, and REAL account arming is disabled.
`REAL_LIVE` remains disabled in this phase,
including in production. `LIVE` is not accepted as a real-order alias; the
gateway resolves it to `PAPER`.

### DEMO_LIVE emergency stop and reconciliation

For schema migrations, gateway deployments, idempotency/callback changes, or
open-order reconciliation, first set `SCANNER_ALLOW_ORDERS=false`. Then:

1. Enable the backend kill switch and reload gateway configuration.
2. Verify `/health` reports the expected `configVersion`, then inspect
   `/orders/active`.
3. Cancel only the required open orders, and stop the gateway only if needed.
4. Reconcile gateway orders and positions against backend `order_logs` before
   setting `SCANNER_ALLOW_ORDERS=true` again.

Every DEMO_LIVE order refreshes `GetTradeUser()` if its account check is more
than five seconds old. A failed check, a non-demo account, or a changed account
blocks the order.

### 5. Install the Matriks gateway

1. Open `matriks/TradeAiGateway.cs` and copy it into a new algo in Matriks
   IQ's algo editor.
2. Set its parameters:

   | Parameter | Value | Notes |
   |---|---|---|
   | `Port` | `8787` | must match `MATRIKS_GATEWAY_URL` |
   | `ApiToken` | same as `MATRIKS_GATEWAY_TOKEN` in `.env` | shared secret |
   | `SymbolsCsv` | e.g. `THYAO,AKBNK,SISE,KCHOL,TUPRS,ANELE` | tradeable universe |
   | `LockedLongTermCsv` | e.g. `THYAO:100,ASELS:50` | lots that can never be sold |
   | `ServerBaseUrl` | `http://127.0.0.1:8000` | where this FastAPI server listens |
   | `ServerApiToken` | same as `GATEWAY_API_TOKEN` in `.env` | used for callback and config APIs |
   | `EnableDemoOrders` / `EnableRealOrders` | `false` / `false` | flip on only when you're ready (see checklist) |
   | `MaxOrderValueTl`, `MaxQtyPerOrder`, `MaxOrdersPerDay`, `MaxOrdersPerSymbolPerDay` | conservative values | hard caps the server cannot override |
   | `OrderTimeInForce` | `Day` | or `GTC` |

3. Start the algo. It binds to `127.0.0.1:8787` only — nothing is exposed
   to the network.
4. Verify: `python scripts/gateway_smoke.py` from the repo root, on the
   same machine.

### 6. Run the server as a Windows service

Using NSSM:

```powershell
nssm install TradeAiServer "C:\trade-ai-server\.venv\Scripts\python.exe" ^
  "-m uvicorn app.main:app --host 0.0.0.0 --port 8000"
nssm set TradeAiServer AppDirectory "C:\trade-ai-server"
nssm start TradeAiServer
```

Check liveness with `curl http://localhost:8000/api/health/live`, then verify
readiness with `curl http://localhost:8000/api/health/ready`.

### 7. Remote access to the admin panel

Port 8000 is **not** exposed to the internet — no port forwarding, no
DDNS. Install [Tailscale](https://tailscale.com/) on the server and on
whatever device you check the panel from; they join the same private
network with no open ports. Then browse to
`http://<tailscale-machine-name>:8000/admin`. The gateway's port 8787
stays loopback-only regardless — Tailscale never sees it.

### 8. Parallel run and cutover

If an older Matriks bot is already trading live, don't cut over blind:

1. Keep the old bot running. Start this stack with `SCANNER_ENABLED=true`,
   `SCANNER_ALLOW_ORDERS=false` — it will evaluate every scan interval and
   log decisions to `ai_decisions`/`risk_decisions`, but never place an
   order.
2. Compare its decisions against the old bot's for a few days.
3. Once you trust it, flip `SCANNER_ALLOW_ORDERS=true` and set the admin
   panel's trading mode to `DEMO_LIVE` — only DEMO_LIVE decisions become
   orders at this stage; `REAL_LIVE` stays blocked in code until a later,
   deliberate change.
4. Only then stop the old bot.

### Order Result

Matriks IQ bir emri gerçekleştirdiğinde bu endpoint'e sonucu bildirir.
`order_logs` tablosuna kaydedilir.

```bash
curl -X POST http://localhost:8000/api/order-result \
  -H "Authorization: Bearer ***" \
  -H "Content-Type: application/json" \
  -d '{
    "requestId": "sig-001",
    "symbol": "THYAO",
    "action": "BUY",
    "qty": 100,
    "price": 71.25,
    "status": "FILLED",
    "matriksMessage": "Order accepted by exchange",
    "orderId": "EXCH-12345"
  }'
```

**Response (200):**
```json
{"status": "ok"}
```

**Request Alanları:**

| Alan | Tip | Zorunlu | Açıklama |
|---|---|---|---|
| `requestId` | string | ✅ | Signal isteğiyle eşleşen ID |
| `symbol` | string | ✅ | Sembol |
| `action` | string | ✅ | `BUY` / `SELL` |
| `qty` | float | ✅ | Gerçekleşen lot |
| `price` | float | ✅ | Gerçekleşen fiyat |
| `status` | string | ✅ | `FILLED` / `REJECTED` / `CANCELED` |
| `matriksMessage` | string | ✅ | Matriks'ten dönen ham mesaj/hata |
| `orderId` | string | ❌ | Borsa emir ID'si |

DB hatası endpoint'i çökertmez — hata loglanır, istemci her zaman `{"status": "ok"}` alır.

## Dev DB Notes

Geliştirme ortamında `APP_ENV=development` ile SQLite (`dev.db`) kullanılır
ve tablolar ilk istekte `create_all` ile otomatik oluşturulur. **Yeni sütun eklendiğinde**
(örn. `matrix_message`) `create_all` mevcut tabloyu ALTER etmez — sütun eksik kalır.

Çözüm: `dev.db` dosyasını silip sunucuyu yeniden başlatın:
```bash
rm -f dev.db
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```
Tablolar yeniden oluşturulacak ve tüm sütunlar mevcut olacak.

**Not:** Production ortamında (`APP_ENV=production`) PostgreSQL + Alembic migration
kullanılması önerilir. Şu an migration sistemi kurulu değil.

## Risk Configuration

Trading safety rules live in `app/core/risk_config.py` and are loaded from
`RISK_*` environment variables with sensible defaults.

| Rule                        | Default                 | Env Var                        | Description                              |
|-----------------------------|-------------------------|--------------------------------|------------------------------------------|
| Allowed symbols             | `THYAO,AKBNK,SISE,...`  | `RISK_ALLOWED_SYMBOLS`         | Comma-separated tradeable symbols        |
| Locked long-term symbols    | `ASELS,EREGL`           | `RISK_LOCKED_LONG_TERM_SYMBOLS`| Never auto-sold                          |
| Max position per symbol     | `3000`                  | `RISK_MAX_POSITION_VALUE_PER_SYMBOL` | TL limit per symbol               |
| Max daily trades            | `3`                     | `RISK_MAX_DAILY_TRADE_COUNT`   | Hard cap per day using request count or DB fallback |
| Min confidence for BUY      | `75`                    | `RISK_MIN_CONFIDENCE_FOR_BUY`  | Score threshold (0–100)                  |
| Min confidence for SELL     | `70`                    | `RISK_MIN_CONFIDENCE_FOR_SELL` | Score threshold (0–100)                  |
| AlphaTrend alignment        | `true`                  | `RISK_REQUIRE_ALPHA_TREND_ALIGNMENT` | Opposing `alphaTrendSignal` blocks BUY/SELL |
| Indicator consensus alignment | `true`                | `RISK_REQUIRE_INDICATOR_CONSENSUS_ALIGNMENT` | Strong opposing `indicatorConsensus` blocks BUY/SELL |
| Min consensus count         | `4`                     | `RISK_MIN_INDICATOR_CONSENSUS_COUNT` | Same-side count needed for strong consensus |
| Max nATR for BUY            | `8.0`                   | `RISK_MAX_NATR_FOR_BUY`        | Blocks new BUY above this normalized ATR percent |
| Max depth queue drop for BUY | `35.0`                 | `RISK_MAX_DEPTH_QUEUE_DROP_PCT_FOR_BUY` | Blocks new BUY when bid queue weakens too much |
| Allow sell long-term        | `false`                 | `RISK_ALLOW_SELL_LONG_TERM`    | Override locked symbol protection        |
| Allow short selling         | `false`                 | `RISK_ALLOW_SHORT_SELLING`     | Enable short positions                   |
| Trading cutoff time         | `17:30`                 | `RISK_DISABLE_TRADING_AFTER`  | HH:MM — no trades past this time         |
| Trading timezone            | `Europe/Istanbul`       | `RISK_TIMEZONE`                | IANA timezone used for cutoff checks     |

**How it works:**
- `risk_config.is_symbol_allowed("THYAO")` → `True` / `False`
- `risk_config.is_long_term_locked("ASELS")` → `True` / `False`
- `risk_config.can_trade_now()` → `True` before 17:30 in `RISK_TIMEZONE`, `False` after
- `risk_config.get_min_confidence("BUY")` → `75.0`

Override any rule via `.env`:
```bash
RISK_ALLOWED_SYMBOLS=THYAO,AKBNK,GARAN,YKBNK
RISK_MAX_DAILY_TRADE_COUNT=5
RISK_DISABLE_TRADING_AFTER=18:00
RISK_TIMEZONE=Europe/Istanbul
RISK_MAX_NATR_FOR_BUY=8
RISK_MAX_DEPTH_QUEUE_DROP_PCT_FOR_BUY=35
```

## Project Structure

```
trade-ai-server/
├── app/
│   ├── main.py               # FastAPI entry point (starts the scanner)
│   ├── config.py             # Pydantic settings (.env)
│   ├── core/                 # Core utilities
│   ├── db/                   # Database layer
│   ├── models/               # Pydantic / SQLAlchemy models
│   ├── routers/              # API route handlers
│   └── services/
│       ├── scanner.py        # Background scan loop → evaluator → orders
│       ├── evaluator.py      # The brain: snapshot → AI → RiskEngine
│       ├── matriks_gateway.py# HTTP client for the Matriks gateway
│       ├── position_sync.py  # Gateway positions → bot_positions
│       └── risk_engine.py    # Safety gates
├── matriks/
│   └── TradeAiGateway.cs     # Matriks IQ algo: data + order gateway
├── scripts/gateway_smoke.py  # End-to-end gateway check
├── requirements.txt
├── .env.example
└── README.md
```
