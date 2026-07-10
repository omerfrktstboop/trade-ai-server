# trade-ai-server

Modular FastAPI backend for AI-powered trading. Clean, typed, and extensible.

## Quick Start

```bash
# 1. Clone and enter
git clone https://github.com/YOUR_USER/trade-ai-server.git
cd trade-ai-server

# 2. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and configure environment
cp .env.example .env
# Edit .env and fill in your keys

# 5. Run the server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

## Environment Variables

Copy `.env.example` → `.env` and fill in the required values:

| Variable            | Required | Default                 | Description                              |
|---------------------|----------|-------------------------|------------------------------------------|
| `APP_ENV`           | Yes      | `development`           | `development` / `staging` / `production` |
| `API_TOKEN`         | Prod     | `dev-token-change-me`   | API auth token                           |
| `AI_PROVIDER`       | Yes      | `mock`                  | `mock` (dev) / `deepseek`. `openai` / `anthropic` are accepted by config validation but **not implemented yet** — selecting them raises `ValueError` at first signal evaluation. |
| `DEEPSEEK_API_KEY`  | *        | —                       | Required when `AI_PROVIDER=deepseek`     |
| `DEEPSEEK_MODEL`    | No       | `deepseek-chat`         | Model name                               |
| `DATABASE_URL`      | Prod     | `sqlite+aiosqlite:///./dev.db` (dev) | PostgreSQL for production, SQLite auto for dev |
| `POSTGRES_PASSWORD`  | Prod    | —                       | PostgreSQL password (used by docker compose) |
| `TELEGRAM_BOT_TOKEN`| No       | —                       | Telegram bot token                       |
| `TELEGRAM_CHAT_ID`  | No       | —                       | Default chat ID                          |
| `DEFAULT_MODE`      | No       | `paper`                 | `paper` / `live` / `demo_live` / `real_live` — fallback mode used when a request doesn't specify one. `manual` is only valid as a **per-request** `mode` field, not as `DEFAULT_MODE`. |

\* `DEEPSEEK_API_KEY` is only required in production or when `AI_PROVIDER=deepseek`.

**Production safety:** When `APP_ENV=production`, the server will refuse to start if:
- `API_TOKEN` is empty or still set to the dev default
- `AI_PROVIDER=mock` (mock is not allowed in production)
- `AI_PROVIDER=deepseek` but `DEEPSEEK_API_KEY` is empty
- `DATABASE_URL` is missing or uses SQLite

**Database:** In development, `DATABASE_URL` can be left empty — the server
auto-creates a SQLite database (`dev.db`) on first request. No PostgreSQL
setup needed for local development. In production, PostgreSQL is required:
`DATABASE_URL` must be set and must not be SQLite.

## Docker

`docker compose up` starts both the API server and a **PostgreSQL** database
(postgres:16-alpine). It is the recommended way to run the full stack for
staging and production.

```bash
# Copy env template and set a DB password
cp .env.example .env
# Edit .env → set POSTGRES_PASSWORD and uncomment the production DATABASE_URL

# Start both API + PostgreSQL
docker compose up --build
```

| Service    | Port | Credentials                      |
|------------|------|----------------------------------|
| API        | 8000 | Bearer token: `API_TOKEN`        |
| PostgreSQL | 5432 | `trade_ai` / `trade_ai` / from `.env` |

The API waits for PostgreSQL to be healthy before starting (`depends_on` + healthcheck).

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
| 4 | **MANUAL test** | `mode=MANUAL` ile test istekleri | `requiresConfirmation=true`, emir otomatik gönderilmez |
| 5 | **LIVE test (gerçek AI)** | `mode=LIVE` test istekleri | `allowOrder=true` olabilir, emirler gerçek gönderilir |
| 6 | **Uzun vade lotlar** | `RISK_LOCKED_LONG_TERM_SYMBOLS=ASELS,EREGL` ayarlandı mı? | Kilitli semboller asla satılmaz |
| 7 | **Günlük limit** | `RISK_MAX_DAILY_TRADE_COUNT=3` doğru mu? | Aşılınca BUY/SELL bloklanır |
| 8 | **Cutoff saati** | `RISK_DISABLE_TRADING_AFTER=17:30`, `RISK_TIMEZONE=Europe/Istanbul` doğru mu? | Sonrası sadece WAIT |
| 9 | **PostgreSQL** | `DATABASE_URL=postgresql+asyncpg://...` ayarlandı mı? | Production'da SQLite çalışmaz |
| 10 | **Docker healtcheck** | `docker compose up --build` + `/api/health` → 200 | Tüm servisler ayakta |

> **⚠️ LIVE mod gerçek emir gönderir.** Sadece PAPER → MANUAL → LIVE sırasıyla
> test ettikten ve tüm kontrolleri tamamladıktan sonra aktif edin.

## API Endpoints

| Method | Path                    | Auth     | Description              |
|--------|-------------------------|----------|--------------------------|
| GET    | `/`                     | —        | Root (docs links)        |
| GET    | `/api/health`           | —        | Health check             |
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
- Browser login uses `ADMIN_PASSWORD`; admin API calls also accept the existing Bearer `API_TOKEN`.
- Secrets are not exposed by admin config endpoints: `API_TOKEN`, `DEEPSEEK_API_KEY`, and `DATABASE_URL` are not returned.
- Config edits are stored in `system_configs`; every changed value writes `config_audit_logs`.
- Risky changes require confirmation value `CONFIRM`: switching `tradingMode` or `botMode` to a live mode, disabling `killSwitchEnabled`, and enabling `botEnableRealOrders`/`botDemoAccountConfirmed`.
- `killSwitchEnabled=true` makes `/api/signal/evaluate` return `WAIT` with `allowOrder=false`.
- If `tradingMode` exists in DB, it overrides the incoming request mode; otherwise request mode defaults remain unchanged.
- Order limits, confidence thresholds, signal filters, and scan interval are no longer standalone admin config keys — they're managed by the active Trade Profile (`/admin/trade-profiles`), which also requires `CONFIRM` for risky changes (raising limits, lowering confidence thresholds, disabling alignment guards, enabling `allowRealLive`, or activating a HIGH/EXTREME risk-level profile).

### Signal Evaluate

```bash
# PAPER mode — always returns allowOrder: false, requiresConfirmation: false
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
  "requiresConfirmation": false,
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
  "requiresConfirmation": false,
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
| `requiresConfirmation` | bool | `true` ise kullanıcıya sor (MANUAL mode), `false` ise onaysız |
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
confirmation flags. Two independent brakes, one in each process.

Two environment switches gate the automation, both defaulting to off:
`SCANNER_ENABLED` starts the scan loop at all, and `SCANNER_ALLOW_ORDERS`
lets decisions become orders. With orders disabled every decision is forced
to PAPER regardless of the admin panel's trading mode.

The Matriks-side gateway is configured through its algo parameters
(`Port`, `ApiToken`, `SymbolsCsv`, `LockedLongTermCsv`, `ServerBaseUrl`,
`ServerApiToken`, plus the order limits). `ApiToken` must match the
server's `MATRIKS_GATEWAY_TOKEN`.

Verify the gateway end-to-end with:

```bash
python scripts/gateway_smoke.py
```

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
