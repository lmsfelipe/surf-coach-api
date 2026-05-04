# SPEC — Phase 3: AI-Generated Training Plans

**Project:** Surf Coaching Platform — MVP  
**Phase:** 3 of N  
**Scope:** Training plan generation via Gemini, exercise library, training plan retrieval  
**Depends on:** Phase 2 (sessions, media upload, AI review pipeline, `Review` table, `GeminiService`)  
**Target environment:** Same Docker Compose stack as Phase 2; no new containers required

---

## 1. Phase Goal

Deliver the training suggestion feature (PRD § 4.1–4.3) so that a surfer who has received an AI review can, with a single API call, receive a structured 3-workout training plan derived from their review scores and improvement tips.

By the end of this phase a developer can, via Postman:

1. **Generate a training plan** for a session that already has a review — the pipeline reads the review's scores and improvement tips, calls Gemini, and persists a plan with 3 workouts, each containing 4–6 exercises with sets, reps, and descriptions.
2. **Retrieve the full training plan** by plan ID — all workouts and exercises included.
3. **Retrieve the training plan for a review** — shortcut via `GET /api/v1/reviews/{review_id}/training-plan`.
4. **Retrieve a single workout** and its exercises — useful when the surfer is in the middle of a session and only needs one workout at a time.

Definition of done: Postman runs `generate review → generate training plan → retrieve plan → retrieve workout` and all steps succeed with data persisted in Postgres.

This phase activates the `api/ai.py` stub that has been empty since Phase 1. All other stubs remain untouched.

---

## 2. Assumptions from Phase 2

Phase 3 can rely on the following, already shipped:

- `Review` table populated with `narrative`, `improvement_tips` (`TEXT[]`), and 6 individual scores + `overall_score`.
- `GeminiService` in `services/ai.py` — injectable, mockable, with one public method (`analyze_surf_media`). Phase 3 adds a second method without restructuring the class.
- `ReviewRepository` in `repositories/ai.py` — Phase 3 adds `TrainingPlanRepository` alongside it.
- `api/ai.py` stub with an empty `APIRouter` ready to receive routes without touching `main.py`.
- `Profile` model exposing `surf_level`, `height_cm`, `weight_kg` — all available for training context.
- Standard `AppError` hierarchy and error envelope (`core/errors.py`).
- `Settings` via `pydantic-settings` (`core/config.py`) — Phase 3 adds one optional env var.
- Alembic chain ends at `0005_create_reviews.py` — Phase 3 adds `0006` and `0007`.

---

## 3. Architecture Principles

Same Clean Architecture layers. Phase 3 fills `api/ai.py` and extends `services/ai.py` and `repositories/ai.py` — it does **not** add new folders or change `main.py`.

### Phase 3 additions within the existing structure

```
app/
├── models/
│   ├── training_plan.py      ← NEW
│   ├── workout.py            ← NEW
│   └── exercise.py           ← NEW
├── schemas/
│   └── training.py           ← NEW (TrainingPlanResponse, WorkoutResponse, ExerciseResponse)
├── api/
│   └── ai.py                 ← Phase 1/2 stub → fully implemented (training plan routes)
├── services/
│   └── ai.py                 ← extended: TrainingService added alongside ReviewService
└── repositories/
    └── ai.py                 ← extended: TrainingPlanRepository added alongside ReviewRepository
```

`main.py` — **no changes needed**. `ai.router` is already mounted.

---

## 4. Out of Scope (Phase 3)

Must **not** be implemented in this phase:

- Professional-authored training plans (PRD 4.4) — coach/personal trainer authoring UI (Phase 4).
- Exercise video upload or storage — `video_url` accepts an external URL string only; no Supabase Storage integration for video in this phase.
- Progress dashboard or score aggregation over multiple sessions.
- Plan rotation logic beyond the 3-workout structure persisted by the AI.
- Background task queue — training plan generation is **synchronous**, same as review generation.
- Rate limiting on AI pipeline endpoints.
- Board recommendation (Phase 4).
- Marketplace features.
- Vue.js frontend.

---

## 5. Tech Stack Additions (Phase 3)

No new Python dependencies required. All Phase 2 packages are sufficient.

The only addition is one optional environment variable — see § 11.

---

## 6. Data Models (Phase 3)

### 6.1 `public.training_plans`

| Column | Type | Constraints |
|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` |
| `review_id` | UUID | NOT NULL, FK → `public.reviews.id` ON DELETE CASCADE, **UNIQUE** |
| `profile_id` | UUID | NOT NULL, FK → `public.profiles.id` ON DELETE CASCADE |
| `generated_by` | TEXT | NOT NULL, check `('ai', 'coach', 'personal_trainer')`, default `'ai'` |
| `ai_model_version` | TEXT | NULL — e.g. `"gemini-1.5-pro"` |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` |

> The `UNIQUE` constraint on `review_id` enforces one plan per review. A surfer who wants a new plan must create a new session and review.

### SQLAlchemy model sketch

```python
class TrainingPlan(Base):
    __tablename__ = "training_plans"
    __table_args__ = (
        UniqueConstraint("review_id", name="uq_training_plans_review"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True,
                                     server_default=text("gen_random_uuid()"))
    review_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True),
                                             ForeignKey("public.reviews.id", ondelete="CASCADE"),
                                             nullable=False, unique=True)
    profile_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True),
                                              ForeignKey("public.profiles.id", ondelete="CASCADE"),
                                              nullable=False)
    generated_by: Mapped[str] = mapped_column(String, nullable=False, server_default="ai")
    ai_model_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())

    workouts: Mapped[list["Workout"]] = relationship(
        "Workout", back_populates="plan", order_by="Workout.sequence_number"
    )
```

---

### 6.2 `public.workouts`

| Column | Type | Constraints |
|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` |
| `plan_id` | UUID | NOT NULL, FK → `public.training_plans.id` ON DELETE CASCADE |
| `sequence_number` | INTEGER | NOT NULL, check `1–3` |
| `title` | TEXT | NOT NULL |
| `focus_area` | TEXT | NOT NULL — e.g. `"balance and bottom turn"` |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` |

> `UNIQUE (plan_id, sequence_number)` ensures no duplicate sequence within a plan.

### SQLAlchemy model sketch

```python
class Workout(Base):
    __tablename__ = "workouts"
    __table_args__ = (
        UniqueConstraint("plan_id", "sequence_number", name="uq_workouts_plan_seq"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True,
                                     server_default=text("gen_random_uuid()"))
    plan_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True),
                                           ForeignKey("public.training_plans.id", ondelete="CASCADE"),
                                           nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    focus_area: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())

    plan: Mapped["TrainingPlan"] = relationship("TrainingPlan", back_populates="workouts")
    exercises: Mapped[list["Exercise"]] = relationship(
        "Exercise", back_populates="workout", order_by="Exercise.sequence_number"
    )
```

---

### 6.3 `public.exercises`

| Column | Type | Constraints |
|---|---|---|
| `id` | UUID | PK, default `gen_random_uuid()` |
| `workout_id` | UUID | NOT NULL, FK → `public.workouts.id` ON DELETE CASCADE |
| `sequence_number` | INTEGER | NOT NULL, check `>= 1` |
| `name` | TEXT | NOT NULL |
| `description` | TEXT | NOT NULL — how to execute the exercise |
| `sets` | INTEGER | NOT NULL, check `>= 1` |
| `reps` | TEXT | NOT NULL — e.g. `"10"`, `"12–15"`, `"30 seconds"` |
| `video_url` | TEXT | NULL — external URL (YouTube, Vimeo, etc.) |
| `created_at` | TIMESTAMPTZ | NOT NULL, default `now()` |

### SQLAlchemy model sketch

```python
class Exercise(Base):
    __tablename__ = "exercises"
    __table_args__ = {"schema": "public"}

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True,
                                     server_default=text("gen_random_uuid()"))
    workout_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True),
                                              ForeignKey("public.workouts.id", ondelete="CASCADE"),
                                              nullable=False)
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    sets: Mapped[int] = mapped_column(Integer, nullable=False)
    reps: Mapped[str] = mapped_column(Text, nullable=False)
    video_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True),
                                                  server_default=func.now())

    workout: Mapped["Workout"] = relationship("Workout", back_populates="exercises")
```

---

### 6.4 Alembic Migrations

Two new migrations chained after `0005_create_reviews.py`:

**`0006_create_training_plans_and_workouts.py`**
```python
def upgrade():
    op.execute("""
        CREATE TABLE public.training_plans (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            review_id UUID NOT NULL UNIQUE REFERENCES public.reviews(id) ON DELETE CASCADE,
            profile_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
            generated_by TEXT NOT NULL DEFAULT 'ai'
                CHECK (generated_by IN ('ai', 'coach', 'personal_trainer')),
            ai_model_version TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX idx_training_plans_profile_id ON public.training_plans(profile_id);

        CREATE TABLE public.workouts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_id UUID NOT NULL REFERENCES public.training_plans(id) ON DELETE CASCADE,
            sequence_number INTEGER NOT NULL CHECK (sequence_number BETWEEN 1 AND 3),
            title TEXT NOT NULL,
            focus_area TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (plan_id, sequence_number)
        );
        CREATE INDEX idx_workouts_plan_id ON public.workouts(plan_id);
    """)

def downgrade():
    op.execute("""
        DROP TABLE IF EXISTS public.workouts;
        DROP TABLE IF EXISTS public.training_plans;
    """)
```

**`0007_create_exercises.py`**
```python
def upgrade():
    op.execute("""
        CREATE TABLE public.exercises (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workout_id UUID NOT NULL REFERENCES public.workouts(id) ON DELETE CASCADE,
            sequence_number INTEGER NOT NULL CHECK (sequence_number >= 1),
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            sets INTEGER NOT NULL CHECK (sets >= 1),
            reps TEXT NOT NULL,
            video_url TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX idx_exercises_workout_id ON public.exercises(workout_id);
    """)

def downgrade():
    op.execute("DROP TABLE IF EXISTS public.exercises;")
```

---

### 6.5 Row Level Security

```sql
-- training_plans
ALTER TABLE public.training_plans ENABLE ROW LEVEL SECURITY;
CREATE POLICY "training_plans_select_own" ON public.training_plans
    FOR SELECT USING (auth.uid() = profile_id);

-- workouts (access via training plan ownership)
ALTER TABLE public.workouts ENABLE ROW LEVEL SECURITY;
CREATE POLICY "workouts_select_own" ON public.workouts
    FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM public.training_plans tp
            WHERE tp.id = workouts.plan_id AND auth.uid() = tp.profile_id
        )
    );

-- exercises (access via workout → training plan ownership)
ALTER TABLE public.exercises ENABLE ROW LEVEL SECURITY;
CREATE POLICY "exercises_select_own" ON public.exercises
    FOR SELECT USING (
        EXISTS (
            SELECT 1 FROM public.workouts w
            JOIN public.training_plans tp ON tp.id = w.plan_id
            WHERE w.id = exercises.workout_id AND auth.uid() = tp.profile_id
        )
    );
```

Include these statements at the end of each respective migration's `upgrade()` function.

---

## 7. New Services

### 7.1 `GeminiService` — extended method in `app/services/ai.py`

Add `generate_training_plan` alongside the existing `analyze_surf_media` method. No restructuring of the class.

```python
class TrainingContext(BaseModel):
    surf_level: str
    improvement_tips: list[str]        # from Review.improvement_tips
    score_flow: float
    score_balance: float
    score_maneuvers: float
    score_wave_selection: float
    score_drop: float
    score_arms: float
    overall_score: float
    height_cm: int | None
    weight_kg: int | None

class ExerciseOutput(BaseModel):
    name: str
    description: str
    sets: int                          # >= 1
    reps: str                          # e.g. "10", "12–15", "30 seconds"
    video_url: str | None = None

class WorkoutOutput(BaseModel):
    sequence_number: int               # 1, 2, or 3
    title: str
    focus_area: str
    exercises: list[ExerciseOutput]    # 4–6 items

class TrainingPlanOutput(BaseModel):
    workouts: list[WorkoutOutput]      # exactly 3 items

class GeminiService:
    def analyze_surf_media(self, ...) -> ReviewOutput: ...   # Phase 2 — unchanged

    def generate_training_plan(
        self,
        context: TrainingContext,
    ) -> TrainingPlanOutput: ...
```

**Prompt structure for `generate_training_plan`** (three parts):

1. **System persona** — "You are an expert surf-fitness coach. Generate a structured 3-workout training plan tailored to a surfer's performance data and improvement areas."
2. **Surfer context** — JSON block with `surf_level`, `improvement_tips`, all 6 scores, `overall_score`, `height_cm`, `weight_kg`.
3. **Output schema instruction** — Strict JSON schema matching `TrainingPlanOutput`. Exactly 3 workouts, each with 4–6 exercises. Each exercise must have `name`, `description`, `sets` (integer ≥ 1), and `reps` (string). `video_url` is null unless a well-known public URL is appropriate. No markdown fencing, no preamble.

If the Gemini response cannot be parsed by `TrainingPlanOutput.model_validate_json()`, raises `AIParseFailedError`.

---

### 7.2 `TrainingService` — `app/services/ai.py`

New class added below `ReviewService`. Injected with `ReviewRepository`, `ProfileRepository`, `TrainingPlanRepository`, and `GeminiService`.

```
training_service.generate_training_plan(review_id, auth_user) →
    1. Load review by review_id                              (404 NOT_FOUND if missing)
    2. Assert review.profile_id == auth_user.id              (403 FORBIDDEN if mismatch)
    3. Assert no training plan exists for review_id          (409 TRAINING_PLAN_ALREADY_EXISTS if duplicate)
    4. Load profile (for height_cm, weight_kg, surf_level)
    5. Build TrainingContext from review + profile
    6. Call gemini_service.generate_training_plan(context)
    7. Persist TrainingPlan + 3 Workouts + exercises via TrainingPlanRepository
    8. Return TrainingPlan ORM object (with workouts and exercises eager-loaded)
```

---

### 7.3 `TrainingPlanRepository` — `app/repositories/ai.py`

Added alongside `ReviewRepository` in the same file.

```python
class TrainingPlanRepository:
    def get_by_review_id(self, review_id: UUID) -> TrainingPlan | None: ...
    def get_by_id(self, plan_id: UUID) -> TrainingPlan | None: ...
    def get_workout_by_id(self, workout_id: UUID) -> Workout | None: ...
    def create(self, plan_data: TrainingPlanCreateInternal) -> TrainingPlan: ...
```

`create` inserts the `TrainingPlan`, then bulk-inserts the 3 `Workout` rows, then bulk-inserts all exercises in a single transaction. Uses `returning()` to get generated IDs without extra round-trips.

Eager-load pattern: `get_by_id` and `get_by_review_id` use `selectinload(TrainingPlan.workouts).selectinload(Workout.exercises)` so the API layer always receives fully-populated objects without N+1 queries.

---

## 8. Error Handling

### New `AppError` subclasses (added to `core/errors.py`)

| Class | `code` | `status_code` | Default message |
|---|---|---|---|
| `TrainingPlanAlreadyExistsError` | `TRAINING_PLAN_ALREADY_EXISTS` | 409 | "A training plan already exists for this review." |
| `ReviewNotFoundError` | `REVIEW_NOT_FOUND` | 404 | "Review not found." |

> `NotFoundError` (already in Phase 2) covers plan/workout not found. `ReviewNotFoundError` is a narrower 404 specific to the review lookup in the training pipeline, useful for clear client-side messaging.

---

## 9. API Contracts

Base URL: `http://localhost:8000`  
All requests/responses: `application/json`.  
All routes require `Authorization: Bearer <access_token>`.

All routes live in `app/api/ai.py` and are mounted under the existing `ai.router`.

---

### 9.1 `POST /api/v1/training-plans/` — Generate training plan

Triggers Gemini training plan generation synchronously. Expect 5–10 seconds.

**Request body**
```json
{
  "reviewId": "d4e5f6g7-..."
}
```

**Pipeline (server-side):**
1. Verify review ownership → `403 FORBIDDEN` if not owner.
2. Check no plan exists for review → `409 TRAINING_PLAN_ALREADY_EXISTS` if duplicate.
3. Load `Profile` for surfer context.
4. Build `TrainingContext` from review + profile.
5. Call `GeminiService.generate_training_plan(context)`.
6. Persist `TrainingPlan` + workouts + exercises.

**Success — 201 Created**
```json
{
  "id": "e5f6g7h8-...",
  "reviewId": "d4e5f6g7-...",
  "profileId": "a7b2c3d4-...",
  "generatedBy": "ai",
  "aiModelVersion": "gemini-1.5-pro",
  "createdAt": "2026-04-17T10:20:00.000Z",
  "workouts": [
    {
      "id": "f6g7h8i9-...",
      "sequenceNumber": 1,
      "title": "Balance & Pop-Up Power",
      "focusArea": "balance and drop technique",
      "exercises": [
        {
          "id": "g7h8i9j0-...",
          "sequenceNumber": 1,
          "name": "Single-leg Balance on Bosu Ball",
          "description": "Stand on the flat side of a Bosu ball on one foot, arms extended for balance. Hold for the full duration, switch legs.",
          "sets": 3,
          "reps": "45 seconds",
          "videoUrl": null
        }
      ]
    }
  ]
}
```

**Errors:** `401`, `403 FORBIDDEN`, `404 REVIEW_NOT_FOUND`, `409 TRAINING_PLAN_ALREADY_EXISTS`, `502 AI_GENERATION_FAILED`, `502 AI_PARSE_FAILED`.

---

### 9.2 `GET /api/v1/training-plans/{plan_id}` — Get training plan by ID

Returns the full plan including all workouts and exercises.

**Success — 200 OK** — same shape as create response.

**Errors:** `401`, `403 FORBIDDEN`, `404 NOT_FOUND`.

---

### 9.3 `GET /api/v1/reviews/{review_id}/training-plan` — Get training plan for a review

Shortcut endpoint. Verifies the review belongs to the authenticated user, then returns the associated plan.

**Success — 200 OK** — same shape as create response.

**Errors:** `401`, `403 FORBIDDEN` (review belongs to another user), `404 NOT_FOUND` (review not found or has no plan yet).

---

### 9.4 `GET /api/v1/workouts/{workout_id}` — Get single workout

Returns one workout and its exercises. Useful for in-session display.

**Success — 200 OK**
```json
{
  "id": "f6g7h8i9-...",
  "planId": "e5f6g7h8-...",
  "sequenceNumber": 2,
  "title": "Wave Reading & Maneuver Prep",
  "focusArea": "maneuvers and wave selection",
  "exercises": [
    {
      "id": "...",
      "sequenceNumber": 1,
      "name": "Skateboard Carving Drill",
      "description": "On a skateboard or balance board, practice carving turns mimicking a surfboard bottom turn. Focus on shifting weight to the rear foot at the apex.",
      "sets": 4,
      "reps": "10",
      "videoUrl": null
    }
  ]
}
```

**Errors:** `401`, `403 FORBIDDEN`, `404 NOT_FOUND`.

---

### 9.5 Pydantic Schema conventions

Same `_CamelModel` base class from Phase 1/2 (`alias_generator=to_camel`, `populate_by_name=True`, `from_attributes=True`). Snake_case in Python, camelCase in JSON.

**`schemas/training.py`** exports:
- `GenerateTrainingPlanRequest` — `review_id: UUID`
- `ExerciseResponse`
- `WorkoutResponse` — includes `exercises: list[ExerciseResponse]`
- `TrainingPlanResponse` — includes `workouts: list[WorkoutResponse]`

---

## 10. Environment Variables

All Phase 2 variables remain unchanged. One optional variable added:

| Variable | Type | Default | Purpose |
|---|---|---|---|
| `TRAINING_WORKOUTS_PER_PLAN` | `int` | `3` | Number of workouts Gemini should generate per plan (passed in the prompt) |

Add to `core/config.py` `Settings` and `.env.example`. Not required — boots with the default.

---

## 11. Project Structure (Phase 3 Final State)

```
surf-coach-api/
├── alembic/
│   └── versions/
│       ├── 0001–0005        # Phase 1–2 — untouched
│       ├── 0006_create_training_plans_and_workouts.py  ← NEW
│       └── 0007_create_exercises.py                    ← NEW
├── tests/
│   ├── test_training.py     ← NEW
│   └── conftest.py          # updated with plan/workout fixtures
└── app/
    ├── main.py              # unchanged — ai.router already mounted
    ├── api/
    │   └── ai.py            ← stub → 4 training plan routes
    ├── services/
    │   └── ai.py            ← extended: TrainingService + GeminiService.generate_training_plan
    ├── repositories/
    │   └── ai.py            ← extended: TrainingPlanRepository
    ├── models/
    │   ├── training_plan.py ← NEW
    │   ├── workout.py       ← NEW
    │   └── exercise.py      ← NEW
    ├── schemas/
    │   └── training.py      ← NEW
    └── core/
        ├── config.py        # updated: TRAINING_WORKOUTS_PER_PLAN
        └── errors.py        # updated: TrainingPlanAlreadyExistsError, ReviewNotFoundError
```

---

## 12. Build Order

| Step | Task | Depends on |
|---|---|---|
| 1 | Add `TRAINING_WORKOUTS_PER_PLAN` to `core/config.py` and `.env.example` | — |
| 2 | Write Alembic migrations 0006–0007; run `alembic upgrade head` | Step 1 |
| 3 | Implement `TrainingPlan`, `Workout`, `Exercise` ORM models | Step 2 |
| 4 | Add `TrainingPlanAlreadyExistsError`, `ReviewNotFoundError` to `core/errors.py` | — |
| 5 | Add `generate_training_plan` method to `GeminiService` with unit test (mock API call) | Step 1 |
| 6 | Implement `TrainingPlanRepository` in `repositories/ai.py` | Step 3 |
| 7 | Implement `TrainingService` in `services/ai.py` | Steps 5, 6 |
| 8 | Implement `schemas/training.py` | Step 3 |
| 9 | Implement 4 routes in `api/ai.py` | Steps 7, 8 |
| 10 | Integration tests | Step 9 |

---

## 13. Postman Flow — Definition of Done

Phase 3 is complete when these steps, run in order against `docker compose up`, all succeed:

1. `GET /health` → `200 { "status": "ok" }`.
2. Supabase login → `access_token`.
3. `POST /api/v1/sessions/` → `201`, save `session_id`.
4. `POST /api/v1/sessions/{session_id}/media/` with a valid JPEG → `201`.
5. `POST /api/v1/reviews/` with `{ "sessionId": "..." }` → `201`, save `review_id`.
6. `POST /api/v1/training-plans/` with `{ "reviewId": "..." }` → `201` with `workouts` array containing exactly 3 items, each with 4–6 exercises.
7. `GET /api/v1/training-plans/{plan_id}` → `200` — same shape, fully populated.
8. `GET /api/v1/reviews/{review_id}/training-plan` → `200` — same data.
9. `GET /api/v1/workouts/{workout_id}` (use `workouts[1].id` from step 6) → `200` with exercises.
10. `POST /api/v1/training-plans/` again for the same review → `409 TRAINING_PLAN_ALREADY_EXISTS`.
11. `POST /api/v1/training-plans/` with a `reviewId` belonging to another user → `403 FORBIDDEN`.
12. `POST /api/v1/training-plans/` with a non-existent `reviewId` → `404 REVIEW_NOT_FOUND`.
13. Phase 2 flows still pass (create session, upload media, generate review, retrieve review).

A Postman collection (`docs/surf-coach-phase3.postman_collection.json`) with all requests pre-configured, using environment variables for `ACCESS_TOKEN`, `SESSION_ID`, `REVIEW_ID`, `PLAN_ID`, and `WORKOUT_ID`.

---

## 14. Testing (minimum bar)

### Unit tests

- **`GeminiService.generate_training_plan` prompt builder** — assert `TrainingContext` fields appear verbatim in the constructed prompt; assert output schema block is present; assert workout count instruction is present.
- **`TrainingPlanOutput` parsing** — valid JSON with 3 workouts + 4–6 exercises per workout parses cleanly; JSON missing `exercises` raises `AIParseFailedError`; JSON with 2 workouts raises `AIParseFailedError` (validation).
- **`TrainingContext` construction** — given a `Review` and `Profile` fixture, assert all 6 scores and `improvement_tips` are mapped correctly.

### Integration tests

- **`POST /api/v1/training-plans/`** — with mocked `GeminiService` returning a fixed `TrainingPlanOutput`, assert 201 + 3 workouts + exercises persisted.
- **`GET /api/v1/training-plans/{plan_id}`** — returns same structure with all nested objects.
- **`GET /api/v1/reviews/{review_id}/training-plan`** — returns same data; 404 when review has no plan.
- **`GET /api/v1/workouts/{workout_id}`** — returns single workout; 403 for wrong owner.
- **Duplicate plan** — second `POST` for same review → `409`.
- **Wrong owner** — user B requests plan generation for user A's review → `403`.
- **Missing review** — non-existent `reviewId` → `404`.

### Test fixtures

- Mock `GeminiService.generate_training_plan` — returns a fixed `TrainingPlanOutput` (3 workouts × 5 exercises) without hitting the Gemini API. Used in all training integration tests.
- `conftest.py` — add `training_plan` fixture that creates a plan row (with workouts and exercises) for use in GET tests.

Run inside the container:
```bash
docker compose exec api pytest
```

---

## 15. Security Notes (Phase 3 additions)

- **`GEMINI_API_KEY`** — already server-side only from Phase 2. No additional exposure in Phase 3.
- **Training plan access** — the same ownership check pattern used for reviews applies: `profile_id == auth_user.id`. No plan ID should be guessable without knowing its UUID.
- **Workout access** — ownership resolved by joining `workouts → training_plans → profile_id`. Never expose workout data without this chain.
- **`video_url`** — stored as a plain TEXT field; the backend does not validate or fetch the URL. The frontend is responsible for safe rendering (e.g., embedding only trusted domains). Consider adding a URL allowlist in a future phase.
- **5xx error bodies** — Gemini errors are logged server-side (`logger.exception(...)`) and never forwarded to the client. Only the sanitised `AppError` envelope is returned.

---

## 16. Handoff to Phase 4

When Phase 3 ships, Phase 4 can assume:

- `TrainingPlan`, `Workout`, and `Exercise` tables are live.
- `TrainingPlanRepository` exposes clean query methods for Phase 4 to build professional-authored plan creation on top of (PRD 4.4) — the `generated_by` column already distinguishes `'ai'` from `'coach'` / `'personal_trainer'`.
- `GeminiService` has two injectable, mockable methods. Phase 4 can extend it further (e.g., board recommendation) using the same pattern.
- `api/ai.py` has room for additional endpoints (board recommendation, coach plan creation) without touching `main.py`.
- The full review → training plan pipeline is stable and tested — Phase 4 can reference it as the integration baseline.
