# SPEC — Async Review & Training Plan Processing

**Status:** Proposed
**Owner:** Backend
**Date:** 2026-07-14

---

## 1. Problem

Review generation (`POST /api/v1/reviews/`) and training plan generation
(`POST /api/v1/ai/training-plans/`) execute **synchronously** inside the HTTP
request handler. A single review request does:

1. **Download** every media file from Supabase Storage (network I/O, blocking SDK)
2. **Extract frames** from videos via OpenCV (CPU-bound)
3. **Call Gemini** `generate_content()` with images (10–60 s round-trip, blocking SDK)
4. **Persist** the review to Postgres

All of this runs inside an `async def` endpoint, but the Gemini and Supabase
SDKs are **synchronous** — they block the event loop. With `--workers 2` in the
production Dockerfile, **two concurrent review requests freeze every other
request** (health checks, logins, uploads) on those workers.

Additionally, holding an HTTP connection open for 30–60 s will trip
load-balancer / reverse-proxy timeouts (commonly 30–60 s), resulting in `504
Gateway Timeout` even when the review succeeds server-side.

## 2. Goals

- Review and training plan generation run **in the background**, not inline
  with the HTTP request.
- The API returns immediately (`202 Accepted`) so the client is never blocked.
- The event loop is never starved by blocking SDK calls.
- Users can poll for completion status.
- Failures are surfaced cleanly — the client sees a `failed` status with an
  error message, not a timeout.

## 3. Non-goals

- WebSocket / SSE push notifications for completion (can be added later).
- Automatic retry (worker-side retry with backoff — Phase 2).
- Changing the upload flow.

---

## 4. Design

### 4.1 Task queue — `arq` (recommended)

[`arq`](https://arq-docs.helpmanual.io/) is a lightweight async-native task
queue built on Redis. It fits the stack well because:

- Native `asyncio` — runs inside the same event loop style as FastAPI.
- Minimal dependencies (just Redis).
- Built-in retry, timeout, and result storage.

Alternative considered: **Celery**. Rejected — heavier dependency tree,
sync-first design, overkill for current scale.

Alternative considered: **`asyncio.to_thread()`** only. Rejected — unblocks the
event loop but does not solve the HTTP timeout problem. The client still waits
for completion.

### 4.2 New infrastructure dependency

- **Redis** — added to `docker-compose.yml` for local dev, required in
  production (any managed Redis: AWS ElastiCache, Railway Redis, etc.).

### 4.3 Review lifecycle — status field

Add a `status` column to the `reviews` table:

```sql
ALTER TABLE reviews ADD COLUMN status VARCHAR(20) NOT NULL DEFAULT 'completed';
-- Backfill existing rows (all were generated synchronously and succeeded):
UPDATE reviews SET status = 'completed' WHERE status = 'completed';
```

Allowed values:

| Status | Meaning |
|---|---|
| `processing` | Job enqueued, not yet finished |
| `completed` | AI analysis finished, scores and narrative populated |
| `failed` | Job failed after retries; `error_message` column has details |

Similarly for `training_plans`.

### 4.4 Updated API contract

#### Create review

```
POST /api/v1/reviews/
```

**Before:**
- Returns `201` with full `ReviewOut` (after 10–60 s).

**After:**
- Returns `202 Accepted` with a partial `ReviewOut`:

```jsonc
{
  "id": "uuid",
  "sessionId": "uuid",
  "status": "processing",
  "narrative": null,
  "improvementTips": [],
  "scores": null,
  "overallScore": null,
  "createdAt": "2026-07-14T12:00:00Z"
}
```

#### Poll for completion

```
GET /api/v1/reviews/{review_id}
```

Returns the same `ReviewOut`. When `status` is `"completed"`, all fields are
populated. When `"failed"`, an `errorMessage` field is present.

The frontend polls this endpoint on an interval (suggested: every 3 s for the
first 30 s, then every 10 s, stop after 5 min).

#### Retry a failed review

```
POST /api/v1/reviews/{review_id}/retry
```

Only allowed when `status` is `"failed"`. Resets the review to `"processing"`,
clears `error_message`, and enqueues a new worker job. Returns `202` with the
updated `ReviewOut`.

Returns `409 REVIEW_NOT_RETRYABLE` if the review is `"processing"` or
`"completed"`.

#### Create training plan — same pattern

`POST /api/v1/ai/training-plans/` → `202` with `status: "processing"`.
Retry: `POST /api/v1/ai/training-plans/{plan_id}/retry`.

### 4.5 Failure handling & UI behavior

This is what the user sees at each stage:

| Review status | UI behavior |
|---|---|
| `processing` | Loading state — spinner/skeleton with "Analyzing your session…" message. Poll every 3 s. |
| `completed` | Full review displayed (scores, narrative, tips). |
| `failed` | Error state with `errorMessage` shown, plus a **"Try again"** button that calls `POST /reviews/{id}/retry`. |

**Key design decision — the UI is never permanently blocked.** A failed review
does not prevent the user from retrying. The retry endpoint exists specifically
for this: it reuses the existing review row (same `id`, same `session_id`)
instead of creating a new one, so the `ReviewAlreadyExistsError` guard is
never hit.

#### What can fail and why

| Failure | `errorMessage` (example) | User action |
|---|---|---|
| Gemini API timeout / 5xx | "AI service is temporarily unavailable." | Retry (transient) |
| Gemini content filter block | "AI could not analyze this content." | Retry with different media, or contact support |
| Supabase download failure | "Could not retrieve your media files." | Retry (transient) |
| Frame extraction failure | "Video could not be processed." | Re-upload a different video |
| Worker crash / OOM | "Processing was interrupted." | Retry (transient) |

#### Existing guard change

The current code at `app/services/ai.py:292-294` blocks review creation if
**any** review row exists for the session:

```python
existing = await self.review_repo.get_for_session(session_id)
if existing is not None:
    raise ReviewAlreadyExistsError()
```

This must change to only block when the existing review is `"completed"` or
`"processing"`:

```python
existing = await self.review_repo.get_for_session(session_id)
if existing is not None and existing.status in ("completed", "processing"):
    raise ReviewAlreadyExistsError()
```

The retry endpoint handles the `"failed"` → `"processing"` transition
separately (see §4.4).

### 4.5 Worker process

A separate `arq` worker process runs alongside the API:

```bash
# docker-compose.yml — new service
worker:
  build: .
  command: arq app.worker.WorkerSettings
  depends_on: [redis, db]
  env_file: .env
```

The worker module (`app/worker.py`) imports the existing `ReviewService` and
`TrainingPlanService` and calls them inside a task function. The blocking
Gemini/Supabase calls are wrapped in `asyncio.to_thread()` inside the worker
so they don't block other queued tasks.

### 4.7 Sequence — happy path

```
Client                      API                        Redis / arq worker         Supabase / Gemini
  │ POST /reviews/           │                            │                          │
  │ ────────────────────────▶│                            │                          │
  │                          │ INSERT review (processing) │                          │
  │                          │ enqueue job ──────────────▶│                          │
  │ ◀──── 202 { status:     │                            │                          │
  │        "processing" }    │                            │                          │
  │                          │                            │ download media ─────────▶│
  │                          │                            │ extract frames           │
  │                          │                            │ Gemini analyze ─────────▶│
  │                          │                            │ ◀──────── response ──────│
  │                          │                            │ UPDATE review (completed)│
  │ GET /reviews/{id}        │                            │                          │
  │ ────────────────────────▶│                            │                          │
  │ ◀──── 200 { status:     │                            │                          │
  │        "completed", …}   │                            │                          │
```

### 4.8 Sequence — failure + retry

```
Client                      API                        Redis / arq worker         Supabase / Gemini
  │ POST /reviews/           │                            │                          │
  │ ────────────────────────▶│                            │                          │
  │                          │ INSERT review (processing) │                          │
  │                          │ enqueue job ──────────────▶│                          │
  │ ◀──── 202 { status:     │                            │                          │
  │        "processing" }    │                            │                          │
  │                          │                            │ download media ─────────▶│
  │                          │                            │ Gemini analyze ─────────▶│
  │                          │                            │ ◀──────── 500 error ─────│
  │                          │                            │ UPDATE review (failed,   │
  │                          │                            │   error_message="…")     │
  │ GET /reviews/{id}        │                            │                          │
  │ ────────────────────────▶│                            │                          │
  │ ◀──── 200 { status:     │                            │                          │
  │   "failed", errorMsg }   │                            │                          │
  │                          │                            │                          │
  │ ── user taps "Try again" │                            │                          │
  │                          │                            │                          │
  │ POST /reviews/{id}/retry │                            │                          │
  │ ────────────────────────▶│                            │                          │
  │                          │ UPDATE review (processing) │                          │
  │                          │ enqueue job ──────────────▶│                          │
  │ ◀──── 202 { status:     │                            │                          │
  │        "processing" }    │                            │                          │
  │          … (poll again until completed) …             │                          │
```

---

## 5. Migration strategy

This is a **breaking change** to the review creation response (201 → 202,
fields initially null). Coordinate with frontend:

- **Option A (recommended):** Ship backend + frontend together. Frontend
  switches to poll-based flow in the same release.
- **Option B (transitional):** Keep a `?sync=true` query param that preserves
  the old blocking behavior for one release, then remove it.

## 6. Database migration

```python
# alembic — new migration
op.add_column("reviews", sa.Column("status", sa.String(20), nullable=False, server_default="completed"))
op.add_column("reviews", sa.Column("error_message", sa.Text, nullable=True))
op.add_column("training_plans", sa.Column("status", sa.String(20), nullable=False, server_default="completed"))
op.add_column("training_plans", sa.Column("error_message", sa.Text, nullable=True))
```

## 7. Acceptance criteria

- [ ] `POST /api/v1/reviews/` returns `202` within 1 s (not blocked by AI).
- [ ] `GET /api/v1/reviews/{id}` shows `status: "processing"` immediately after
      creation, then `"completed"` once the worker finishes.
- [ ] A failed Gemini call results in `status: "failed"` with a meaningful
      `errorMessage`, not a timeout or 5xx on the API.
- [ ] Two concurrent review requests do not block health checks or other API
      endpoints.
- [ ] Existing reviews (created before migration) have `status: "completed"`.
- [ ] Worker process starts cleanly via `docker compose up`.
- [ ] Training plan creation follows the same async pattern.
- [ ] `POST /reviews/{id}/retry` on a `"failed"` review returns `202` and
      re-enqueues the job.
- [ ] `POST /reviews/{id}/retry` on a `"completed"` review returns `409`.
- [ ] `POST /reviews/{id}/retry` on a `"processing"` review returns `409`.
- [ ] A user can retry a failed review multiple times until it succeeds.
- [ ] The UI is never permanently blocked — failed state always shows a retry
      action.

## 8. Future enhancements (out of scope)

- WebSocket / SSE push notification when processing completes.
- Configurable retry count and backoff for failed jobs.
- Dead-letter queue for inspection of permanently failed jobs.
- Priority queue (e.g., paid users processed first).
