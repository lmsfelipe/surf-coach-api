# SPEC — Phase 1: Backend Foundation (Auth & User Profile)

**Project:** Surf Coaching Platform — MVP
**Phase:** 1 of N
**Scope:** FastAPI backend bootstrapping, Supabase Auth integration, user profile persistence
**Target environment:** Docker for local dev (no Python / Postgres installed on the host)

---

## 1. Phase Goal

Deliver a runnable FastAPI service that can:

1. Be spun up with a single `docker compose up` command — no Python, no local DB, nothing on the host except Docker.
2. Verify Supabase-issued JWTs on protected routes.
3. On first authenticated request, auto-provision a `profile` row in Postgres linked to the Supabase user (`auth.users.id`), storing surf-specific fields (`surf_level`, `height_cm`, `weight_kg`).
4. Expose endpoints so the developer can, from Postman end-to-end:
   - Register a user against Supabase Auth (hitting Supabase's REST API directly).
   - Log in against Supabase Auth and get a JWT.
   - Hit FastAPI's `GET /me` with that JWT and get back the merged user + profile payload.
   - Hit FastAPI's `PATCH /me` to update the profile fields.

The definition of done: Postman runs `register → login → me → patch /me → me` and everything succeeds with data persisted in the Supabase Postgres DB.

---

## 2. Architecture Principles

This phase applies a **pragmatic subset of Clean Architecture** — enough to keep the code decoupled and testable without over-engineering an MVP.

### Layers

```
src/
├── api/          → Presentation layer: routes, request/response schemas (Pydantic)
├── services/     → Business logic layer: orchestration, rules, no HTTP/DB concerns
├── repositories/ → Data access layer: all SQLAlchemy queries live here
├── models/       → SQLAlchemy ORM models (DB representation)
├── schemas/      → Pydantic schemas (API representation)
└── core/         → Config, security, DB session, shared utilities
```

Files are organized flat within each layer, named by domain:

```
api/auth.py          services/auth.py          repositories/auth.py
api/sessions.py      services/sessions.py      repositories/sessions.py
api/media.py         services/media.py         repositories/media.py
api/ai.py            services/ai.py            repositories/ai.py
```

### Layer responsibilities (hard rules — no mixing)

- **`api/`** handles HTTP only: parse the request, call a service, return a response. No SQLAlchemy, no business logic.
- **`services/`** handles business logic only: orchestration, rules, decisions. Receives repositories via FastAPI `Depends()`. No `Request`/`Response` objects, no raw SQL.
- **`repositories/`** handles data access only: all SQLAlchemy queries live here, nowhere else. Returns ORM model instances or primitives — never HTTP-aware types.
- **`models/`** defines the DB shape (SQLAlchemy). Not imported by `api/` directly.
- **`schemas/`** defines the API shape (Pydantic). Not imported by `repositories/` directly.
- **`core/`** is shared infrastructure: `config.py`, `security/jwt.py`, `db.py`, `errors.py`, `deps.py`.

### SOLID applied (MVP scope)

- **Single Responsibility** — each layer has exactly one reason to change (HTTP contract, business rule, or query shape).
- **Dependency Inversion** — routes depend on service interfaces, not implementations. Services receive repositories via FastAPI `Depends()`.
- **Open/Closed** — new review score categories or session fields should require adding, not modifying, existing code.

> Liskov and Interface Segregation are acknowledged but not strictly enforced at MVP scale.

---

## 3. Why This Split (Auth vs. Profile)

Supabase Auth (GoTrue) owns: `email`, `password_hash`, email confirmation, password reset, session tokens. These live in Supabase's managed `auth.users` table — we never touch them directly from application code.

Our FastAPI service owns: everything **domain-specific** — `surf_level`, `height_cm`, `weight_kg`, and later on `sessions`, `media`, `reviews`. These live in a `public.profiles` table in the same Supabase Postgres instance, with `profiles.id` as a foreign key to `auth.users.id`.

This is the standard Supabase pattern and it's important to get right in Phase 1 because every future table (`sessions`, `media`, etc.) will FK to `profiles.id`.

---

## 4. Out of Scope (Phase 1)

These belong to later phases and must **not** be implemented here:

- Surf sessions, media upload, AI analysis, session history (stub files scaffolded across all layers — see §6).
- Password reset / email verification UI flows (Supabase handles the mechanics; we don't wire them yet).
- Social login.
- Supabase Storage bucket setup (Phase 2, when media upload arrives).
- Gemini Vision integration (Phase 3).
- Row Level Security policies beyond the minimum needed for the `profiles` table.
- Frontend Vue app (separate phase).
- Production deploy to Railway / Vercel (local Docker dev only for this phase).

---

## 5. Tech Stack

| Layer | Choice | Rationale |
|---|---|---|
| Frontend | Vue.js | User's primary stack *(not built in Phase 1)* |
| Backend | FastAPI (Python 3.12) | Best fit for AI pipeline work (OpenCV, Gemini, Pydantic) |
| Database | PostgreSQL via Supabase | Managed Postgres + built-in auth + storage bucket |
| Auth | **Supabase Auth (GoTrue)** | JWT issued by Supabase; FastAPI verifies it |
| File storage | Supabase Storage (S3-compatible) | *(Phase 2)* |
| AI | Gemini Vision API | *(Phase 3)* |
| Frontend deploy | Vercel | *(later phase)* |
| Backend deploy | Railway | *(later phase)* |
| Containerization (dev) | Docker + Docker Compose | Zero local installs |

### Python dependencies (Phase 1)

```
fastapi
uvicorn[standard]
pydantic>=2
pydantic-settings
sqlalchemy>=2
asyncpg
alembic
python-jose[cryptography]    # JWT verification
httpx                         # optional, for calling Supabase admin API
python-dotenv
```

Dev:
```
pytest
pytest-asyncio
httpx                         # test client
ruff
```

---

## 6. Project Structure

Follows the Clean Architecture layers defined in §2. Future domains (`sessions`, `media`, `ai`) add one file per layer — no new folders, no restructuring.

```
surf-coach-backend/
├── docker-compose.yml
├── Dockerfile
├── .dockerignore
├── .env.example
├── pyproject.toml
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
└── app/
    ├── __init__.py
    ├── main.py                         # FastAPI app factory — mounts all routers
    ├── api/                            # Presentation layer
    │   ├── __init__.py
    │   ├── auth.py                     # GET /me, PATCH /me
    │   ├── sessions.py                 # ← empty stub (no routes yet)
    │   ├── media.py                    # ← empty stub
    │   └── ai.py                       # ← empty stub
    ├── services/                       # Business logic layer
    │   ├── __init__.py
    │   ├── auth.py                     # profile provisioning, update logic
    │   ├── sessions.py                 # ← empty stub
    │   ├── media.py                    # ← empty stub
    │   └── ai.py                       # ← empty stub
    ├── repositories/                   # Data access layer
    │   ├── __init__.py
    │   ├── auth.py                     # get_profile, upsert_profile
    │   ├── sessions.py                 # ← empty stub
    │   ├── media.py                    # ← empty stub
    │   └── ai.py                       # ← empty stub
    ├── models/                         # SQLAlchemy ORM models
    │   ├── __init__.py
    │   └── profile.py                  # Profile model
    ├── schemas/                        # Pydantic schemas (API shapes)
    │   ├── __init__.py
    │   └── auth.py                     # ProfileOut, ProfileUpdate, AuthUser
    └── core/                           # Shared infrastructure
        ├── __init__.py
        ├── config.py                   # pydantic-settings — env vars + validation
        ├── db.py                       # async SQLAlchemy engine + get_db dependency
        ├── deps.py                     # get_current_user (JWT → AuthUser)
        ├── errors.py                   # AppError hierarchy + exception handlers
        └── security/
            ├── __init__.py
            └── jwt.py                  # verify_supabase_jwt()
```

### Stub convention

Each stub file in `api/`, `services/`, and `repositories/` exports a module-level symbol so `main.py` can import it unconditionally. For `api/` stubs this is an empty `APIRouter`:

```python
# app/api/sessions.py
from fastapi import APIRouter
router = APIRouter(prefix="/sessions", tags=["sessions"])
# endpoints added in Phase 2
```

For `services/` and `repositories/` stubs, a single `pass`-body class is sufficient:

```python
# app/services/sessions.py
class SessionsService:
    pass  # Phase 2
```

This keeps `main.py` stable across all phases — it never needs to change its import list as new domains are implemented.

---

## 7. Data Model (Phase 1 Only)

### Managed by Supabase (read-only from our perspective)

`auth.users` — handled entirely by Supabase Auth. Key columns we care about: `id` (uuid), `email`, `created_at`.

### Owned by us — `public.profiles`

| Column | Type | Constraints |
|---|---|---|
| `id` | UUID | PK, **FK → auth.users.id**, ON DELETE CASCADE |
| `surf_level` | TEXT (check constraint) | NOT NULL — one of `beginner`, `intermediate`, `advanced`, `pro` |
| `height_cm` | INTEGER | NULL, check 100–250 |
| `weight_kg` | INTEGER | NULL, check 30–200 |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default `now()`, touched on update |

### SQLAlchemy model sketch

```python
class Profile(Base):
    __tablename__ = "profiles"
    __table_args__ = {"schema": "public"}

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    surf_level: Mapped[str] = mapped_column(String, nullable=False)
    height_cm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    weight_kg: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
```

### Alembic migration (initial)

The first migration creates the `profiles` table with the FK to `auth.users.id`. Because `auth` is a Supabase-managed schema, the migration only references it — it does **not** create it.

```python
# alembic/versions/0001_init_profiles.py
def upgrade():
    op.execute("""
        CREATE TABLE public.profiles (
            id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
            surf_level TEXT NOT NULL CHECK (surf_level IN
                ('beginner','intermediate','advanced','pro')),
            height_cm INTEGER CHECK (height_cm BETWEEN 100 AND 250),
            weight_kg INTEGER CHECK (weight_kg BETWEEN 30 AND 200),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)
```

### Row Level Security (RLS)

Enable RLS on `public.profiles` and add two policies so users can only read/update their own row when accessed through the Supabase client key (defense in depth — FastAPI uses the service-role key which bypasses RLS, but policies protect against direct-from-client access in later phases):

```sql
ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY;

CREATE POLICY "profiles_select_own" ON public.profiles
    FOR SELECT USING (auth.uid() = id);

CREATE POLICY "profiles_update_own" ON public.profiles
    FOR UPDATE USING (auth.uid() = id);
```

---

## 8. Authentication Flow

```
┌─────────┐  POST /auth/v1/signup        ┌──────────────┐
│ Client  │ ───────────────────────────▶ │ Supabase     │
│(Postman)│ ◀─────────────────────────── │ Auth (GoTrue)│
└─────────┘   { access_token, user }     └──────────────┘
     │
     │ Authorization: Bearer <access_token>
     ▼
┌─────────┐    GET /me
│ FastAPI │ 1. Verify JWT (HS256, JWT_SECRET from Supabase)
│         │ 2. Extract sub (user_id) + email from claims
│         │ 3. Fetch profile by id; if missing, create with defaults
│         │ 4. Return { id, email, surf_level, height_cm, weight_kg, ... }
└─────────┘
```

### JWT verification (FastAPI side)

- Supabase signs access tokens with **HS256** using a secret available in the Supabase dashboard under *Settings → API → JWT Settings → JWT Secret*.
- FastAPI stores this secret as `SUPABASE_JWT_SECRET` and verifies every incoming token.
- Claims we rely on: `sub` (user UUID), `email`, `exp`, `aud` (must equal `"authenticated"`).
- A `get_current_user` FastAPI dependency extracts the `Authorization: Bearer <token>` header, verifies the JWT, and returns a lightweight `AuthUser(id: UUID, email: str)` object.

```python
# app/security/jwt.py — sketch
from jose import jwt, JWTError

def verify_supabase_jwt(token: str) -> AuthUser:
    try:
        payload = jwt.decode(
            token,
            settings.SUPABASE_JWT_SECRET,
            algorithms=["HS256"],
            audience="authenticated",
        )
    except JWTError as e:
        raise InvalidTokenError() from e
    return AuthUser(id=UUID(payload["sub"]), email=payload["email"])
```

### Profile auto-provisioning

On the **first** authenticated request for a given user, `get_current_profile` (a dependency that builds on `get_current_user`) will:

1. Look up `profiles` by `id = auth_user.id`.
2. If not found, INSERT a new row with `surf_level = 'beginner'` as a sensible default.
3. Return the ORM object.

This removes the need for a separate "complete your profile" step in Phase 1 — the profile is guaranteed to exist whenever the client holds a valid Supabase token. The user updates it via `PATCH /me`.

> Alternative: use a Supabase Postgres trigger on `auth.users` INSERT to create the profile row. That's cleaner long-term but couples the migration to Supabase-specific SQL. For Phase 1 we do it in application code, which is easier to test and move later.

---

## 9. API Contracts

Base URL (local FastAPI): `http://localhost:8000`
Supabase Auth URL (Postman only): `https://<PROJECT_REF>.supabase.co/auth/v1`

All FastAPI requests/responses are `application/json`. Timestamps are ISO 8601 UTC.

### 9.1 Register — hits Supabase directly (Postman)

This does **not** go through FastAPI. Postman calls Supabase Auth's REST API:

**Request**
```
POST https://<PROJECT_REF>.supabase.co/auth/v1/signup
apikey: <SUPABASE_ANON_KEY>
Content-Type: application/json

{
  "email": "surfer@example.com",
  "password": "SuperSecret123!"
}
```

**Response (abbreviated)**
```json
{
  "access_token": "eyJhbGciOi...",
  "token_type": "bearer",
  "expires_in": 3600,
  "refresh_token": "...",
  "user": { "id": "a7b2c3d4-...", "email": "surfer@example.com" }
}
```

> The Supabase project should have **email confirmation disabled** for Phase 1 dev (Dashboard → Authentication → Providers → Email → uncheck "Confirm email"). Otherwise `/signup` returns a user with no session until the link is clicked.

### 9.2 Login — hits Supabase directly (Postman)

```
POST https://<PROJECT_REF>.supabase.co/auth/v1/token?grant_type=password
apikey: <SUPABASE_ANON_KEY>
Content-Type: application/json

{
  "email": "surfer@example.com",
  "password": "SuperSecret123!"
}
```

Response shape matches `/signup`. Copy `access_token` — this is the Bearer token for FastAPI calls.

### 9.3 `GET /me` (FastAPI, protected)

Returns the Supabase user merged with the local profile. Auto-creates the profile on first call.

**Headers**
```
Authorization: Bearer <access_token from Supabase>
```

**Success — 200 OK**
```json
{
  "id": "a7b2c3d4-...",
  "email": "surfer@example.com",
  "surfLevel": "beginner",
  "heightCm": null,
  "weightKg": null,
  "createdAt": "2026-04-16T12:00:00.000Z",
  "updatedAt": "2026-04-16T12:00:00.000Z"
}
```

**Errors**
- `401 MISSING_TOKEN` — no `Authorization` header.
- `401 INVALID_TOKEN` — malformed, expired, wrong audience, or bad signature.

### 9.4 `PATCH /me` (FastAPI, protected)

Updates the local profile. All fields optional; only provided ones are updated.

**Request body**
```json
{
  "surfLevel": "intermediate",
  "heightCm": 180,
  "weightKg": 75
}
```

**Validation (Pydantic)**
- `surfLevel`: optional, one of `beginner` | `intermediate` | `advanced` | `pro`.
- `heightCm`: optional, integer, 100–250.
- `weightKg`: optional, integer, 30–200.

**Success — 200 OK** — same shape as `GET /me`.

**Errors**
- `400 VALIDATION_ERROR` — payload failed validation (422 from FastAPI by default; we map to 400 with our envelope).
- `401` — auth errors as above.

### 9.5 `GET /health`

No auth. Returns `{ "status": "ok" }`. Liveness probe.

### 9.6 Standard error envelope

```json
{
  "error": {
    "code": "INVALID_TOKEN",
    "message": "The provided token is invalid or expired.",
    "details": null
  }
}
```

Implemented as a FastAPI exception handler over a custom `AppError` hierarchy. FastAPI's default `RequestValidationError` is caught and reshaped to match.

---

## 10. Environment Variables

`.env` (git-ignored); `.env.example` committed.

| Variable | Example | Purpose |
|---|---|---|
| `APP_ENV` | `development` | Runtime mode |
| `PORT` | `8000` | Uvicorn port inside container |
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@db:5432/postgres` | Async SQLAlchemy DSN — see §11.1 |
| `SUPABASE_URL` | `https://abcdefg.supabase.co` | Supabase project URL |
| `SUPABASE_ANON_KEY` | `eyJhbGciOi...` | Public key — safe in frontend; not used by FastAPI but kept for reference |
| `SUPABASE_SERVICE_ROLE_KEY` | `eyJhbGciOi...` | **Server-only.** Required if/when FastAPI talks back to Supabase admin APIs |
| `SUPABASE_JWT_SECRET` | `super-long-jwt-secret` | HMAC secret FastAPI uses to verify tokens |
| `LOG_LEVEL` | `INFO` | Uvicorn log level |

`config.py` uses `pydantic-settings` to validate on boot — missing or malformed values fail fast with a clear error.

---

## 11. Docker Setup (zero local installs)

### 11.1 Choice: local Postgres in Docker vs. remote Supabase DB

Two options. **We use Option A for Phase 1.**

**Option A — Local Postgres in Docker (chosen)**
A `postgres:16-alpine` container on `docker compose` standing in for the Supabase DB during local dev. Alembic migrations run against it. Fast, offline-friendly, no network round-trips.

Caveat: Supabase-managed schemas (`auth.*`, `storage.*`) don't exist in plain Postgres. We handle this by creating a minimal `auth.users` table in a **dev-only bootstrap migration** so the FK in `profiles.id → auth.users.id` resolves. This bootstrap migration is gated behind `APP_ENV=development` and skipped in production (where the real Supabase `auth.users` already exists).

For auth itself, Postman still calls the **real hosted Supabase project** — there's no GoTrue running in Docker. The only thing local is the DB for our owned tables.

> Trade-off: the user ID in Supabase Auth and the user ID in local Postgres will diverge unless we also insert a matching `auth.users` row when a new user hits `/me` for the first time. The bootstrap migration + a small dev-only service method (`ensure_dev_auth_user_exists(user_id, email)`) handle this, gated on `APP_ENV=development`. It's a small amount of dev-only glue in exchange for fully offline iteration.

**Option B — Use Supabase's hosted DB directly from Docker**
Skip local Postgres; point `DATABASE_URL` at the Supabase connection string. Simpler (no bootstrap trick), but every dev needs a Supabase project and network access, and migrations run against the hosted DB.

If Option A's dev-auth glue feels uncomfortable, switch to Option B by deleting the `db` service from `docker-compose.yml` and changing `DATABASE_URL` — nothing else in the app changes.

### 11.2 `Dockerfile`

Multi-stage, Python 3.12-slim.

- **Stage `base`**: `python:3.12-slim`, install system deps (`build-essential`, `libpq-dev` if ever needed — not strictly for asyncpg).
- **Stage `deps`**: install Python packages from `pyproject.toml` / `requirements.txt` into a venv.
- **Stage `dev`**: copy venv + source, run `uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload`. This is the Compose target for local work.
- **Stage `prod`**: copy venv + source, run `uvicorn` without `--reload`, multiple workers. Not exercised in Phase 1 but scaffolded.

### 11.3 `docker-compose.yml`

**`db`** *(Option A only)*
- Image: `postgres:16-alpine`
- Env: `POSTGRES_USER=postgres`, `POSTGRES_PASSWORD=postgres`, `POSTGRES_DB=postgres`
- Volume: named `surfcoach_pgdata` on `/var/lib/postgresql/data`.
- Healthcheck: `pg_isready`.
- Port: `5432:5432`.

**`api`**
- Build: local `Dockerfile`, target `dev`.
- `depends_on: db` with `condition: service_healthy` (Option A only).
- Env file: `.env`.
- Volumes: bind-mount `./app` and `./alembic` for hot reload.
- Port: `8000:8000`.
- Command: `sh -c "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload"`.

### 11.4 Developer workflow

From a clean machine with only Docker:

```bash
# 1. Create a Supabase project (free tier).
#    Copy Project URL, anon key, service role key, and JWT secret into .env.
#    In Authentication → Providers → Email, DISABLE "Confirm email" for Phase 1.
cp .env.example .env
# fill in SUPABASE_* values

# 2. Boot everything
docker compose up --build

# 3. (First time only) Create the initial migration
docker compose exec api alembic revision --autogenerate -m "init profiles"
docker compose exec api alembic upgrade head

# 4. Hit the flow from Postman (see §12)
```

Shutdown: `docker compose down` (keeps data) or `docker compose down -v` (wipes the local DB volume).

---

## 12. Postman Flow — Definition of Done

Phase 1 is complete when these steps, run in order, all succeed:

1. `docker compose up --build` boots `db` and `api` with no errors.
2. `GET http://localhost:8000/health` → `200 { "status": "ok" }`.
3. `POST https://<PROJECT_REF>.supabase.co/auth/v1/signup` with a new email/password → `200` with `access_token` + `user.id`.
4. `GET http://localhost:8000/me` with `Authorization: Bearer <access_token>` → `200`, profile auto-created with `surfLevel: "beginner"`.
5. `PATCH http://localhost:8000/me` with `{ "surfLevel": "intermediate", "heightCm": 180, "weightKg": 75 }` → `200` with the updated profile.
6. `GET http://localhost:8000/me` again → `200` showing the updated values (persistence check).
7. `POST https://<PROJECT_REF>.supabase.co/auth/v1/token?grant_type=password` with the same credentials → `200` with a fresh `access_token`. Repeating step 4 with it still returns the updated profile (not a new one).
8. `GET /me` with no token → `401 MISSING_TOKEN`.
9. `GET /me` with a tampered token → `401 INVALID_TOKEN`.
10. `docker compose down && docker compose up` — the profile from step 5 still exists (volume persistence).

A Postman collection (`docs/surf-coach-phase1.postman_collection.json`) with all five requests pre-configured, using environment variables for `SUPABASE_URL`, `SUPABASE_ANON_KEY`, and `ACCESS_TOKEN`, ships as part of this phase.

---

## 13. Testing (minimum bar)

- **Unit tests** for `verify_supabase_jwt` — valid token, expired token, wrong audience, bad signature, missing claims.
- **Integration tests** for `/me` GET and PATCH, using a forged JWT signed with a test-only `SUPABASE_JWT_SECRET` and `httpx.AsyncClient` against the FastAPI app. Profile auto-provisioning covered.
- DB fixtures run against an ephemeral Postgres (either a `db-test` Compose service or `testcontainers-python`).
- Run inside the container: `docker compose exec api pytest`.

Coverage target isn't enforced in Phase 1; the goal is a safety net on auth verification and profile provisioning before Phase 2 builds on it.

---

## 14. Security Notes (Phase 1 baseline)

- `SUPABASE_JWT_SECRET` and `SUPABASE_SERVICE_ROLE_KEY` live only server-side; never log or echo them.
- `aud` claim must equal `"authenticated"` — reject any other.
- `exp` strictly enforced by `python-jose`.
- CORS: disabled by default; Phase 2 adds an allow-list when the Vue app arrives.
- Security headers: add `starlette.middleware.trustedhost.TrustedHostMiddleware` and strict security headers via a lightweight middleware.
- Rate limiting: out of scope; Supabase's built-in auth rate limits cover the register/login surface.

---

## 15. Handoff to Phase 2

When this phase ships, Phase 2 can assume:

- A verified `AuthUser` dependency (`core/deps.py`) and auto-provisioned `Profile` on every authenticated request.
- Stub files already in place across all four layers (`api/`, `services/`, `repositories/`, `models/`) for `sessions`, `media`, and `ai` — Phase 2 adds endpoints, service methods, and repository queries without touching `main.py` or the layer structure.
- A consistent error envelope (`core/errors.py`) and settings pattern (`core/config.py`) to extend.
- A working Docker dev loop (Supabase Auth live, local Postgres for our owned tables).

Phase 2 will populate `api/sessions.py`, `services/sessions.py`, and `repositories/sessions.py`; add `models/session.py` and `models/media.py` (both FK to `profiles.id`); and wire up the Supabase Storage bucket for media uploads.
