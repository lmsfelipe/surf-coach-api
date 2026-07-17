# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

FastAPI backend for a Surf Coaching Platform. Surfers upload session videos, receive AI-powered coaching reviews (via Google Gemini), and track progress. Auth and media storage are handled by Supabase.

## Commands

```bash
# Run with Docker (starts Postgres + API with live reload, runs migrations automatically)
docker compose up --build

# Run locally
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:app --reload

# Lint & format
ruff check .
ruff format .

# Tests (uses pytest-asyncio with asyncio_mode="auto")
pytest
pytest tests/test_sessions.py          # single file
pytest tests/test_sessions.py::test_create_session -v  # single test

# Migrations
alembic revision --autogenerate -m "description"
alembic upgrade head
alembic downgrade -1
```

## Architecture

**Layered structure** in `app/`:

- `api/` — FastAPI routers (auth, sessions, media, reviews, ai, surfboards, health). Each router maps to a resource.
- `services/` — Business logic layer. Services receive repository and infrastructure dependencies via constructor injection (not FastAPI `Depends`). Key services: `AuthService`, `SessionsService`, `MediaService`, `AIService` (wraps Gemini), `SurfboardsService`.
- `repositories/` — Async SQLAlchemy data access. Each repo handles CRUD for its domain model.
- `models/` — SQLAlchemy ORM models inheriting from `app.core.db.Base` (DeclarativeBase).
- `schemas/` — Pydantic request/response schemas.
- `core/` — Cross-cutting concerns:
  - `config.py` — `Settings` via pydantic-settings, loaded from `.env`, accessed via `get_settings()` (lru_cached).
  - `db.py` — Async engine + session factory (`SessionLocal`), `get_db()` generator.
  - `deps.py` — FastAPI dependencies: `get_current_user` (JWT verification), `db_session`.
  - `security/` — JWT verification (`verify_supabase_jwt`) and media token signing.
  - `storage.py` — Supabase Storage client wrapper.
  - `frame_extractor.py` — OpenCV-based video frame extraction.
  - `errors.py` — Custom exceptions and global exception handlers.

**Dependency flow**: Routers instantiate services with repos/clients, then call service methods. Services never import FastAPI directly.

## Testing

Tests use **in-memory fakes** (not mocks) defined in `tests/fake_deps.py`. Each fake implements the same interface as the real repo/service (e.g., `FakeSessionsRepo`, `FakeMediaRepo`, `FakeGeminiService`, `FakeStorageClient`). Tests override FastAPI dependencies to inject fakes — no real database or external services needed.

`tests/conftest.py` sets dummy env vars via `os.environ.setdefault` so `Settings` loads without a real `.env` file.

## Key Conventions

- All database operations are async (asyncpg + SQLAlchemy async sessions).
- Auth uses Supabase-issued JWTs verified server-side with `python-jose`. The `AuthUser` object (from JWT) carries `user_id` and `email`.
- App is created via factory function `create_app()` in `app/main.py`.
- Ruff config: line-length 100, target Python 3.12, lint rules: E, F, I, B, UP.
