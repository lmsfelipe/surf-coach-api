# SPEC — Phase 3 Patch 1: Profile Enhancements, Surfboard Management & Session Refactor

**Project:** Surf Coaching Platform — MVP  
**Phase:** 3 — Patch 1  
**Scope:** Extend user profile with personal data and avatar, introduce surfboard inventory per user, and refactor session wave field and board selection  
**Depends on:** Phase 3 (training plans, `TrainingPlan`/`Workout`/`Exercise` tables, migrations up to `0007`)  
**Target environment:** Same Docker Compose stack; no new containers required

---

## 1. Phase Goal

Improve session context quality and user personalization by:

1. Enriching the `Profile` with identity fields (`name`, `gender`, `birthday`) and an avatar stored in Supabase Storage.
2. Introducing a `Surfboard` entity so users can maintain a board inventory and associate a specific board with each surf session.
3. Replacing the free-text `wave_conditions` field on `Session` with a numeric `wave_size` (feet), enabling quantitative tracking and better AI context.

Definition of done: Postman can create/update a profile (with avatar upload), manage surfboards (CRUD), create a session referencing a board and a numeric wave size — and all downstream endpoints (reviews, training plans) continue to work unchanged.

---

## 2. Out of Scope (This Patch)

- Board recommendation engine (PRD Future Considerations).
- AI prompts updated to include `wave_size` or board data — that is a follow-up prompt-engineering task.
- Frontend UI.
- Pagination on `GET /api/v1/surfboards/`.

---

## 3. Data Model Changes

### 3.1 `public.profiles` — new columns

| Column | Type | Constraints | Notes |
|---|---|---|---|
| `name` | TEXT | NULL | Display name; set during onboarding |
| `gender` | TEXT | NULL, check `('male', 'female')` | Enum enforced at DB level |
| `birthday` | DATE | NULL | Used to compute age for AI context |
| `avatar_url` | TEXT | NULL | Full public URL from Supabase Storage after upload |

No existing columns are removed. `surf_level`, `height_cm`, `weight_kg` remain unchanged.

#### SQLAlchemy model additions (`app/models/profile.py`)

```python
from sqlalchemy import CheckConstraint, Date

class Profile(Base):
    # ... existing fields unchanged ...

    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    gender: Mapped[str | None] = mapped_column(
        String,
        nullable=True,
        # DB-level constraint added via migration; model documents it only
    )
    birthday: Mapped[date | None] = mapped_column(Date, nullable=True)
    avatar_url: Mapped[str | None] = mapped_column(Text, nullable=True)
```

---

### 3.2 `public.surfboards` — new table

| Column | Type | Constraints |
|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` |
| `profile_id` | UUID | NOT NULL, FK → `public.profiles.id` ON DELETE CASCADE |
| `board_type` | TEXT | NOT NULL, check `('shortboard', 'longboard', 'funboard', 'bodyboard', 'other')` |
| `board_size` | NUMERIC(4,2) | NOT NULL — length in feet, e.g. `6.2` |
| `volume` | NUMERIC(5,1) | NULL — litres, e.g. `28.5` |
| `label` | TEXT | NULL — optional user-defined nickname, e.g. `"My daily driver"` |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default `now()` |

> `board_size` and `volume` are stored as `NUMERIC` (not `FLOAT`) to avoid floating-point rounding issues in display.

#### SQLAlchemy model (`app/models/surfboard.py`) — NEW file

```python
from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class Surfboard(Base):
    __tablename__ = "surfboards"
    __table_args__ = {"schema": "public"}

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    profile_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("public.profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    board_type: Mapped[str] = mapped_column(String, nullable=False)
    board_size: Mapped[float] = mapped_column(Numeric(4, 2), nullable=False)
    volume: Mapped[float | None] = mapped_column(Numeric(5, 1), nullable=True)
    label: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
```

---

### 3.3 `public.sessions` — column changes

| Change | Old | New |
|---|---|---|
| Remove | `wave_conditions TEXT NOT NULL` | — |
| Add | — | `wave_size NUMERIC(4,1) NOT NULL` — wave face height in feet |
| Add | — | `surfboard_id UUID NULL` — FK → `public.surfboards.id` ON DELETE SET NULL |

`board_type` (free-text, added informally in Phase 2) is **dropped** — replaced by the `surfboard_id` FK.

> `wave_size` uses feet as the unit (e.g., `3.0` = 3 ft / ~1 m). The front-end is responsible for unit conversion display. NULL is not allowed — the surfer must estimate wave size when creating a session.

#### SQLAlchemy model changes (`app/models/session.py`)

```python
from sqlalchemy import Numeric

class Session(Base):
    # Remove: wave_conditions, board_type
    # Add:
    wave_size: Mapped[float] = mapped_column(Numeric(4, 1), nullable=False)
    surfboard_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("public.surfboards.id", ondelete="SET NULL"),
        nullable=True,
    )
```

---

### 3.4 Alembic Migrations

Chained after `0007_create_exercises.py`.

#### `0008_extend_profiles_and_add_surfboards.py`

```python
def upgrade():
    op.execute("""
        -- Profile enhancements
        ALTER TABLE public.profiles
            ADD COLUMN name TEXT,
            ADD COLUMN gender TEXT CHECK (gender IN ('male', 'female')),
            ADD COLUMN birthday DATE,
            ADD COLUMN avatar_url TEXT;

        -- Surfboards table
        CREATE TABLE public.surfboards (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            profile_id UUID NOT NULL
                REFERENCES public.profiles(id) ON DELETE CASCADE,
            board_type TEXT NOT NULL
                CHECK (board_type IN ('shortboard', 'longboard', 'funboard', 'bodyboard', 'other')),
            board_size NUMERIC(4,2) NOT NULL CHECK (board_size > 0),
            volume NUMERIC(5,1) CHECK (volume > 0),
            label TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX idx_surfboards_profile_id ON public.surfboards(profile_id);

        -- RLS
        ALTER TABLE public.surfboards ENABLE ROW LEVEL SECURITY;
        CREATE POLICY "surfboards_select_own" ON public.surfboards
            FOR SELECT USING (auth.uid() = profile_id);
        CREATE POLICY "surfboards_insert_own" ON public.surfboards
            FOR INSERT WITH CHECK (auth.uid() = profile_id);
        CREATE POLICY "surfboards_update_own" ON public.surfboards
            FOR UPDATE USING (auth.uid() = profile_id);
        CREATE POLICY "surfboards_delete_own" ON public.surfboards
            FOR DELETE USING (auth.uid() = profile_id);
    """)

def downgrade():
    op.execute("""
        DROP TABLE IF EXISTS public.surfboards;
        ALTER TABLE public.profiles
            DROP COLUMN IF EXISTS avatar_url,
            DROP COLUMN IF EXISTS birthday,
            DROP COLUMN IF EXISTS gender,
            DROP COLUMN IF EXISTS name;
    """)
```

#### `0009_refactor_sessions_wave_and_board.py`

```python
def upgrade():
    op.execute("""
        ALTER TABLE public.sessions
            ADD COLUMN wave_size NUMERIC(4,1),
            ADD COLUMN surfboard_id UUID
                REFERENCES public.surfboards(id) ON DELETE SET NULL;

        -- Back-fill: default existing rows to 3.0 ft (small wave placeholder)
        UPDATE public.sessions SET wave_size = 3.0 WHERE wave_size IS NULL;

        -- Now enforce NOT NULL
        ALTER TABLE public.sessions
            ALTER COLUMN wave_size SET NOT NULL,
            DROP COLUMN wave_conditions,
            DROP COLUMN board_type;

        CREATE INDEX idx_sessions_surfboard_id ON public.sessions(surfboard_id);
    """)

def downgrade():
    op.execute("""
        ALTER TABLE public.sessions
            ADD COLUMN wave_conditions TEXT NOT NULL DEFAULT 'unknown',
            ADD COLUMN board_type TEXT,
            DROP COLUMN IF EXISTS surfboard_id,
            DROP COLUMN IF EXISTS wave_size;
    """)
```

---

### 3.5 Row Level Security

Profile additions do not require new RLS policies — existing `profiles` policies already cover all columns on the row.

For `surfboards`, see policies in migration `0008` above.

Session `surfboard_id` column is covered by the existing `sessions` RLS policies (row-level ownership via `profile_id`).

---

## 4. Supabase Storage — Avatar

### Bucket

- Bucket name: `avatars`
- Access: **private** (signed URLs or public with restricted policy)
- Recommended path pattern: `{profile_id}/avatar.{ext}`
- Max file size: 5 MB. Accepted types: `image/jpeg`, `image/png`, `image/webp`.

### Upload flow

The backend does **not** proxy the file upload. The client uploads directly to Supabase Storage using the Supabase JS/mobile SDK with the user's JWT. After a successful upload, the client calls `PATCH /api/v1/me` with `{ "avatarUrl": "<public-or-signed-url>" }` to persist the URL on the profile.

> This keeps the FastAPI service stateless with respect to file storage — consistent with the existing media upload pattern.

---

## 5. New Service & Repository

### 5.1 File additions within existing structure

```
app/
├── models/
│   └── surfboard.py          ← NEW
├── schemas/
│   └── surfboard.py          ← NEW
├── api/
│   └── surfboards.py         ← NEW  (mounted as /api/v1/surfboards)
├── services/
│   └── surfboards.py         ← NEW
└── repositories/
    └── surfboards.py         ← NEW
```

`main.py` — add `surfboards.router` mount (one line change).

---

### 5.2 `SurfboardRepository` (`app/repositories/surfboards.py`)

```python
class SurfboardRepository:
    def get_all_by_profile(self, profile_id: UUID) -> list[Surfboard]: ...
    def get_by_id(self, surfboard_id: UUID) -> Surfboard | None: ...
    def create(self, profile_id: UUID, data: SurfboardCreateInternal) -> Surfboard: ...
    def update(self, surfboard: Surfboard, data: SurfboardUpdateInternal) -> Surfboard: ...
    def delete(self, surfboard: Surfboard) -> None: ...
```

---

### 5.3 `SurfboardService` (`app/services/surfboards.py`)

```
list_boards(auth_user) → SurfboardRepository.get_all_by_profile(auth_user.id)

get_board(surfboard_id, auth_user)
    → load board; 404 if not found; 403 if board.profile_id != auth_user.id
    → return board

create_board(data, auth_user)
    → SurfboardRepository.create(auth_user.id, data)

update_board(surfboard_id, data, auth_user)
    → load board; 404 / 403 checks
    → SurfboardRepository.update(board, data)

delete_board(surfboard_id, auth_user)
    → load board; 404 / 403 checks
    → SurfboardRepository.delete(board)
```

---

### 5.4 Profile service changes (`app/services/profile.py`)

`update_profile` already handles `PATCH /me`. Extend `ProfileUpdateRequest` schema with the four new optional fields:

```python
class ProfileUpdateRequest(BaseModel):
    surf_level: str | None = None
    height_cm: int | None = None
    weight_kg: int | None = None
    name: str | None = None
    gender: Literal["male", "female"] | None = None
    birthday: date | None = None
    avatar_url: str | None = None
```

No service logic changes needed — the repository `update` call already patches only provided fields.

---

### 5.5 Session service changes (`app/services/sessions.py`)

`create_session` must accept `wave_size` (required) and `surfboard_id` (optional) instead of `wave_conditions` and `board_type`.

When `surfboard_id` is provided:
- Verify the board exists and belongs to `auth_user.id` → `403 FORBIDDEN` if not.
- Persist `surfboard_id` on the session row.

`SessionCreateRequest` schema changes:

```python
class SessionCreateRequest(BaseModel):
    session_date: date
    location: str
    wave_size: float          # required; feet; must be > 0
    surfboard_id: UUID | None = None
    notes: str | None = None
```

> `wave_conditions` and `board_type` are removed from this schema entirely.

---

## 6. API Contracts

Base URL: `http://localhost:8000`  
All routes require `Authorization: Bearer <access_token>` unless stated otherwise.

---

### 6.1 `GET /api/v1/me` — Get profile (updated response)

**200 OK** — adds new fields to existing response:

```json
{
  "id": "a7b2c3d4-...",
  "surfLevel": "intermediate",
  "heightCm": 178,
  "weightKg": 72,
  "name": "João Silva",
  "gender": "male",
  "birthday": "1995-06-15",
  "avatarUrl": "https://xxx.supabase.co/storage/v1/object/public/avatars/a7b2c3d4-.../avatar.jpg",
  "createdAt": "2026-01-10T08:00:00.000Z",
  "updatedAt": "2026-05-01T12:00:00.000Z"
}
```

---

### 6.2 `PATCH /api/v1/me` — Update profile (updated request)

All fields remain optional. New accepted fields: `name`, `gender`, `birthday`, `avatarUrl`.

**Request body example:**
```json
{
  "name": "João Silva",
  "gender": "male",
  "birthday": "1995-06-15",
  "avatarUrl": "https://xxx.supabase.co/storage/v1/object/public/avatars/..."
}
```

**200 OK** — same shape as `GET /me`.

---

### 6.3 `GET /api/v1/surfboards/` — List user's surfboards

**200 OK**
```json
[
  {
    "id": "b1c2d3e4-...",
    "profileId": "a7b2c3d4-...",
    "boardType": "shortboard",
    "boardSize": 6.2,
    "volume": 28.5,
    "label": "My daily driver",
    "createdAt": "2026-03-01T10:00:00.000Z",
    "updatedAt": "2026-03-01T10:00:00.000Z"
  }
]
```

---

### 6.4 `POST /api/v1/surfboards/` — Create surfboard

**Request body:**
```json
{
  "boardType": "shortboard",
  "boardSize": 6.2,
  "volume": 28.5,
  "label": "My daily driver"
}
```

`boardType` values: `"shortboard"`, `"longboard"`, `"funboard"`, `"bodyboard"`, `"other"`.

**201 Created** — same shape as list item above.

**Errors:** `400 VALIDATION_ERROR` (invalid enum or size ≤ 0), `401`, `403`.

---

### 6.5 `GET /api/v1/surfboards/{surfboard_id}` — Get surfboard

**200 OK** — same shape as list item.

**Errors:** `401`, `403 FORBIDDEN`, `404 NOT_FOUND`.

---

### 6.6 `PATCH /api/v1/surfboards/{surfboard_id}` — Update surfboard

All fields optional in request body. Same shape as create.

**200 OK** — updated surfboard.

**Errors:** `401`, `403 FORBIDDEN`, `404 NOT_FOUND`.

---

### 6.7 `DELETE /api/v1/surfboards/{surfboard_id}` — Delete surfboard

**204 No Content.**

> Sessions that referenced this board will have `surfboard_id` set to NULL (ON DELETE SET NULL).

**Errors:** `401`, `403 FORBIDDEN`, `404 NOT_FOUND`.

---

### 6.8 `POST /api/v1/sessions/` — Create session (updated)

**Request body:**
```json
{
  "sessionDate": "2026-05-01",
  "location": "Praia do Rosa",
  "waveSize": 4.5,
  "surfboardId": "b1c2d3e4-...",
  "notes": "Fun session, offshore wind"
}
```

`waveConditions` and `boardType` are **removed**. `waveSize` is required. `surfboardId` is optional.

**201 Created**
```json
{
  "id": "c2d3e4f5-...",
  "profileId": "a7b2c3d4-...",
  "sessionDate": "2026-05-01",
  "location": "Praia do Rosa",
  "waveSize": 4.5,
  "surfboardId": "b1c2d3e4-...",
  "notes": "Fun session, offshore wind",
  "createdAt": "2026-05-01T14:00:00.000Z",
  "updatedAt": "2026-05-01T14:00:00.000Z"
}
```

**Errors:** `400 VALIDATION_ERROR` (waveSize ≤ 0), `401`, `403 FORBIDDEN` (surfboardId belongs to another user), `404 NOT_FOUND` (surfboardId not found).

---

### 6.9 `GET /api/v1/sessions/{session_id}` — Get session (updated response)

Same as create response shape — `waveSize` and `surfboardId` replace `waveConditions` and `boardType`.

---

## 7. Pydantic Schema Conventions

Same `_CamelModel` base (`alias_generator=to_camel`, `populate_by_name=True`, `from_attributes=True`).

**`schemas/surfboard.py`** exports:
- `SurfboardCreateRequest` — `board_type`, `board_size`, `volume?`, `label?`
- `SurfboardUpdateRequest` — all optional
- `SurfboardResponse` — full board representation

**`schemas/profile.py`** — extend existing `ProfileResponse` and `ProfileUpdateRequest` with `name`, `gender`, `birthday`, `avatar_url`.

**`schemas/session.py`** — replace `wave_conditions: str` with `wave_size: float` and add `surfboard_id: UUID | None` in both request and response schemas; remove `board_type`.

---

## 8. Error Handling

### New `AppError` subclasses (added to `core/errors.py`)

| Class | `code` | `status_code` | Default message |
|---|---|---|---|
| `SurfboardNotFoundError` | `SURFBOARD_NOT_FOUND` | 404 | "Surfboard not found." |
| `SurfboardForbiddenError` | `SURFBOARD_FORBIDDEN` | 403 | "This surfboard does not belong to you." |

---

## 9. Project Structure (Patch 1 Final State)

```
surf-coach-api/
├── alembic/
│   └── versions/
│       ├── 0001–0007           # Phase 1–3 — untouched
│       ├── 0008_extend_profiles_and_add_surfboards.py  ← NEW
│       └── 0009_refactor_sessions_wave_and_board.py    ← NEW
└── app/
    ├── main.py                 ← add surfboards.router mount
    ├── api/
    │   └── surfboards.py       ← NEW (5 routes)
    ├── services/
    │   ├── surfboards.py       ← NEW
    │   ├── profile.py          ← extend update schema
    │   └── sessions.py         ← wave_size + surfboard_id
    ├── repositories/
    │   └── surfboards.py       ← NEW
    ├── models/
    │   ├── profile.py          ← add name/gender/birthday/avatar_url
    │   ├── session.py          ← swap wave_conditions→wave_size, add surfboard_id
    │   └── surfboard.py        ← NEW
    ├── schemas/
    │   ├── profile.py          ← extend request/response
    │   ├── session.py          ← swap wave_conditions→wave_size
    │   └── surfboard.py        ← NEW
    └── core/
        └── errors.py           ← SurfboardNotFoundError, SurfboardForbiddenError
```

---

## 10. Build Order

| Step | Task | Depends on |
|---|---|---|
| 1 | Write Alembic migration `0008` (profile columns + surfboards table); run `alembic upgrade head` | — |
| 2 | Write Alembic migration `0009` (session refactor); run `alembic upgrade head` | Step 1 |
| 3 | Implement `Surfboard` ORM model (`models/surfboard.py`) | Step 1 |
| 4 | Update `Profile` ORM model with new columns | Step 1 |
| 5 | Update `Session` ORM model (`wave_size`, `surfboard_id`, remove `wave_conditions`/`board_type`) | Step 2 |
| 6 | Add `SurfboardNotFoundError`, `SurfboardForbiddenError` to `core/errors.py` | — |
| 7 | Implement `schemas/surfboard.py` | Step 3 |
| 8 | Extend `schemas/profile.py` and `schemas/session.py` | Steps 4–5 |
| 9 | Implement `SurfboardRepository` | Step 3 |
| 10 | Implement `SurfboardService` | Steps 6, 9 |
| 11 | Implement `api/surfboards.py` (5 routes) | Step 10 |
| 12 | Extend `ProfileService`/`ProfileRepository` with new fields | Steps 4, 8 |
| 13 | Extend `SessionService`/`SessionRepository` with `wave_size` and `surfboard_id` | Steps 5, 8, 10 |
| 14 | Add `surfboards.router` mount in `main.py` | Step 11 |
| 15 | Integration tests | Step 14 |

---

## 11. Postman Flow — Definition of Done

Patch 1 is complete when these steps succeed against `docker compose up`:

1. `GET /health` → `200 { "status": "ok" }`.
2. Supabase login → `access_token`.
3. `PATCH /api/v1/me` with `{ "name": "Test User", "gender": "male", "birthday": "1995-06-15" }` → `200` with updated fields.
4. `POST /api/v1/surfboards/` with `{ "boardType": "shortboard", "boardSize": 6.2, "volume": 28.5 }` → `201`, save `surfboard_id`.
5. `GET /api/v1/surfboards/` → `200` with array containing the created board.
6. `GET /api/v1/surfboards/{surfboard_id}` → `200`.
7. `PATCH /api/v1/surfboards/{surfboard_id}` with `{ "label": "Updated label" }` → `200`.
8. `POST /api/v1/sessions/` with `{ "sessionDate": "...", "location": "...", "waveSize": 4.5, "surfboardId": "<id>" }` → `201` with `waveSize` and `surfboardId` in response.
9. `POST /api/v1/sessions/` with `waveConditions` field → `422 VALIDATION_ERROR` (field no longer accepted).
10. `DELETE /api/v1/surfboards/{surfboard_id}` → `204`. Verify session `surfboard_id` is now null via `GET /api/v1/sessions/{session_id}`.
11. All Phase 3 flows (generate review, generate training plan) still pass.

---

## 12. Testing (Minimum Bar)

### Unit tests

- **`SurfboardService.create_board`** — assert board is persisted with correct `profile_id`.
- **`SurfboardService.get_board`** — 404 on missing board; 403 on wrong owner.
- **`SessionService.create_session`** — `surfboard_id` ownership check: 403 when board belongs to other user.
- **`ProfileUpdateRequest`** — valid `gender` values pass; invalid value raises `ValidationError`; `birthday` in future date is accepted (no age restriction at MVP).

### Integration tests

- **`POST /api/v1/surfboards/`** — 201 with all fields persisted.
- **`GET /api/v1/surfboards/`** — returns only boards for authenticated user.
- **`PATCH /api/v1/surfboards/{id}`** — partial update; untouched fields unchanged.
- **`DELETE /api/v1/surfboards/{id}`** — 204; subsequent GET returns 404; related session `surfboard_id` is NULL.
- **`POST /api/v1/sessions/` with `waveSize`** — 201 with numeric wave size; `waveConditions` rejected.
- **`POST /api/v1/sessions/` with foreign surfboard** — 403.
- **`PATCH /api/v1/me` with avatar URL** — persists `avatarUrl` in response.

---

## 13. Security Notes

- **Avatar URL** — stored as plain TEXT; no server-side fetch or redirect. Front-end is responsible for rendering only trusted Supabase Storage URLs. A URL allowlist or domain validation can be added in a future patch.
- **`gender` / `birthday`** — PII fields. Covered by existing Supabase RLS on `profiles` (`auth.uid() = id`). No additional exposure.
- **Surfboard ownership** — enforced at the service layer (`board.profile_id == auth_user.id`) in addition to RLS. Double-checking at the application layer prevents accidental bypass if RLS is misconfigured.
- **`wave_size`** — numeric, no injection risk. Validated at schema level (`> 0`).

---

## 14. Handoff to Next Phase

When Patch 1 ships, the next phase can assume:

- `Profile` has `name`, `gender`, `birthday`, `avatar_url` — sufficient for richer AI context and onboarding UX.
- `Surfboard` table is live with full CRUD and ownership enforcement.
- `Session.wave_size` is a numeric field — enables wave-size trend analysis and can be passed to the Gemini prompt for better training recommendations.
- `Session.surfboard_id` links sessions to specific equipment — enables board-performance correlation in future AI analysis.
- AI prompts (review generation, training plan) can be updated in isolation to include `wave_size` and board data without any model/migration changes.
