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
| `AI_PROVIDER`       | Yes      | `mock`                  | `mock` (dev) / `deepseek` / `openai` / `anthropic` |
| `DEEPSEEK_API_KEY`  | *        | —                       | Required when `AI_PROVIDER=deepseek`     |
| `DEEPSEEK_MODEL`    | No       | `deepseek-chat`         | Model name                               |
| `DATABASE_URL`      | Prod     | `sqlite+aiosqlite:///./dev.db` (dev) | PostgreSQL for production, SQLite auto for dev |
| `POSTGRES_PASSWORD`  | Prod    | —                       | PostgreSQL password (used by docker compose) |
| `TELEGRAM_BOT_TOKEN`| No       | —                       | Telegram bot token                       |
| `TELEGRAM_CHAT_ID`  | No       | —                       | Default chat ID                          |
| `DEFAULT_MODE`      | No       | `paper`                 | `paper` / `live`                         |

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

## API Endpoints

| Method | Path                    | Auth     | Description              |
|--------|-------------------------|----------|--------------------------|
| GET    | `/`                     | —        | Root (docs links)        |
| GET    | `/api/health`           | —        | Health check             |
| POST   | `/api/signal/evaluate`  | Bearer   | Evaluate trading signal  |
| POST   | `/api/order-result`     | Bearer   | Receive order result     |
| GET    | `/docs`                 | —        | Swagger UI               |
| GET    | `/redoc`                | —        | ReDoc                    |

### Signal Evaluate

```bash
# PAPER mode — always returns allowOrder: false, requiresConfirmation: false
curl -X POST http://localhost:8000/api/signal/evaluate \
  -H "Authorization: Bearer ***" \
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

**Response (200):**
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
| Max daily trades            | `3`                     | `RISK_MAX_DAILY_TRADE_COUNT`   | Hard cap per day                         |
| Min confidence for BUY      | `75`                    | `RISK_MIN_CONFIDENCE_FOR_BUY`  | Score threshold (0–100)                  |
| Min confidence for SELL     | `70`                    | `RISK_MIN_CONFIDENCE_FOR_SELL` | Score threshold (0–100)                  |
| Allow sell long-term        | `false`                 | `RISK_ALLOW_SELL_LONG_TERM`    | Override locked symbol protection        |
| Allow short selling         | `false`                 | `RISK_ALLOW_SHORT_SELLING`     | Enable short positions                   |
| Trading cutoff time         | `17:30`                 | `RISK_DISABLE_TRADING_AFTER`  | HH:MM — no trades past this time         |

**How it works:**
- `risk_config.is_symbol_allowed("THYAO")` → `True` / `False`
- `risk_config.is_long_term_locked("ASELS")` → `True` / `False`
- `risk_config.can_trade_now()` → `True` before 17:30, `False` after
- `risk_config.get_min_confidence("BUY")` → `75.0`

Override any rule via `.env`:
```bash
RISK_ALLOWED_SYMBOLS=THYAO,AKBNK,GARAN,YKBNK
RISK_MAX_DAILY_TRADE_COUNT=5
RISK_DISABLE_TRADING_AFTER=18:00
```

## Project Structure

```
trade-ai-server/
├── app/
│   ├── main.py          # FastAPI entry point
│   ├── config.py        # Pydantic settings (.env)
│   ├── core/            # Core utilities
│   ├── db/              # Database layer
│   ├── models/          # Pydantic / SQLAlchemy models
│   ├── routers/         # API route handlers
│   │   └── health.py    # Health check endpoint
│   └── services/        # Business logic
├── requirements.txt
├── .env.example
├── .gitignore
├── docker-compose.yml
└── README.md
```
