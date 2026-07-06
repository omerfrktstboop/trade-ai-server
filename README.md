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
| `AI_PROVIDER`       | Yes      | `deepseek`              | `openai` / `deepseek` / `anthropic`      |
| `DEEPSEEK_API_KEY`  | Prod     | —                       | DeepSeek API key                         |
| `DEEPSEEK_MODEL`    | No       | `deepseek-chat`         | Model name                               |
| `DATABASE_URL`      | No       | —                       | PostgreSQL / SQLite connection string    |
| `TELEGRAM_BOT_TOKEN`| No       | —                       | Telegram bot token                       |
| `TELEGRAM_CHAT_ID`  | No       | —                       | Default chat ID                          |
| `DEFAULT_MODE`      | No       | `paper`                 | `paper` / `live`                         |

**Production safety:** When `APP_ENV=production`, the server will refuse to start
if `API_TOKEN` is empty, still set to the dev default, or if the selected AI
provider's API key is missing.

## Docker

```bash
docker compose up --build
```

## API Endpoints

| Method | Path          | Description        |
|--------|---------------|--------------------|
| GET    | `/`           | Root (docs links)  |
| GET    | `/api/health` | Health check       |
| GET    | `/docs`       | Swagger UI         |
| GET    | `/redoc`      | ReDoc              |

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
