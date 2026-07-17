# SPEC — Stuck Job Recovery for Async Review & Training Plan Processing

**Status:** Proposed
**Owner:** Backend
**Date:** 2026-07-17
**Parent spec:** `SPEC_BACKEND_Async_Review_Processing.md`
**Companion doc:** `SPEC_FRONTEND_Async_Review_Processing.md`

---

## 1. Problem

The async processing pipeline (arq worker + `status` column) handles failures
the worker can **observe**: an exception inside `process_review_task` is caught
and the row is marked `failed` with a friendly message (`app/worker.py:64-77`).

It does **not** handle jobs that die without running their error handler. In
every case below, the row stays in `"processing"` forever — and the user is
**permanently locked out**, because:

- `POST /api/v1/reviews/` is blocked by `ReviewAlreadyExistsError`
  (the guard in `app/services/ai.py:298-300` blocks `"processing"` rows), and
- `POST /api/v1/reviews/{id}/retry` returns `409 REVIEW_NOT_RETRYABLE`
  (`retry_review` only allows `"failed"` rows).

The only way out today is a manual DB fix. This violates the parent spec's own
acceptance criterion: *"The UI is never permanently blocked."*

### 1.1 Hole A — `job_timeout` cancellation is not caught

`WorkerSettings.job_timeout` is configured (`WORKER_JOB_TIMEOUT_SEC`, default
300 s), but arq enforces it by **cancelling the task**, which raises
`asyncio.CancelledError` inside the coroutine. `CancelledError` is a
`BaseException` (Python ≥ 3.8), so the worker's `except Exception` block
(`app/worker.py:67`) never runs and `mark_failed` is never called.

A job that hits the timeout leaves its row in `"processing"` permanently.
With `retry_jobs = False`, arq will not re-run it either.

### 1.2 Hole B — enqueue failure after row insert

All four routes commit the `"processing"` row first, then enqueue with no
error handling:

```python
# app/api/reviews.py:60-61 (same pattern in retry route and app/api/ai.py)
review = await service.enqueue_review(payload.session_id, user)
await arq_pool.enqueue_job("process_review_task", str(review.id))
```

If Redis is down or the enqueue call raises for any reason, the client gets a
500 — but the row is already committed as `"processing"` with no job behind
it. Subsequent creates hit `ReviewAlreadyExistsError`; retry hits the 409.

### 1.3 Hole C — worker hard-crash or Redis data loss

- Worker OOM-killed / `SIGKILL` mid-job: no `except` block runs. With
  `retry_jobs = False` the job is abandoned, not re-run.
- Redis restarts without persistence after enqueue: the job vanishes.

In both cases nothing ever transitions the row out of `"processing"`.

### 1.4 Missing timestamp anchor

There is no way to tell *how long* a row has been processing. `reviews` and
`training_plans` only have `created_at`, and `reset_for_retry`
(`app/repositories/ai.py:108-115`) does not touch any timestamp — so a
sweeper keyed on `created_at` would incorrectly sweep a review that was
created days ago and retried a minute ago.

## 2. Goals

- **No permanently stuck rows.** Every `"processing"` row eventually
  transitions to `"completed"` or `"failed"`, no matter how the job died.
- Failures always land the user in the existing `failed` → retry-button UI
  flow; no new client states required.
- Timeout failures surface **within the frontend's 5-minute polling window**
  where possible.

## 3. Non-goals

- Automatic worker-side retry with backoff (still Phase 2, per parent spec).
- SSE / push notifications (parent spec §8).
- Dead-letter queue (parent spec §8).

---

## 4. Design

Three fixes, one per hole, plus a shared prerequisite (§4.1). The sweeper
(§4.4) is the belt-and-braces catch-all: it recovers from *any* lost job
regardless of cause, so §4.2/§4.3 exist only to make common failures surface
in seconds/minutes instead of waiting for the sweep threshold.

### 4.1 Prerequisite — `processing_started_at` column

Add a nullable timestamp to both tables recording when the **current**
processing attempt began:

| Write path | Change |
|---|---|
| `ReviewRepository.create_pending` / `TrainingPlanRepository.create_pending` | set `processing_started_at = func.now()` |
| `reset_for_retry` (both repos) | set `processing_started_at = func.now()` |
| `mark_completed` / `mark_failed` | no change (sweeper filters on `status`) |

This is the staleness anchor for the sweeper and correctly restarts the clock
on every retry.

### 4.2 Fix A — mark timed-out jobs as failed

In both task functions in `app/worker.py`, handle cancellation explicitly and
re-raise it (arq expects cancelled jobs to propagate `CancelledError`):

```python
TIMEOUT_MESSAGE = "Processing took too long and was stopped."

async def process_review_task(ctx: dict, review_id: str) -> None:
    service = await _build_review_service()
    try:
        await service.process_review(UUID(review_id))
    except asyncio.CancelledError:
        # job_timeout fired (or worker shutdown) — record the failure, then
        # let arq see the cancellation. Shield the DB write so it is not
        # itself cancelled mid-flight.
        await asyncio.shield(_mark_review_failed(review_id, TIMEOUT_MESSAGE))
        raise
    except Exception as exc:
        await _mark_review_failed(review_id, _friendly_message(exc))
    finally:
        await service.sessions_repo.db.close()
```

`_mark_review_failed` / `_mark_plan_failed` extract the existing
"fresh `SessionLocal`, mark, close" block into helpers shared by both branches.

**Config change:** lower `WORKER_JOB_TIMEOUT_SEC` default from `300` to `240`.
The frontend stops polling at 5 minutes; a 4-minute job timeout means the
`failed` state (and retry button) appears while the client is still polling.

**Note on shutdown:** a clean worker shutdown (`SIGTERM`, deploy) also cancels
in-flight jobs, so this path marks them `failed` too. That is acceptable — the
user gets a retry button rather than a stuck spinner — and the sweeper would
have reached the same terminal state anyway.

### 4.3 Fix B — guard the enqueue call

Wrap enqueue in all four routes (`app/api/reviews.py` create + retry,
`app/api/ai.py` create + retry). On enqueue failure, mark the row `failed`
and **return it in the normal `202` response**:

```python
ENQUEUE_FAILED_MESSAGE = "Processing could not be started. Please try again."

review = await service.enqueue_review(payload.session_id, user)
try:
    await arq_pool.enqueue_job("process_review_task", str(review.id))
except Exception:
    logger.exception("Failed to enqueue review %s", review.id)
    review = await service.review_repo.mark_failed(review.id, ENQUEUE_FAILED_MESSAGE)
return ReviewOut.model_validate(review)
```

Returning the `failed` object (rather than a 500) means the frontend's
existing flow handles it with zero changes: the seeded cache renders the
failed state with the retry button immediately, and retry re-attempts the
enqueue. If Redis is still down, retry fails the same way — the user loops
through a working retry button instead of hitting `ReviewAlreadyExistsError`.

Prefer a small shared helper (e.g., `safe_enqueue(pool, task_name, row, repo)`)
over four copies of the try/except.

### 4.4 Fix C — sweeper cron job (the catch-all)

An arq **cron job** in the same worker process (arq has native cron support —
no new infrastructure) runs every minute and fails any row stuck in
`"processing"` beyond a threshold:

```python
# app/worker.py
from arq import cron

SWEEP_MESSAGE = "Processing was interrupted."

async def sweep_stuck_jobs(ctx: dict) -> None:
    threshold = get_settings().STUCK_JOB_THRESHOLD_SEC
    db = SessionLocal()
    try:
        for table in ("reviews", "training_plans"):
            result = await db.execute(
                text(f"""
                    UPDATE public.{table}
                    SET status = 'failed', error_message = :msg
                    WHERE status = 'processing'
                      AND processing_started_at < now() - make_interval(secs => :threshold)
                """),
                {"msg": SWEEP_MESSAGE, "threshold": threshold},
            )
            if result.rowcount:
                logger.warning("Sweeper: marked %d stuck %s as failed", result.rowcount, table)
        await db.commit()
    finally:
        await db.close()


class WorkerSettings:
    functions = [process_review_task, process_training_plan_task]
    cron_jobs = [cron(sweep_stuck_jobs, minute=set(range(60)), run_at_startup=True)]
    ...
```

**Config:** `STUCK_JOB_THRESHOLD_SEC`, default `600` (10 min). Constraint:
must exceed `WORKER_JOB_TIMEOUT_SEC` plus worst-case queue wait, so the
sweeper never races a job that is legitimately still running. With defaults:
job timeout at 4 min, sweep at 10 min, sweep cadence 1 min → worst-case
recovery ~11 min after the attempt started.

`run_at_startup=True` clears any backlog accumulated while the worker itself
was down.

The sweeper needs **no knowledge of arq state** — it doesn't matter whether
the job was never enqueued, lost by Redis, or died with the worker. Rows in
old-but-valid states (`completed`, `failed`, retried-then-completed) are
untouched because it filters on `status` + `processing_started_at`.

### 4.5 Why not `after_job_end` hooks / arq result inspection

Considered syncing DB status from arq job outcomes in an `after_job_end`
hook. Rejected: it only covers jobs arq *knows about* (misses Hole B and
Redis loss), duplicates what §4.2 does more directly, and couples DB state to
arq internals. The sweeper subsumes it.

### 4.6 Why not staleness-based retryability

Considered allowing `POST /retry` on `"processing"` rows older than N
minutes. Rejected as primary mechanism: it puts recovery behind a user
action, complicates the retry contract, and races the worker if the job is
slow rather than dead. The sweeper keeps the API contract exactly as the
parent spec defines it. (Can be revisited as a UX escape hatch if sweeper
latency proves annoying.)

### 4.7 Frontend coordination

No frontend changes are required — every recovery path lands in the existing
`failed` + retry flow. Two timing notes for the frontend team:

- Timeout failures (`WORKER_JOB_TIMEOUT_SEC = 240`) surface inside the
  5-minute polling window → user sees the retry button without reloading.
- Sweeper recoveries (crash/lost-job cases) land at up to ~11 min, after
  polling has stopped. The client's `refetchOnWindowFocus` picks up the
  `failed` state on the next focus/reload. Acceptable for a rare case.
- Reminder of a known frontend bug tracked separately: the client's 5-minute
  polling window is anchored to component mount and is not reset when a retry
  succeeds, so a retry clicked >5 min after mount never resumes polling.

## 5. Database migration

```python
# alembic — 0012_add_processing_started_at.py
def upgrade() -> None:
    op.add_column(
        "reviews",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )
    op.add_column(
        "training_plans",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )
    # Backfill in-flight rows so the sweeper can age them from migration time.
    op.execute("UPDATE public.reviews SET processing_started_at = now() WHERE status = 'processing'")
    op.execute("UPDATE public.training_plans SET processing_started_at = now() WHERE status = 'processing'")
```

No index needed at current scale: the sweeper scans rows with
`status = 'processing'`, which is a handful at any time. Revisit with a
partial index (`WHERE status = 'processing'`) if volume grows.

## 6. Config summary

| Setting | Default | Change |
|---|---|---|
| `WORKER_JOB_TIMEOUT_SEC` | ~~300~~ → **240** | lowered so timeout failures beat the frontend's 5-min polling cutoff |
| `STUCK_JOB_THRESHOLD_SEC` | **600** (new) | sweeper staleness threshold; must be > job timeout + queue wait |

## 7. Acceptance criteria

- [ ] A job that exceeds `WORKER_JOB_TIMEOUT_SEC` results in `status: "failed"`
      with `errorMessage` = timeout message (test: task that sleeps past a
      short timeout).
- [ ] `POST /api/v1/reviews/` with Redis unavailable returns `202` with
      `status: "failed"` and the enqueue-failure message — **not** a 500 and
      **not** a stuck `"processing"` row (test: mock `enqueue_job` to raise).
- [ ] Retry on such a review re-attempts the enqueue and succeeds once Redis
      is back.
- [ ] `SIGKILL` the worker mid-job → the row becomes `failed` within
      `STUCK_JOB_THRESHOLD_SEC` + 60 s of the attempt start (sweeper test).
- [ ] A review retried after sitting `failed` for hours is **not** swept
      immediately (`processing_started_at` reset by `reset_for_retry`).
- [ ] Sweeper ignores `completed` and `failed` rows.
- [ ] Sweeper covers `training_plans` identically.
- [ ] Cron job runs at startup and every minute (assert registration in
      `WorkerSettings.cron_jobs`).
- [ ] End-to-end: after any of the above failures, the user can tap
      "Tentar novamente" and reach `completed` — no state requires a manual
      DB fix.

## 8. Open questions

1. **Error message language.** Worker messages are English
   ("AI service is temporarily unavailable.") while the UI is pt-BR; the
   frontend displays `errorMessage` verbatim when present, so users currently
   see mixed-language errors. Options: (a) translate the backend message
   catalog to pt-BR, (b) return a machine-readable `errorCode` alongside
   `errorMessage` and let the frontend map codes to localized strings
   (frontend already has this map). Recommend (b) as the durable fix; (a) as
   the quick one. Out of scope here — new messages introduced by this spec
   follow whatever convention is chosen.
