# SPEC — CI Pipeline (GitHub Actions)

**Status:** Proposed
**Owner:** Backend
**Date:** 2026-07-14

---

## 1. Problem

The project has 55 tests and `ruff` configured for linting/formatting, but no
CI pipeline. Nothing prevents a broken commit from being merged or deployed.
Regressions can reach production undetected.

## 2. Goals

- Every push and pull request runs **lint + format check + tests** automatically.
- PRs cannot be merged with failing checks (branch protection).
- Fast feedback — pipeline completes in under 3 minutes.
- Tests run against a real PostgreSQL instance (not SQLite) to match production.

## 3. Non-goals

- CD (continuous deployment) — deployment strategy is a separate concern.
- Docker image builds in CI (can be added later).
- E2E / integration tests against external services (Supabase, Gemini).

---

## 4. Design

### 4.1 Workflow file

`.github/workflows/ci.yml` — runs on every push to `main` and every PR.

```yaml
name: CI

on:
  push:
    branches: [main]
  pull_request:
    branches: [main]

jobs:
  lint:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install ruff
      - run: ruff check .
      - run: ruff format --check .

  test:
    runs-on: ubuntu-latest
    services:
      postgres:
        image: postgres:16
        env:
          POSTGRES_USER: test
          POSTGRES_PASSWORD: test
          POSTGRES_DB: surf_coach_test
        ports:
          - 5432:5432
        options: >-
          --health-cmd="pg_isready -U test"
          --health-interval=5s
          --health-timeout=5s
          --health-retries=5
    env:
      DATABASE_URL: postgresql+asyncpg://test:test@localhost:5432/surf_coach_test
      APP_ENV: development
      SUPABASE_URL: https://fake.supabase.co
      SUPABASE_ANON_KEY: fake-anon-key
      SUPABASE_SERVICE_ROLE_KEY: fake-service-role-key
      SUPABASE_JWT_SECRET: fake-jwt-secret-at-least-32-characters-long
      GEMINI_API_KEY: fake-gemini-key
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
          cache: pip
      - run: pip install -e ".[dev]"
      - run: alembic upgrade head
      - run: pytest --tb=short -q
```

### 4.2 Job structure

| Job | Purpose | Duration (est.) |
|---|---|---|
| `lint` | `ruff check` + `ruff format --check` | ~15 s |
| `test` | `alembic upgrade head` + `pytest` against Postgres 16 | ~90 s |

Jobs run in **parallel** — total wall time ≈ 90 s.

### 4.3 Branch protection

After the workflow is live, enable on `main`:

- Require status checks to pass: `lint`, `test`
- Require branches to be up to date before merging

### 4.4 Test environment

Tests use **fake/mock dependencies** (the project already has `tests/fake_deps.py`)
so no real Supabase or Gemini credentials are needed. The `DATABASE_URL` points
to the CI Postgres service.

The `SUPABASE_JWT_SECRET` and other required env vars are set to dummy values —
tests that validate JWTs use the test secret directly.

---

## 5. Acceptance criteria

- [ ] Push to `main` triggers the workflow and both jobs pass.
- [ ] A PR with a ruff violation shows a failing `lint` check.
- [ ] A PR with a broken test shows a failing `test` check.
- [ ] Migrations run successfully against a clean Postgres 16 database.
- [ ] No real API keys or secrets are required in CI.
- [ ] Pipeline completes in under 3 minutes.

## 6. Future enhancements (out of scope)

- **Coverage reporting** — upload to Codecov or similar.
- **Docker build** — build and push the production image on merge to `main`.
- **Deploy step** — trigger deployment after CI passes.
- **Matrix testing** — test against multiple Python versions.
- **Security scanning** — `pip-audit` or `safety` for dependency vulnerabilities.
