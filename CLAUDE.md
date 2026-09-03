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

# Coverage (fail_under gate lives in pyproject.toml [tool.coverage.report])
pytest --cov=app --cov-report=term-missing

# Repository integration tests need a live, migrated database. They skip
# silently without one, so run them before touching app/repositories/.
docker compose up -d db
alembic upgrade head
pytest tests/test_repositories_integration.py

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

Most tests use **in-memory fakes** (not mocks) defined in `tests/fake_deps.py`. Each fake implements the same interface as the real repo/service (e.g., `FakeSessionsRepo`, `FakeMediaRepo`, `FakeGeminiService`, `FakeStorageClient`). Tests override FastAPI dependencies to inject fakes — no real database or external services needed.

Because the fakes stand in for the real classes everywhere, two suites keep them honest:

- `tests/test_repo_contracts.py` compares each fake's method names, signatures, keyword-only args and async-ness against the real class. A renamed repository method fails here instead of passing silently.
- `tests/test_repositories_integration.py` runs the real SQL against Postgres — ordering clauses, cascade deletes, server defaults, JSON round-trips. **Skipped when no database is reachable**, so run it (or check CI) when changing `app/repositories/`. Each test runs in a transaction that is rolled back, so no cleanup is needed. Note that Postgres `now()` is transaction-scoped: rows written in one test share a `created_at`, so ordering assertions must backdate explicitly (see `_backdate`).

`tests/conftest.py` sets dummy env vars, then **disables `Settings`' `env_file`** so the developer's real `.env` cannot leak into a test run — without that, any setting not stubbed there would differ between local runs and CI. It also provides shared `client` / `auth_headers` / `user_id` fixtures, a `make_token()` helper, and an autouse guard that fails any test leaking a FastAPI dependency override.

## Key Conventions

- All database operations are async (asyncpg + SQLAlchemy async sessions).
- Auth uses Supabase-issued JWTs verified server-side with `python-jose`. The `AuthUser` object (from JWT) carries `user_id` and `email`.
- App is created via factory function `create_app()` in `app/main.py`.
- Ruff config: line-length 100, target Python 3.12, lint rules: E, F, I, B, UP.
