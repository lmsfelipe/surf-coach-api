# SPEC — Phase 2: Sessions, Media Upload & AI Review Pipeline

**Project:** Surf Coaching Platform — MVP  
**Phase:** 2 of N  
**Scope:** Surf session CRUD, Supabase Storage media upload, Gemini Vision AI review generation  
**Depends on:** Phase 1 (auth, profile, Docker dev loop, error envelope, stub files)  
**Target environment:** Same Docker Compose stack as Phase 1; no new containers required

---

## 1. Phase Goal

Deliver a fully functional AI coaching backend that, by the end of this phase, allows a developer to:

1. **Create a surf session** with date, location, wave conditions, and optional notes.
2. **Upload surf media** (image or short video) linked to that session — stored in Supabase Storage.
3. **Trigger an AI review** for the session — the pipeline downloads media, extracts frames from videos (OpenCV), builds a structured prompt, calls Gemini Vision, parses the response, and persists the review.
4. **Retrieve the review** — by review ID or by session.

The definition of done: Postman runs `create session → upload media → generate review → retrieve review` and all steps succeed with data persisted in Postgres and media stored in Supabase Storage.

This phase activates the two stub domains already scaffolded in Phase 1 — `sessions` and `media` — and wires `ai` partially (review generation lives in `services/ai.py`; the `api/ai.py` stub remains empty until Phase 3).

---

## 2. Assumptions from Phase 1

Phase 2 can rely on the following, already shipped:

- `get_current_user` FastAPI dependency (`core/deps.py`) — verifies the Supabase JWT and returns `AuthUser(id: UUID, email: str)`.
- Auto-provisioned `Profile` row on first authenticated request — every authenticated user has a `profiles` record.
- Standard error envelope (`core/errors.py`) with `AppError` hierarchy and exception handlers.
- `Settings` via `pydantic-settings` (`core/config.py`) — validates env vars on boot.
- Alembic migration chain: `0001_dev_auth_users_shim` → `0002_init_profiles`.
- Stub files in all layers for `sessions`, `media`, and `ai` — no `main.py` changes needed to add new routes (routers are already mounted).
- Docker Compose stack with `db` (postgres:16-alpine) and `api` (FastAPI + Alembic) services.

---

## 3. Architecture Principles

Same Clean Architecture layers from Phase 1. Phase 2 fills in the stubs — it does **not** add new folders or restructure.

### Layer responsibilities (unchanged)

- **`api/`** — HTTP only: parse request, call service, return response.
- **`services/`** — Business logic and orchestration. Receives repositories via `Depends()`.
- **`repositories/`** — All SQLAlchemy queries. Returns ORM instances or primitives.
- **`models/`** — SQLAlchemy ORM models.
- **`schemas/`** — Pydantic API shapes.
- **`core/`** — Shared infrastructure. Phase 2 adds `storage.py` and `frame_extractor.py` here.

### Phase 2 additions within this structure

```
app/
├── core/
│   ├── storage.py          ← NEW: Supabase Storage client wrapper
│   └── frame_extractor.py  ← NEW: OpenCV-based video frame extractor
├── models/
│   ├── session.py          ← NEW
│   ├── media.py            ← NEW
│   └── review.py           ← NEW
├── schemas/
│   ├── sessions.py         ← NEW
│   ├── media.py            ← NEW
│   └── reviews.py          ← NEW
├── api/
│   ├── sessions.py         ← Phase 1 stub → fully implemented
│   ├── media.py            ← Phase 1 stub → fully implemented
│   └── reviews.py          ← NEW (not stubbed in Phase 1)
├── services/
│   ├── sessions.py         ← Phase 1 stub → fully implemented
│   ├── media.py            ← Phase 1 stub → fully implemented
│   └── ai.py               ← Phase 1 stub → ReviewService + GeminiService
└── repositories/
    ├── sessions.py         ← Phase 1 stub → fully implemented
    ├── media.py            ← Phase 1 stub → fully implemented
    └── ai.py               ← Phase 1 stub → ReviewRepository
```

`app/api/ai.py` remains an empty stub — Phase 3 will add the training-suggestion endpoints there.

---

## 4. Out of Scope (Phase 2)

Must **not** be implemented in this phase:

- Training suggestion generation (Phase 3 — `api/ai.py` stays stubbed).
- Progress dashboard aggregation queries (Phase 3).
- Background task queue (Celery, ARQ) — review generation is **synchronous** in Phase 2.
- Rate limiting on AI pipeline endpoints.
- Video playback with AI-annotated timestamps.
- Human coach comparison or override mode.
- Board recommendation logic.
- Marketplace features (coaches, photographers).
- Vue.js frontend (separate repo).
- Production deploy to Railway.

---

## 5. Tech Stack Additions (Phase 2)

All Phase 1 dependencies remain unchanged.

### New Python dependencies

| Package | Version | Purpose |
|---|---|---|
| `google-generativeai` | `>=0.7` | Official Gemini Python SDK |
| `opencv-python-headless` | `>=4.10` | Frame extraction from video — headless avoids GUI deps in Docker |
| `supabase` | `>=2.0` | Supabase Python client for Storage uploads |
| `python-magic` | `>=0.4` | MIME type detection from file bytes (not filename) — prevents extension spoofing |
| `aiofiles` | `>=23.0` | Async file I/O for temporary file handling |
| `python-multipart` | `>=0.0.9` | Required by FastAPI for `UploadFile` / multipart form parsing |

> `opencv-python-headless` is mandatory in Docker environments — `opencv-python` pulls in GUI libraries that bloat the image and fail on import in headless containers.

Add these to `pyproject.toml` under `[project]` → `dependencies`.

---

## 6. Project Structure (Phase 2 Final State)

```
surf-coach-api/
├── docker-compose.yml
├── Dockerfile
├── .dockerignore
├── .env.example                            # updated with Phase 2 vars
├── pyproject.toml                          # updated with new deps
├── alembic.ini
├── alembic/
│   ├── env.py
│   └── versions/
│       ├── 0001_dev_auth_users_shim.py     # Phase 1 — untouched
│       ├── 0002_init_profiles.py           # Phase 1 — untouched
│       ├── 0003_create_sessions.py         ← NEW
│       ├── 0004_create_media.py            ← NEW
│       └── 0005_create_reviews.py          ← NEW
├── tests/
│   ├── conftest.py                         # updated with session/media fixtures
│   ├── test_jwt.py                         # Phase 1 — untouched
│   ├── test_me_endpoints.py                # Phase 1 — untouched
│   ├── test_sessions.py                    ← NEW
│   ├── test_media_upload.py                ← NEW
│   ├── test_reviews.py                     ← NEW
│   └── fixtures/
│       ├── surf_sample.jpg                 ← NEW: small JPEG for upload tests
│       └── surf_sample.mp4                 ← NEW: short synthetic MP4 for frame extraction tests
├── docs/
│   └── surf-coach-phase1.postman_collection.json
└── app/
    ├── main.py                             # mounts reviews.router (added here)
    ├── api/
    │   ├── auth.py                         # Phase 1 — untouched
    │   ├── health.py                       # Phase 1 — untouched
    │   ├── sessions.py                     ← stub → full CRUD
    │   ├── media.py                        ← stub → upload/get/delete
    │   ├── reviews.py                      ← NEW (not stubbed in Phase 1)
    │   └── ai.py                           # Phase 1 stub — still empty (Phase 3)
    ├── services/
    │   ├── auth.py                         # Phase 1 — untouched
    │   ├── sessions.py                     ← stub → SessionsService
    │   ├── media.py                        ← stub → MediaService
    │   ├── ai.py                           ← stub → ReviewService + GeminiService
    │   └── (sessions_stub_was_here)
    ├── repositories/
    │   ├── auth.py                         # Phase 1 — untouched
    │   ├── sessions.py                     ← stub → SessionsRepository
    │   ├── media.py                        ← stub → MediaRepository
    │   └── ai.py                           ← stub → ReviewRepository
    ├── models/
    │   ├── profile.py                      # Phase 1 — untouched
    │   ├── session.py                      ← NEW
    │   ├── media.py                        ← NEW
    │   └── review.py                       ← NEW
    ├── schemas/
    │   ├── auth.py                         # Phase 1 — untouched
    │   ├── sessions.py                     ← NEW
    │   ├── media.py                        ← NEW
    │   └── reviews.py                      ← NEW
    └── core/
        ├── config.py                       # updated with Phase 2 env vars
        ├── db.py                           # Phase 1 — untouched
        ├── deps.py                         # Phase 1 — untouched
        ├── errors.py                       # updated with new AppError subclasses
        ├── storage.py                      ← NEW: Supabase Storage client
        ├── frame_extractor.py              ← NEW: OpenCV utility
        └── security/
            └── jwt.py                      # Phase 1 — untouched
```

### `main.py` — single change

Add `from app.api import reviews` and mount `reviews.router`:

```python
# app/main.py (addition only)
from app.api import ai, auth, health, media, reviews, sessions

# inside create_app():
app.include_router(reviews.router)
```

---

## 7. Data Models (Phase 2)

### 7.1 `public.sessions`

| Column | Type | Constraints |
|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` |
| `profile_id` | UUID | NOT NULL, FK → `public.profiles.id` ON DELETE CASCADE |
| `session_date` | DATE | NOT NULL |
| `location` | TEXT | NOT NULL |
| `wave_conditions` | TEXT | NOT NULL |
| `board_type` | TEXT | NULL |
| `notes` | TEXT | NULL |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` |
| `updated_at` | TIMESTAMPTZ | NOT NULL, default `now()`, touched on update |

> Column named `session_date` (not `date`) to avoid SQL reserved word conflicts.

### SQLAlchemy model sketch

```python
class Session(Base):
    __tablename__ = "sessions"
    __table_args__ = {"schema": "public"}

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True,
                                     server_default=text("gen_random_uuid()"))
    profile_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True),
                                             ForeignKey("public.profiles.id", ondelete="CASCADE"),
                                             nullable=False)
    session_date: Mapped[date] = mapped_column(Date, nullable=False)
    location: Mapped[str] = mapped_column(String, nullable=False)
    wave_conditions: Mapped[str] = mapped_column(String, nullable=False)
    board_type: Mapped[str | None] = mapped_column(String, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now(),
                                                  onupdate=func.now())
```

---

### 7.2 `public.media`

| Column | Type | Constraints |
|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` |
| `session_id` | UUID | NOT NULL, FK → `public.sessions.id` ON DELETE CASCADE |
| `media_type` | TEXT | NOT NULL, check `('image', 'video')` |
| `storage_url` | TEXT | NOT NULL |
| `file_name` | TEXT | NOT NULL |
| `file_size_bytes` | BIGINT | NULL |
| `duration_seconds` | NUMERIC(6,2) | NULL — video only |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` |

### SQLAlchemy model sketch

```python
class Media(Base):
    __tablename__ = "media"
    __table_args__ = {"schema": "public"}

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True,
                                     server_default=text("gen_random_uuid()"))
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True),
                                              ForeignKey("public.sessions.id", ondelete="CASCADE"),
                                              nullable=False)
    media_type: Mapped[str] = mapped_column(String, nullable=False)
    storage_url: Mapped[str] = mapped_column(Text, nullable=False)
    file_name: Mapped[str] = mapped_column(String, nullable=False)
    file_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(6, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())
```

---

### 7.3 `public.reviews`

| Column | Type | Constraints |
|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` |
| `session_id` | UUID | NOT NULL, FK → `public.sessions.id` ON DELETE CASCADE, **UNIQUE** |
| `profile_id` | UUID | NOT NULL, FK → `public.profiles.id` ON DELETE CASCADE |
| `narrative` | TEXT | NOT NULL |
| `improvement_tips` | TEXT[] | NOT NULL — exactly 3 elements |
| `score_flow` | NUMERIC(4,1) | NOT NULL, check 0–10 |
| `score_drop` | NUMERIC(4,1) | NOT NULL, check 0–10 |
| `score_balance` | NUMERIC(4,1) | NOT NULL, check 0–10 |
| `score_wave_selection` | NUMERIC(4,1) | NOT NULL, check 0–10 |
| `score_maneuvers` | NUMERIC(4,1) | NOT NULL, check 0–10 |
| `score_arms` | NUMERIC(4,1) | NOT NULL, check 0–10 |
| `overall_score` | NUMERIC(4,1) | NOT NULL — arithmetic mean of 6 scores, rounded to 1dp |
| `ai_model_version` | TEXT | NULL — e.g. `"gemini-1.5-pro"` |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` |

> The `UNIQUE` constraint on `session_id` enforces the one-review-per-session product rule. A new session must be created if the surfer wants a re-analysis.

> `improvement_tips` is stored as a native Postgres `TEXT[]` array. SQLAlchemy maps this to `ARRAY(Text)`.

### SQLAlchemy model sketch

```python
class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_reviews_session"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True,
                                     server_default=text("gen_random_uuid()"))
    session_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True),
                                              ForeignKey("public.sessions.id", ondelete="CASCADE"),
                                              nullable=False, unique=True)
    profile_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True),
                                              ForeignKey("public.profiles.id", ondelete="CASCADE"),
                                              nullable=False)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    improvement_tips: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    score_flow: Mapped[float] = mapped_column(Numeric(4, 1), nullable=False)
    score_drop: Mapped[float] = mapped_column(Numeric(4, 1), nullable=False)
    score_balance: Mapped[float] = mapped_column(Numeric(4, 1), nullable=False)
    score_wave_selection: Mapped[float] = mapped_column(Numeric(4, 1), nullable=False)
    score_maneuvers: Mapped[float] = mapped_column(Numeric(4, 1), nullable=False)
    score_arms: Mapped[float] = mapped_column(Numeric(4, 1), nullable=False)
    overall_score: Mapped[float] = mapped_column(Numeric(4, 1), nullable=False)
    ai_model_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())
```

---

### 7.4 Alembic Migrations

Three new migrations, chained after `0002_init_profiles`:

**`0003_create_sessions.py`**
```python
def upgrade():
    op.execute("""
        CREATE TABLE public.sessions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            profile_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
            session_date DATE NOT NULL,
            location TEXT NOT NULL,
            wave_conditions TEXT NOT NULL,
            board_type TEXT,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX idx_sessions_profile_id ON public.sessions(profile_id);
    """)
```

**`0004_create_media.py`**
```python
def upgrade():
    op.execute("""
        CREATE TABLE public.media (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id UUID NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
            media_type TEXT NOT NULL CHECK (media_type IN ('image', 'video')),
            storage_url TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_size_bytes BIGINT,
            duration_seconds NUMERIC(6,2),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX idx_media_session_id ON public.media(session_id);
    """)
```

**`0005_create_reviews.py`**
```python
def upgrade():
    op.execute("""
        CREATE TABLE public.reviews (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id UUID NOT NULL UNIQUE REFERENCES public.sessions(id) ON DELETE CASCADE,
            profile_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
            narrative TEXT NOT NULL,
            improvement_tips TEXT[] NOT NULL,
            score_flow NUMERIC(4,1) NOT NULL CHECK (score_flow BETWEEN 0 AND 10),
            score_drop NUMERIC(4,1) NOT NULL CHECK (score_drop BETWEEN 0 AND 10),
            score_balance NUMERIC(4,1) NOT NULL CHECK (score_balance BETWEEN 0 AND 10),
            score_wave_selection NUMERIC(4,1) NOT NULL CHECK (score_wave_selection BETWEEN 0 AND 10),
            score_maneuvers NUMERIC(4,1) NOT NULL CHECK (score_maneuvers BETWEEN 0 AND 10),
            score_arms NUMERIC(4,1) NOT NULL CHECK (score_arms BETWEEN 0 AND 10),
            overall_score NUMERIC(4,1) NOT NULL CHECK (overall_score BETWEEN 0 AND 10),
            ai_model_version TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX idx_reviews_profile_id ON public.reviews(profile_id);
    """)
```

---

### 7.5 Row Level Security

Enable RLS on all three new tables. FastAPI uses the service-role key (bypasses RLS); policies protect against direct Supabase client access.

```sql
-- sessions
ALTER TABLE public.sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY "sessions_select_own" ON public.sessions FOR SELECT USING (auth.uid() = profile_id);
CREATE POLICY "sessions_insert_own" ON public.sessions FOR INSERT WITH CHECK (auth.uid() = profile_id);
CREATE POLICY "sessions_update_own" ON public.sessions FOR UPDATE USING (auth.uid() = profile_id);
CREATE POLICY "sessions_delete_own" ON public.sessions FOR DELETE USING (auth.uid() = profile_id);

-- media
ALTER TABLE public.media ENABLE ROW LEVEL SECURITY;
CREATE POLICY "media_select_own" ON public.media FOR SELECT USING (
    EXISTS (SELECT 1 FROM public.sessions s WHERE s.id = media.session_id AND auth.uid() = s.profile_id)
);
CREATE POLICY "media_insert_own" ON public.media FOR INSERT WITH CHECK (
    EXISTS (SELECT 1 FROM public.sessions s WHERE s.id = media.session_id AND auth.uid() = s.profile_id)
);

-- reviews
ALTER TABLE public.reviews ENABLE ROW LEVEL SECURITY;
CREATE POLICY "reviews_select_own" ON public.reviews FOR SELECT USING (auth.uid() = profile_id);
```

Include these `ALTER TABLE` and `CREATE POLICY` statements at the end of each respective migration's `upgrade()` function.

---

## 8. New Services & Utilities

### 8.1 `FrameExtractor` — `app/core/frame_extractor.py`

Stateless utility class. No database access, no HTTP calls.

```python
class FrameExtractor:
    def extract(self, video_bytes: bytes, frame_count: int = 6) -> list[bytes]:
        """
        Accepts raw video bytes, writes to a temp file, opens with cv2.VideoCapture,
        samples `frame_count` evenly-spaced frames, encodes each as JPEG bytes,
        cleans up the temp file, and returns the ordered list.
        """
```

- Uses `tempfile.NamedTemporaryFile` to write video bytes to disk (OpenCV needs a file path).
- Frame sampling: `frame_indices = [int(i * total_frames / frame_count) for i in range(frame_count)]`
- Each frame is encoded with `cv2.imencode('.jpg', frame)` → returns JPEG bytes.
- Temp file is deleted in a `finally` block.
- If OpenCV fails to open the file, raises `InvalidMediaError`.

---

### 8.2 `GeminiService` — `app/services/ai.py`

Stateless service. No database access. Accepts prepared images and surfer context, returns a validated `ReviewOutput`.

```python
class SurferContext(BaseModel):
    skill_level: str
    location: str
    wave_conditions: str | None
    board_type: str | None
    notes: str | None

class ScoreRubric(BaseModel):
    flow: float
    drop: float
    balance: float
    wave_selection: float
    maneuvers: float
    arms: float

class ReviewOutput(BaseModel):
    narrative: str
    improvement_tips: list[str]  # exactly 3
    scores: ScoreRubric

class GeminiService:
    def analyze_surf_media(
        self,
        images: list[bytes],
        context: SurferContext,
    ) -> ReviewOutput: ...
```

**Prompt structure** (three parts assembled in order):

1. **System persona** — "You are an expert surf coach with 20 years of experience analyzing surf footage. Provide structured, actionable feedback calibrated to the surfer's skill level."
2. **Surfer context** — JSON block containing `skill_level`, `location`, `wave_conditions`, `board_type`, `notes`.
3. **Output schema instruction** — Strict JSON schema matching `ReviewOutput` with field descriptions and score range `0.0–10.0`. Explicitly instructs the model to return only valid JSON with no markdown fencing, preamble, or extra commentary.

The model is called via `google.generativeai` SDK using `model.generate_content([prompt_parts + image_parts])`. If the response cannot be parsed by `ReviewOutput.model_validate_json()`, raises `AIParseFailedError`.

---

### 8.3 `ReviewService` — `app/services/ai.py`

Orchestrates the full review pipeline. Injected with `SessionsRepository`, `MediaRepository`, `ReviewRepository`, `GeminiService`, `FrameExtractor`, and the Supabase Storage client.

```
review_service.generate_review(session_id, auth_user) →
    1. Load session; assert profile_id == auth_user.id  (403 if mismatch)
    2. Assert no review exists for session_id            (409 if duplicate)
    3. Load all Media for session_id                     (422 if empty)
    4. For each media item:
         download bytes from Supabase Storage
         if media_type == 'video':  frames = frame_extractor.extract(bytes)
         if media_type == 'image':  frames = [bytes]
    5. Build SurferContext from session + profile
    6. Call gemini_service.analyze_surf_media(all_frames, context)
    7. Normalise scores: clamp [0,10], round 1dp, compute overall_score
    8. Persist Review via ReviewRepository
    9. Return Review ORM object
```

---

### 8.4 `SessionsService` — `app/services/sessions.py`

Handles session CRUD business logic. Injected with `SessionsRepository`.

- `create_session(data, auth_user)` → asserts the profile exists, inserts session.
- `list_sessions(auth_user)` → returns all sessions for the user, ordered by `session_date DESC`.
- `get_session(session_id, auth_user)` → fetches by id; raises `NotFoundError` if missing; raises `ForbiddenError` if `profile_id != auth_user.id`.
- `delete_session(session_id, auth_user)` → same ownership check, then deletes (cascades to media + review).

---

### 8.5 `MediaService` — `app/services/media.py`

Handles file validation, Supabase Storage upload, and Media record persistence.

```
media_service.upload(session_id, file, auth_user) →
    1. Verify session ownership                       (403 if not owner)
    2. Read file bytes; detect true MIME via python-magic
    3. Assert MIME in whitelist                       (422 INVALID_MEDIA_TYPE if not)
    4. Assert file_size_bytes <= MAX_UPLOAD_SIZE_MB   (413 FILE_TOO_LARGE if exceeded)
    5. If video: probe duration with OpenCV           (422 VIDEO_TOO_LONG if > MAX_VIDEO_DURATION_SEC)
    6. Build storage key: {user_id}/{session_id}/{uuid}.{ext}
    7. Upload bytes to Supabase Storage bucket        (502 STORAGE_UPLOAD_FAILED on error)
    8. Persist Media record via MediaRepository
    9. Return Media ORM object
```

---

### 8.6 `StorageClient` — `app/core/storage.py`

Thin wrapper over the `supabase` Python client for Storage operations. Injected as a dependency so it can be mocked in tests.

```python
class StorageClient:
    def upload(self, key: str, data: bytes, content_type: str) -> str:
        """Upload bytes, return public URL."""

    def download(self, key: str) -> bytes:
        """Download bytes by storage key."""

    def delete(self, key: str) -> None:
        """Remove file from bucket."""
```

Initialised with `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, and `SUPABASE_BUCKET` from Settings.

---

## 9. Error Handling

### New `AppError` subclasses (added to `core/errors.py`)

| Class | `code` | `status_code` | Default message |
|---|---|---|---|
| `ForbiddenError` | `FORBIDDEN` | 403 | "You do not have access to this resource." |
| `ConflictError` | `CONFLICT` | 409 | "A conflict occurred with the current state." |
| `InvalidMediaTypeError` | `INVALID_MEDIA_TYPE` | 422 | "File type is not accepted." |
| `FileTooLargeError` | `FILE_TOO_LARGE` | 413 | "File exceeds the maximum allowed size." |
| `VideoTooLongError` | `VIDEO_TOO_LONG` | 422 | "Video exceeds the maximum allowed duration." |
| `NoMediaForSessionError` | `NO_MEDIA_FOR_SESSION` | 422 | "No media found for this session. Upload media before generating a review." |
| `ReviewAlreadyExistsError` | `REVIEW_ALREADY_EXISTS` | 409 | "A review already exists for this session." |
| `StorageUploadFailedError` | `STORAGE_UPLOAD_FAILED` | 502 | "Media storage upload failed. Please try again." |
| `AIGenerationFailedError` | `AI_GENERATION_FAILED` | 502 | "AI review generation failed. Please try again." |
| `AIParseFailedError` | `AI_PARSE_FAILED` | 502 | "AI returned an unexpected response format." |

All 5xx errors log the full exception server-side (`logger.exception(...)`) before returning the sanitised error body. The client never receives internal AI or storage error details.

---

## 10. API Contracts

Base URL: `http://localhost:8000`  
All requests/responses: `application/json` (except `POST /sessions/{id}/media/upload` which is `multipart/form-data`).  
All protected routes require `Authorization: Bearer <access_token>`.

> **API versioning note:** Phase 2 routes use the `/api/v1/` prefix to signal versioning. Phase 1 routes (`/me`, `/health`) retain their existing paths for backwards compatibility. A full `/api/v1/` migration of Phase 1 routes is left for a future housekeeping pass.

---

### 10.1 Sessions

#### `POST /api/v1/sessions/` — Create session

**Request body**
```json
{
  "sessionDate": "2026-04-17",
  "location": "Praia de Santos",
  "waveConditions": "overhead, clean, SW swell",
  "boardType": "shortboard 6'2\"",
  "notes": "Felt good on the bigger sets"
}
```

**Validation**
- `sessionDate`: required, ISO 8601 date string.
- `location`: required, non-empty string, max 200 chars.
- `waveConditions`: required, non-empty string, max 300 chars.
- `boardType`: optional, max 100 chars.
- `notes`: optional, max 1000 chars.

**Success — 201 Created**
```json
{
  "id": "b1c2d3e4-...",
  "profileId": "a7b2c3d4-...",
  "sessionDate": "2026-04-17",
  "location": "Praia de Santos",
  "waveConditions": "overhead, clean, SW swell",
  "boardType": "shortboard 6'2\"",
  "notes": "Felt good on the bigger sets",
  "createdAt": "2026-04-17T10:00:00.000Z",
  "updatedAt": "2026-04-17T10:00:00.000Z"
}
```

**Errors:** `401 MISSING_TOKEN`, `401 INVALID_TOKEN`, `400 VALIDATION_ERROR`.

---

#### `GET /api/v1/sessions/` — List sessions (current user)

Returns all sessions for the authenticated user, ordered by `sessionDate DESC`.

**Success — 200 OK**
```json
[
  { "id": "...", "sessionDate": "2026-04-17", "location": "Praia de Santos", ... },
  { "id": "...", "sessionDate": "2026-04-10", "location": "Maresias", ... }
]
```

---

#### `GET /api/v1/sessions/{session_id}` — Get session detail

**Errors:** `401`, `403 FORBIDDEN` (session belongs to another user), `404 NOT_FOUND`.

---

#### `DELETE /api/v1/sessions/{session_id}` — Delete session

Cascades to all linked media and review records via DB `ON DELETE CASCADE`.

**Success — 204 No Content**

**Errors:** `401`, `403 FORBIDDEN`, `404 NOT_FOUND`.

---

### 10.2 Media

#### `POST /api/v1/sessions/{session_id}/media/` — Upload media

**Content-Type:** `multipart/form-data`

| Field | Type | Required |
|---|---|---|
| `file` | File | Yes — image or video binary |

**Accepted MIME types (detected from bytes, not filename):**
- Images: `image/jpeg`, `image/png`, `image/webp`
- Videos: `video/mp4`, `video/quicktime`, `video/x-m4v`

**Limits:** max file size 100 MB; max video duration 120 seconds.

**Success — 201 Created**
```json
{
  "id": "c3d4e5f6-...",
  "sessionId": "b1c2d3e4-...",
  "mediaType": "image",
  "storageUrl": "https://<project>.supabase.co/storage/v1/object/public/surf-media/...",
  "fileName": "surf_session.jpg",
  "fileSizeBytes": 1048576,
  "durationSeconds": null,
  "createdAt": "2026-04-17T10:05:00.000Z"
}
```

**Errors:**
- `401` — auth errors.
- `403 FORBIDDEN` — session belongs to another user.
- `404 NOT_FOUND` — session does not exist.
- `413 FILE_TOO_LARGE` — file exceeds 100 MB.
- `422 INVALID_MEDIA_TYPE` — MIME type not in whitelist (includes list of accepted types in `details`).
- `422 VIDEO_TOO_LONG` — video duration exceeds 120 s.
- `502 STORAGE_UPLOAD_FAILED` — Supabase Storage unreachable or rejected the upload.

---

#### `GET /api/v1/media/{media_id}` — Get media record

Returns the stored Media metadata (not the file itself — use `storage_url` to fetch the file).

**Success — 200 OK** — same shape as upload response.

**Errors:** `401`, `403 FORBIDDEN`, `404 NOT_FOUND`.

---

#### `DELETE /api/v1/media/{media_id}` — Delete media

Removes the file from Supabase Storage and deletes the Media DB record. If the file is missing from storage, logs a warning but still deletes the DB record (idempotent cleanup).

**Success — 204 No Content**

**Errors:** `401`, `403 FORBIDDEN`, `404 NOT_FOUND`.

---

### 10.3 Reviews

#### `POST /api/v1/reviews/` — Generate AI review

Triggers the full AI pipeline synchronously. Expect 5–15 seconds for Gemini response.

**Request body**
```json
{
  "sessionId": "b1c2d3e4-..."
}
```

**Pipeline (server-side):**
1. Verify session ownership → `403 FORBIDDEN` if not owner.
2. Check no review exists for session → `409 REVIEW_ALREADY_EXISTS` if duplicate.
3. Load all Media for session → `422 NO_MEDIA_FOR_SESSION` if none.
4. Download each media file from Supabase Storage.
5. Extract frames from videos (OpenCV); pass images through directly.
6. Call Gemini Vision with all frames + surfer context.
7. Normalise scores (clamp 0–10, round 1dp), compute `overall_score`.
8. Persist Review record.

**Success — 201 Created**
```json
{
  "id": "d4e5f6g7-...",
  "sessionId": "b1c2d3e4-...",
  "profileId": "a7b2c3d4-...",
  "narrative": "Strong paddle technique with consistent pop-up timing...",
  "improvementTips": [
    "Shift rear foot further back during bottom turns to generate more drive.",
    "Keep arms lower and parallel to the board surface to improve balance.",
    "Practice reading the wave face earlier — position yourself deeper on take-off."
  ],
  "scoreFlow": 7.2,
  "scoreDrop": 6.8,
  "scoreBalance": 7.5,
  "scoreWaveSelection": 6.0,
  "scoreManeuvers": 5.5,
  "scoreArms": 6.5,
  "overallScore": 6.6,
  "aiModelVersion": "gemini-1.5-pro",
  "createdAt": "2026-04-17T10:10:00.000Z"
}
```

**Errors:** `401`, `403 FORBIDDEN`, `404 NOT_FOUND`, `409 REVIEW_ALREADY_EXISTS`, `422 NO_MEDIA_FOR_SESSION`, `502 AI_GENERATION_FAILED`, `502 AI_PARSE_FAILED`, `502 STORAGE_UPLOAD_FAILED` (on media download failure).

---

#### `GET /api/v1/reviews/{review_id}` — Get review by ID

**Success — 200 OK** — same shape as create response.

**Errors:** `401`, `403 FORBIDDEN`, `404 NOT_FOUND`.

---

#### `GET /api/v1/sessions/{session_id}/review` — Get review for a session

Returns the single review associated with the session.

**Success — 200 OK** — same shape as create response.

**Errors:** `401`, `403 FORBIDDEN` (session belongs to another user), `404 NOT_FOUND` (session doesn't exist or has no review).

---

### 10.4 Pydantic Schema conventions

All Phase 2 schemas follow the same `_CamelModel` base class established in Phase 1 (`alias_generator=to_camel`, `populate_by_name=True`, `from_attributes=True`). Fields are snake_case in Python, camelCase in JSON.

---

## 11. Environment Variables

All Phase 1 variables remain unchanged. The following are added to `Settings` in `core/config.py` and to `.env.example`:

| Variable | Type | Default | Purpose |
|---|---|---|---|
| `GEMINI_API_KEY` | `str` | — (required) | Gemini Vision API key |
| `GEMINI_MODEL` | `str` | `"gemini-1.5-pro"` | Model version string, stored in `ai_model_version` column |
| `FRAME_EXTRACT_COUNT` | `int` | `6` | Number of frames to sample from a video |
| `MAX_UPLOAD_SIZE_MB` | `int` | `100` | Hard cap on upload file size (MB) |
| `MAX_VIDEO_DURATION_SEC` | `int` | `120` | Hard cap on video duration (seconds) |
| `SUPABASE_BUCKET` | `str` | `"surf-media"` | Supabase Storage bucket name |

`GEMINI_API_KEY` is required — `Settings` will raise on boot if missing, same as other required fields.

---

## 12. Supabase Storage Setup

Before running Phase 2 for the first time, create the Storage bucket in the Supabase dashboard:

1. Supabase Dashboard → **Storage** → **New bucket**.
2. Name: `surf-media`.
3. **Public** access: OFF (files are accessed via service-role key from the backend).
4. File size limit: 100 MB (matches `MAX_UPLOAD_SIZE_MB`).

Storage key convention:
```
{user_id}/{session_id}/{media_id}.{ext}
```

This namespacing prevents collisions and makes per-user cleanup straightforward.

---

## 13. Docker Changes

No new containers are required. The `api` service gains new Python dependencies and one system library.

### `Dockerfile` — add system package

`python-magic` requires the `libmagic` system library. Add to the `base` stage:

```dockerfile
RUN apt-get update && apt-get install -y --no-install-recommends \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*
```

### `pyproject.toml` — add new dependencies

```toml
[project]
dependencies = [
    # ... Phase 1 deps unchanged ...
    "google-generativeai>=0.7",
    "opencv-python-headless>=4.10",
    "supabase>=2.0",
    "python-magic>=0.4",
    "aiofiles>=23.0",
    "python-multipart>=0.0.9",
]
```

### `.env.example` — add new vars

```bash
# Phase 2 additions
GEMINI_API_KEY=your-gemini-api-key-here
GEMINI_MODEL=gemini-1.5-pro
FRAME_EXTRACT_COUNT=6
MAX_UPLOAD_SIZE_MB=100
MAX_VIDEO_DURATION_SEC=120
SUPABASE_BUCKET=surf-media
```

---

## 14. Build Order

| Step | Task | Depends on |
|---|---|---|
| 1 | Add new deps to `pyproject.toml`; add `libmagic1` to Dockerfile; verify `docker compose build` succeeds | — |
| 2 | Add Phase 2 env vars to `core/config.py` and `.env.example` | Step 1 |
| 3 | Implement `core/storage.py` (Supabase Storage client) | Step 2 |
| 4 | Implement `core/frame_extractor.py` (FrameExtractor) with unit test | Step 1 |
| 5 | Write Alembic migrations 0003–0005; run `alembic upgrade head` | Step 2 |
| 6 | Implement Session model, schemas, repository, service, and `api/sessions.py` routes | Step 5 |
| 7 | Implement Media model, schemas, repository, service, and `api/media.py` routes | Steps 3, 5, 6 |
| 8 | Implement `GeminiService` in `services/ai.py` with unit test (mock API call) | Step 4 |
| 9 | Implement `ReviewService` orchestration in `services/ai.py` | Steps 7, 8 |
| 10 | Add `ReviewRepository` to `repositories/ai.py`; implement `api/reviews.py`; mount router in `main.py` | Steps 5, 9 |
| 11 | Add new error classes to `core/errors.py` | Step 10 |
| 12 | Integration tests: create session → upload image → generate review → retrieve | Step 10 |
| 13 | Error path tests: duplicate review 409, no media 422, wrong owner 403, invalid file type 422 | Step 12 |

---

## 15. Postman Flow — Definition of Done

Phase 2 is complete when these steps, run in order against `docker compose up`, all succeed:

1. `GET /health` → `200 { "status": "ok" }`.
2. Supabase signup + login (same as Phase 1) → `access_token`.
3. `GET /me` → `200` (confirms Phase 1 still works).
4. `POST /api/v1/sessions/` with valid body → `201` with session `id`.
5. `POST /api/v1/sessions/{session_id}/media/` with a valid JPEG → `201` with `storage_url` pointing to Supabase Storage (verify the URL is accessible in a browser).
6. `POST /api/v1/reviews/` with `{ "sessionId": "<id>" }` → `201` with a populated `ReviewResponse` (all 6 scores, `narrative`, 3 `improvementTips`, `overallScore`).
7. Verify `overallScore == round(mean(score_flow, score_drop, score_balance, score_wave_selection, score_maneuvers, score_arms), 1)`.
8. `GET /api/v1/reviews/{review_id}` → `200` — same data as step 6.
9. `GET /api/v1/sessions/{session_id}/review` → `200` — same data.
10. `GET /api/v1/sessions/` → `200` — list contains the session from step 4.
11. `POST /api/v1/reviews/` again for the same session → `409 REVIEW_ALREADY_EXISTS`.
12. `POST /api/v1/sessions/{session_id}/media/` with a `.txt` file (invalid type) → `422 INVALID_MEDIA_TYPE`.
13. `DELETE /api/v1/sessions/{session_id}` → `204`. Subsequent `GET /api/v1/sessions/{session_id}` → `404`.
14. `docker compose down && docker compose up` — sessions, media, and reviews from previous steps are gone (session was deleted in step 13); system starts cleanly.

A Postman collection (`docs/surf-coach-phase2.postman_collection.json`) with all requests pre-configured, using environment variables for `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `ACCESS_TOKEN`, `SESSION_ID`, `MEDIA_ID`, and `REVIEW_ID`.

---

## 16. Testing (minimum bar)

### Unit tests

- **`FrameExtractor`** — given a short synthetic MP4 fixture, assert `len(frames) == FRAME_EXTRACT_COUNT` and each element is non-empty bytes.
- **`GeminiService` prompt builder** — assert `SurferContext` fields appear verbatim in the constructed prompt string; assert Pydantic schema block is present.
- **Score normalisation** — assert clamping (negative → 0.0, >10 → 10.0), rounding (7.254 → 7.3), and `overall_score` computation (arithmetic mean of 6 values, rounded to 1dp).
- **`ReviewOutput` parsing** — valid JSON parses cleanly; JSON missing a required field raises `AIParseFailedError`; JSON with out-of-range score is normalised in the service layer.

### Integration tests

- **`POST /api/v1/sessions/`** — creates a session; assert 201 + `profile_id` matches auth user.
- **`GET /api/v1/sessions/`** — returns list containing the newly created session.
- **`POST /api/v1/sessions/{session_id}/media/`** — with real JPEG fixture, assert 201 + `storage_url` non-empty (mock Supabase Storage upload, assert client called with correct key).
- **`POST /api/v1/reviews/`** — with mocked `GeminiService` returning a fixed `ReviewOutput`, assert 201 + all scores correct + `overall_score` matches mean.
- **Duplicate review** — second `POST /api/v1/reviews/` on the same session returns 409.
- **No media** — `POST /api/v1/reviews/` on a session with no uploaded media returns 422.
- **Wrong owner** — authenticated user B attempting to access user A's session returns 403.
- **Invalid MIME type** — upload a file with `.jpg` extension but non-image magic bytes returns 422 `INVALID_MEDIA_TYPE`.

### Test fixtures

- `tests/fixtures/surf_sample.jpg` — real JPEG under 1 MB, checked into the repo.
- `tests/fixtures/surf_sample.mp4` — short (<5 s) synthetic MP4 generated by OpenCV once and committed (or generated in a conftest fixture on-the-fly using `cv2.VideoWriter`).
- Mock `GeminiService` — returns a fixed `ReviewOutput` without hitting the Gemini API. Used in all review integration tests.
- Mock `StorageClient` — records upload calls and returns a predictable URL. Used in all media tests.

Run inside the container:
```bash
docker compose exec api pytest
```

---

## 17. Security Notes (Phase 2 additions)

- **`GEMINI_API_KEY`** is server-side only — never log, echo, or include in responses.
- **File bytes are inspected with `python-magic`** (not filename) before acceptance — prevents extension-spoofed uploads.
- **Storage keys are namespaced by `user_id`** — prevents one user guessing another user's media URL.
- **All Supabase Storage operations use the service-role key** — the anon key is never used server-side for storage.
- **CORS** — Phase 2 adds CORS middleware once the Vue.js dev server URL is known. For now, leave CORS disabled and add it at the start of Phase 2 with `origins=["http://localhost:5173"]` (Vite default) when frontend work begins.
- **5xx error bodies** — never include Gemini API errors, storage error details, or stack traces. Log the full exception with `logger.exception()`, return only the sanitised `AppError` envelope.

---

## 18. Handoff to Phase 3

When Phase 2 ships, Phase 3 can assume:

- `Session`, `Media`, and `Review` tables are live and populated.
- `SessionsRepository`, `MediaRepository`, and `ReviewRepository` expose clean query methods for Phase 3 to build aggregation queries on top of (e.g. progress charts, score history).
- `GeminiService` is an injectable, mockable class — Phase 3 extends it to generate training suggestions by adding a second call method.
- `api/ai.py` stub is ready to receive training-suggestion routes without touching `main.py`.
- The standard `AppError` hierarchy and error envelope are fully in place — Phase 3 adds new error subclasses as needed.
- Docker Compose stack (including `libmagic1` and all Phase 2 deps) is stable and working.
