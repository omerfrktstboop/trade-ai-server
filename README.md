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

# 5. Run the server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

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
│   ├── config.py        # Pydantic settings
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
