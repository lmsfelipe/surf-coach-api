# Surf Coach API

FastAPI backend for the Surf Coaching Platform. Allows surfers to upload session videos, get AI-powered feedback, and track progress over time.

## Stack

- **Python 3.12** + **FastAPI** + **SQLAlchemy 2 (async)**
- **PostgreSQL** via `asyncpg`
- **Alembic** for migrations
- **Supabase** for auth and media storage
- **Google Gemini** for AI video analysis
- **Docker / Docker Compose** for local development

## Features

- JWT-based auth (Supabase)
- Session and media management
- Frame extraction from surf videos (OpenCV)
- AI-generated coaching reviews (Gemini)
- Health check endpoint

## Getting Started

### 1. Clone and configure

```bash
git clone git@github.com:lmsfelipe/surf-coach-api.git
cd surf-coach-api
cp .env.example .env
```

Fill in the values in `.env` (Supabase credentials, Gemini API key, etc.).

### 2. Run with Docker

```bash
docker compose up --build
```

This starts the PostgreSQL database and the API server with live reload. Migrations run automatically on startup.

The API will be available at `http://localhost:8000`.

### 3. Run locally (without Docker)

```bash
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload
```

## Environment Variables

See [.env.example](.env.example) for all required variables.

| Variable | Description |
|---|---|
| `DATABASE_URL` | Async SQLAlchemy DSN (`postgresql+asyncpg://...`) |
| `SUPABASE_URL` | Supabase project URL |
| `SUPABASE_ANON_KEY` | Supabase anon/public key |
| `SUPABASE_SERVICE_ROLE_KEY` | Supabase service role key |
| `SUPABASE_JWT_SECRET` | JWT secret from Supabase dashboard |
| `GEMINI_API_KEY` | Google Gemini API key |
| `GEMINI_MODEL` | Gemini model to use (default: `gemini-2.0-flash`) |
| `SUPABASE_BUCKET` | Supabase storage bucket name |

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/health` | Health check |
| `POST` | `/auth/register` | Register user |
| `POST` | `/auth/login` | Login |
| `GET` | `/auth/me` | Current user profile |
| `POST` | `/sessions` | Create a surf session |
| `GET` | `/sessions` | List sessions |
| `POST` | `/media/upload` | Upload session video |
| `GET` | `/media/{session_id}` | List session media |
| `POST` | `/reviews/{session_id}` | Generate AI review |
| `GET` | `/reviews/{session_id}` | Get session reviews |
| `POST` | `/ai/analyze` | Direct AI analysis |

## Development

```bash
# Lint
ruff check .

# Format
ruff format .

# Tests
pytest
```

## Migrations

```bash
# Create a new migration
alembic revision --autogenerate -m "description"

# Apply migrations
alembic upgrade head

# Rollback one step
alembic downgrade -1
```
