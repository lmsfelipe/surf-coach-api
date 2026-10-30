# SPEC — Merge the arq Worker into the API (Single Sleepable Service)

**Status:** Implemented (2026-10-29)
**Owner:** Backend
**Date:** 2026-10-21
**Supersedes (in part):** `SPEC_BACKEND_Async_Review_Processing.md` §4.1–4.2, §4.5 (arq + Redis + worker process),
`SPEC_BACKEND_Stuck_Job_Recovery.md` §4.4 (per-minute sweeper cron),
`SPEC_BACKEND_Video_Optimization.md` (server-side transcode)
**Unchanged:** the HTTP contract (202 + poll), the `status` lifecycle, the frontend.
**Companion spec:** `SPEC_BACKEND_Job_Queue_Hardening.md` adds the queue limit, separate
wait and run timeouts, Gemini deadlines and job metrics on top of the `JobRunner`
defined here. **Implement the two together.** This spec alone lets bursts fail late
with a misleading message and leaves hung Gemini threads inside the API process.
Where this spec and the companion disagree (the `JobRunner` signature, the
config table, the route snippet), the companion wins.

---

## 1. Problem

Production runs three always-on Railway services. The project costs money even
with zero users, and almost all of it is idle RAM:

| Service                         | Share of bill | Why it never sleeps                                                                                            |
| ------------------------------- | ------------- | -------------------------------------------------------------------------------------------------------------- |
| `diplomatic-grace` (arq worker) | 57 %          | arq polls Redis every ~0.5 s; `sweep_stuck_jobs` cron runs every minute; `sweep_unoptimized_media` every 5 min |
| `surf-coach-api`                | 39 %          | Could sleep, but Serverless was off                                                                            |
| Redis                           | 4 %           | Kept awake by the worker's polling (highest CPU of the three)                                                  |

Railway bills RAM per minute of runtime (≈ $10 per GB-month). A Serverless
service sleeps after ~10 min **without outbound traffic**, and traffic to other
services in the same project counts. So as long as a worker polls Redis,
neither the worker nor Redis can ever sleep.

At current traffic (a handful of reviews per day, `RATE_LIMIT_AI = 5/hour`
per user) a separate queue process buys nothing: jobs are short (≤ 240 s),
there is no fan-out, and the database already records job state
(`status`, `processing_started_at`).

## 2. Goals

1. **One Railway service.** The API process runs reviews and training plans
   itself. Delete the worker service and Redis.
2. **Idle means asleep.** With no inbound traffic, the API produces no
   outbound traffic, so Railway Serverless puts it to sleep. Idle cost ≈ $0.
3. **Same failure guarantees as today.** A row never stays `processing`
   forever. Timeouts, exceptions, redeploys and hard kills all end in
   `failed` with a friendly message, and the user can retry.
4. **No contract change.** Same endpoints, status codes, response shapes,
   polling behaviour. The frontend does not change.
5. **Shared event loop stays responsive.** Heavy work (download, frame
   extraction, Gemini) stays off the event loop, so HTTP latency doesn't
   suffer while a job runs.

## 3. Non-goals

- Durable queueing or automatic retries. Today `retry_jobs = False`; a lost
  job becomes `failed` and the user taps Retry. That stays.
- Horizontal job scaling or multi-replica coordination (`numReplicas` stays 1).
- Keeping the server-side video optimizer. Videos are compressed in the browser
  before upload (Mediabunny/WebCodecs, 720p). The optimizer's periodic sweep
  would stop the service from sleeping, so it is removed (§4.7).
- Dropping the `media.optimized_at` / `optimize_attempts` columns. A later
  migration can drop them once nothing reads them.

## 4. Design

### 4.1 Overview

```
Before                                   After
──────                                   ─────
POST /reviews ─► API ─► Redis ─► worker   POST /reviews ─► API ─┬─► 202
                  │                │                           └─► JobRunner task (same process)
                  └──► 202         └─► Gemini/Storage/DB                └─► Gemini/Storage/DB
cron: sweep every 1 min (worker)         sweep: once at startup + read-path check
```

### 4.2 `JobRunner` — `app/core/jobs.py` (new)

A small in-process runner that replaces arq. It owns all background tasks so it
can limit concurrency, apply timeouts, and drain them on shutdown.

```python
class JobRunner:
    def __init__(self, max_concurrent: int, timeout_sec: int, shutdown_grace_sec: int): ...

    def submit(
        self,
        fn: Callable[[str], Awaitable[None]],
        job_id: str,
        *,
        on_failure: Callable[[str, str], Awaitable[None]],
    ) -> None:
        """Schedule fn(job_id) and return immediately. Raises JobRunnerClosedError after close()
        (and JobRunnerBusyError when the queue is full; see the companion spec §4.2)."""

    @property
    def shutting_down(self) -> bool: ...

    async def close(self) -> None:
        """Stop accepting work, wait up to shutdown_grace_sec, then cancel what's left."""
```

Behaviour:

| Concern           | Rule                                                                                                                                                                                                                                             |
| ----------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Strong references | Each task goes in `self._tasks: set[asyncio.Task]` and is removed by `add_done_callback`. A bare `create_task` can be garbage-collected mid-run.                                                                                                 |
| Concurrency       | `asyncio.Semaphore(max_concurrent)`. Jobs beyond the cap wait in the runner, not in Redis.                                                                                                                                                       |
| Timeout           | `async with asyncio.timeout(timeout_sec)` wraps **semaphore wait + run**. Total wall time from submit is ≤ `JOB_TIMEOUT_SEC`, so the invariant `STUCK_JOB_THRESHOLD_SEC > JOB_TIMEOUT_SEC` (§4.5) holds without a separate queue-wait allowance. |
| Errors            | The wrapper catches and logs any exception or `TimeoutError` that escapes the job, so one job can never crash the runner or produce "Task exception was never retrieved" warnings.                                                               |
| Jobs that never start | A job cancelled while it's still waiting for a slot (timeout or shutdown) never enters `fn`, so `fn`'s own `except CancelledError` can't mark the row. The runner calls `on_failure(job_id, message)` for that case instead (`mark_review_failed` / `mark_plan_failed`). Once `fn` is running, `fn` handles its own failures and `on_failure` is not called. Without this, those rows stay `processing` until the startup sweep or read-path check. Full design: companion spec §4.2. |
| Signature         | `submit(fn, job_id, *, on_failure)`, where `fn` is the task function itself. The test fake records `(fn.__name__, (job_id,))`, so existing assertions like `("process_review_task", (id,))` keep their shape.                                     |

Why not FastAPI `BackgroundTasks`: it has no concurrency cap, no timeout and no
handle for draining on shutdown, and it runs inside the request's middleware
stack. Why not keep arq in-process (`Worker.async_run()` in the lifespan):
Redis would still be needed and still polled, which is exactly what keeps the
service awake.

### 4.3 Task functions — `app/worker.py` → `app/jobs.py`

Move `process_review_task`, `process_training_plan_task`, `sweep_stuck_jobs`,
`ERROR_MESSAGES`, `_friendly_message` and the `_mark_*_failed` helpers into
`app/jobs.py`, mostly unchanged:

- Drop the arq `ctx` parameter: `async def process_review_task(review_id: str) -> None`.
- Keep the existing structure: build the service with its own `SessionLocal()`
  (never the request's session, which closes when the response is sent),
  `except asyncio.CancelledError` → shielded mark-failed → re-raise,
  `except Exception` → friendly message, `finally` close the DB session.
- Pick the message on cancellation based on why it happened:

  - `runner.shutting_down` is true → `SWEEP_MESSAGE` ("Processing was interrupted.")
  - otherwise, it was the timeout → `TIMEOUT_MESSAGE` ("Processing took too long and was stopped.")

  Pass the runner in (or read a module-level flag set by `close()`) so the
  function stays testable without an app.

- Delete `WorkerSettings`, `optimize_media_task`, `sweep_unoptimized_media`,
  `_key_from_storage_url` and every `arq` import.
- Update log prefixes from `Worker:` to `Job:` (structlog event names).

Keeping the event loop free: `ReviewService.process_review` already runs
`storage.download` and `frame_extractor.extract` in `asyncio.to_thread`, and
Gemini calls go through `to_thread` too (`app/services/ai.py:231`). Any CPU-bound
or blocking step added later **must** do the same, because it now shares the
loop with HTTP. Add a comment at the top of `app/jobs.py` saying so.

### 4.4 Wiring — dependency, routes, lifespan

**Dependency** (`app/core/deps.py`): replace `get_arq_pool` / `close_arq_pool`
with

```python
def get_job_runner(request: Request) -> JobRunner:
    return request.app.state.job_runner
```

**Routes** (`app/api/reviews.py`, `app/api/ai.py` — 4 call sites): replace the
injected pool with the runner, and keep the existing guard:

```python
review = await service.enqueue_review(payload.session_id, user)
try:
    runner.submit(process_review_task, str(review.id), on_failure=mark_review_failed)
except JobRunnerBusyError:  # companion spec §4.4
    review = await service.review_repo.mark_failed(review.id, BUSY_MESSAGE)
except Exception:  # JobRunnerClosedError during shutdown
    logger.exception("Failed to enqueue review %s", review.id)
    review = await service.review_repo.mark_failed(review.id, ENQUEUE_FAILED_MESSAGE)
return ReviewOut.model_validate(review)
```

`submit` only fails when the runner is closed, but the guard keeps the
"never leave a row `processing` with no job behind it" rule
(Stuck Job Recovery, Fix B).

**Lifespan** (`app/main.py`):

```python
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.job_runner = JobRunner(
        max_concurrent=settings.JOBS_MAX_CONCURRENT,
        timeout_sec=settings.JOB_TIMEOUT_SEC,
        shutdown_grace_sec=settings.JOB_SHUTDOWN_GRACE_SEC,
    )
    await _sweep_on_startup()   # §4.5 — logs and continues on failure
    yield
    await app.state.job_runner.close()   # drain before the DB pool goes away
    await engine.dispose()
```

`_sweep_on_startup` wraps `sweep_stuck_jobs()` in try/except plus a short
`asyncio.timeout`. If the DB is briefly unreachable the app must still boot
(the readiness probe reports the DB separately), and the read-path check (§4.5)
covers it in the meantime.

### 4.5 Stuck-job recovery without a cron

The per-minute cron existed to clean up after a worker that died without running
its handlers. In a single process, every failure falls into one of these cases:

| Failure                         | Who marks the row `failed`                                                         |
| ------------------------------- | ---------------------------------------------------------------------------------- |
| Exception in the job            | The task's `except Exception` (unchanged)                                          |
| `JOB_TIMEOUT_SEC` exceeded      | The task's `except CancelledError` → `TIMEOUT_MESSAGE`                             |
| Queue full at submit            | The route → `BUSY_MESSAGE` (companion spec)                                        |
| Waited too long for a slot      | The runner's `on_failure` → `BUSY_MESSAGE` (companion spec)                        |
| Redeploy / SIGTERM, job queued  | `JobRunner.close()` cancels it at once → `on_failure` → `SWEEP_MESSAGE`            |
| Redeploy / SIGTERM, job running | `JobRunner.close()` waits `JOB_SHUTDOWN_GRACE_SEC`, then cancels → `SWEEP_MESSAGE` |
| OOM kill / SIGKILL / host crash | No handler runs. Covered by the two checks below                                   |

For the last case:

1. **Startup sweep.** A hard-killed container always restarts (Railway
   `restartPolicyType: ON_FAILURE`), so running `sweep_stuck_jobs()` once at
   boot catches every row the dead process left behind. It's safe while an
   old container is still draining during a deploy overlap: it only touches
   rows older than `STUCK_JOB_THRESHOLD_SEC` (300 s), and a live job never
   exceeds `JOB_TIMEOUT_SEC` (240 s) from submit (§4.2).
2. **Read-path check.** `ReviewService._fail_if_stale` already fails a stale
   `processing` review when it's polled. **Add the same check to
   `TrainingService.get_plan_by_id` / `get_plan_by_review_id`.** Training
   plans currently rely on the worker cron alone, so without this a
   hard-killed plan would wait for the next restart. Factor the age logic into
   one helper both services use.

Keep the invariant test (`STUCK_JOB_THRESHOLD_SEC > JOB_TIMEOUT_SEC`), and
update the field description to drop "+ worst-case queue wait", since queue
wait now counts against the timeout.

### 4.6 Serverless interplay

- **Nothing runs on a timer.** No cron, no Redis, no polling loop. SQLAlchemy's
  pool only pings on checkout (`pool_pre_ping`), and idle asyncpg connections
  send nothing (OS TCP keepalive defaults to 2 h). Sentry only sends events
  and request sessions, not heartbeats. Verify this on Railway: after 15 min
  with no requests, the service should show **Sleeping** (acceptance §7).
- **No sleeping mid-job.** A running job talks to Supabase and Gemini, which is
  outbound traffic, so Railway won't put the container to sleep. A job lasts
  at most 240 s, well under the ~10 min idle window.
- **Cold start.** The first request after sleeping waits for the container to
  boot (Python imports plus the startup sweep). The frontend already polls with
  generous timeouts, and the sweep is two indexed `UPDATE`s. Keep heavy
  imports lazy (`cv2` and `google.genai` are imported only where used).

### 4.7 Remove the server-side video optimizer

Delete it in the same change (or merge a separate PR first):

- `app/core/video_transcoder.py`, `get_optimize_gate`, the transcode errors in
  `app/core/errors.py`.
- `MediaRepository.mark_optimized`, `increment_optimize_attempts`,
  `list_unoptimized_videos`, plus their fakes in `tests/fake_deps.py`.
- Settings: `VIDEO_OPTIMIZE_ENABLED`, `VIDEO_TARGET_HEIGHT`, `VIDEO_CRF`,
  `VIDEO_KEEP_AUDIO`, `VIDEO_AUDIO_BITRATE_KBPS`, `VIDEO_OPTIMIZE_GRACE_SEC`,
  `VIDEO_OPTIMIZE_BATCH`, `VIDEO_OPTIMIZE_CONCURRENCY`,
  `VIDEO_OPTIMIZE_MAX_ATTEMPTS`; and `VIDEO_OPTIMIZE_ENABLED` in `tests/conftest.py`.
- `ffmpeg` from the Dockerfile `apt-get` line. `FrameExtractor` uses the
  FFmpeg bundled in the `opencv-python-headless` wheel and doesn't need the
  system binary; confirm with `tests/test_frame_extractor.py` in the built image.
- Leave the model columns in place (Non-goals). Mark them
  `# unused since <date>; drop in a later migration`.

### 4.8 Config changes (`app/core/config.py`)

| Remove                    | Add                      | Default | Notes                                                                                                                                                               |
| ------------------------- | ------------------------ | ------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `REDIS_URL`               | —                        | —       |                                                                                                                                                                     |
| `WORKER_MAX_JOBS` (10)    | `JOBS_MAX_CONCURRENT`    | `2`     | Each review holds the raw video bytes and extracted frames in memory. The API container now pays that cost, so keep it low. 2 is plenty at 5 reviews/hour per user. |
| `WORKER_JOB_TIMEOUT_SEC`  | `JOB_TIMEOUT_SEC`        | `240`   | Same value; now includes time spent waiting for a slot.                                                                                                             |
| —                         | `JOB_SHUTDOWN_GRACE_SEC` | `25`    | How long a redeploy waits for running jobs before cancelling them.                                                                                                  |
| `STUCK_JOB_THRESHOLD_SEC` | (kept)                   | `300`   | Description updated (§4.5).                                                                                                                                         |

The companion spec adds `JOBS_MAX_QUEUED`, `JOB_MAX_WAIT_SEC`,
`GEMINI_REQUEST_TIMEOUT_SEC`, `GEMINI_MIN_ATTEMPT_SEC` and `GEMINI_DEADLINE_MARGIN_SEC`
(companion §4.6), with validators tying them to `JOB_TIMEOUT_SEC`.

`Settings` uses `extra="ignore"`, so stale variables left in Railway won't break
boot, but remove them anyway (§6).

**Railway drain window.** Railway sends SIGTERM and then SIGKILL after its
deployment draining period. Set `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` to at
least `JOB_SHUTDOWN_GRACE_SEC + 5`, otherwise `close()` gets killed mid-drain
and the startup sweep or read-path check has to clean up instead (still
correct, just slower for the user). Check the variable name and default in
Railway's docs when doing this.

### 4.9 Process model

`uvicorn --workers 1` stays (already set in `railway.json`). Also change the
Dockerfile `prod` CMD from `--workers 2` to `--workers 1` so local and prod
match. With N uvicorn workers each would have its own runner. That would still
be correct, but it multiplies the idle RAM this spec is trying to cut.

## 5. File-by-file change list

| File                                                                                                | Change                                                                                                                                         |
| --------------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------- |
| `app/core/jobs.py`                                                                                  | **New.** `JobRunner`, `JobRunnerClosedError`.                                                                                                  |
| `app/jobs.py`                                                                                       | **New** (from `app/worker.py`). Task functions + `sweep_stuck_jobs`, no arq.                                                                   |
| `app/worker.py`                                                                                     | **Delete.**                                                                                                                                    |
| `app/core/deps.py`                                                                                  | `get_arq_pool`/`close_arq_pool` → `get_job_runner`.                                                                                            |
| `app/api/reviews.py`, `app/api/ai.py`                                                               | Inject runner; `runner.submit(...)` at 4 call sites.                                                                                           |
| `app/main.py`                                                                                       | Lifespan builds runner, runs startup sweep, drains on shutdown.                                                                                |
| `app/services/ai.py`                                                                                | Stale check for training plans; shared helper; update docstrings that mention "arq worker" / "worker's sweeper".                               |
| `app/core/config.py`                                                                                | §4.8.                                                                                                                                          |
| `app/core/video_transcoder.py`, optimizer bits in `app/repositories/media.py`, `app/core/errors.py` | **Delete** (§4.7).                                                                                                                             |
| `pyproject.toml` + `uv.lock`                                                                        | Remove `arq`, `redis`; run `uv lock`; commit both.                                                                                             |
| `Dockerfile`                                                                                        | Drop `ffmpeg`; prod CMD `--workers 1`.                                                                                                         |
| `docker-compose.yml`                                                                                | Remove `redis` and `worker` services and `REDIS_URL`.                                                                                          |
| `railway.worker.json`                                                                               | **Delete.**                                                                                                                                    |
| `.env.example`                                                                                      | Remove Redis/worker/optimizer vars; add `JOBS_*`.                                                                                              |
| `docs/DEPLOY.md`                                                                                    | Remove §6 (Redis), §8 (Worker), §12 (optimization); update §1 table, §4 order, §13 env vars, §14 rollback; add Serverless + drain-window note. |
| `README.md`                                                                                         | Remove worker/Redis mentions, if any.                                                                                                          |

## 6. Rollout

1. **Turn on Serverless for the API now**, independent of this change.
   Expect ~40 % savings right away.
2. Merge the code. CI runs `uv lock --check` and the full suite.
3. **Deploy the API.** It now runs jobs itself. The old worker is still up and
   finishes anything already sitting in Redis. Nothing new goes to Redis, so
   its queue empties within one `JOB_TIMEOUT_SEC`.
4. Smoke test: create a review, poll it to `completed`; generate a training plan.
5. Wait ≥ 5 min, then **remove the worker service** in Railway.
6. **Keep Redis for ~1 week** as a rollback path (it costs cents once nothing
   polls it), then delete it and the `REDIS_URL` shared variable.
7. Remove stale variables from the API service: `REDIS_URL`, `WORKER_*`,
   `VIDEO_*OPTIMIZE*`, etc. Set `RAILWAY_DEPLOYMENT_DRAINING_SECONDS` (§4.8).
8. After 15 min with no traffic, confirm the API shows **Sleeping**.

**Rollback:** redeploy the previous API build and re-add the worker service from
`railway.worker.json` at the previous commit. This only works while Redis
still exists (step 6).

## 7. Acceptance criteria

**Behaviour**

- [ ] `POST /reviews/` returns 202 with `status: processing`; polling reaches
      `completed` with no worker or Redis running.
- [ ] Same for `POST /training-plans/` and both `/retry` endpoints.
- [ ] A job that exceeds `JOB_TIMEOUT_SEC` ends `failed` with `TIMEOUT_MESSAGE`.
- [ ] A job still running when the app shuts down ends `failed` with
      "Processing was interrupted." within `JOB_SHUTDOWN_GRACE_SEC`.
- [ ] A job still **waiting for a slot** when the app shuts down, or when its wait
      times out, is marked `failed` by the runner without the sweep.
- [ ] The companion spec's acceptance criteria (queue limit, wait vs run
      timeouts, Gemini deadline, `job.finished` logs) pass too.
- [ ] After a simulated hard kill (row left `processing`, older than the
      threshold), the next startup marks it `failed`.
- [ ] Polling a stale `processing` **training plan** marks it `failed`
      (new; reviews already do this).
- [ ] `GET /health/live` stays fast (< 100 ms) while a review is running.

**Code**

- [ ] `grep -rniE "arq|redis" app tests` finds nothing.
- [ ] `app/worker.py`, `railway.worker.json`, `app/core/video_transcoder.py` are gone.
- [ ] Coverage gate (`fail_under`) still passes.

**Cost**

- [ ] Railway project has one app service and no Redis.
- [ ] The API shows **Sleeping** after 15 min idle.
- [ ] The next billing period's idle-day RAM cost is ≈ $0.

## 8. Test plan

**New: `tests/test_jobs.py`** (replaces the arq parts of `test_worker_tasks.py`)

- `submit` returns immediately; the job runs; the task leaves `_tasks` when done.
- No more than `max_concurrent` jobs run at once (use `asyncio.Event` gates).
- A job past `timeout_sec`, counting time spent waiting for a slot, gets
  `CancelledError` inside and `TIMEOUT_MESSAGE` is recorded.
- `close()` waits for a job that finishes within the grace period; cancels one
  that doesn't and records `SWEEP_MESSAGE`; `submit` after `close()` raises
  `JobRunnerClosedError`.
- An exception escaping a job is logged and doesn't affect other jobs.

**Moved, mostly unchanged:** friendly-message tests, review/plan task tests
(success, failure, cancellation, DB session closed), sweeper SQL tests. Drop the
`ctx` argument.

**Endpoint tests** (`test_reviews.py`, `test_training.py`, `test_retry_endpoints.py`,
`test_rate_limit.py`): replace `FakeArqPool` / `get_arq_pool` overrides with
`FakeJobRunner` / `get_job_runner`. `FakeJobRunner.submit` records
`(fn.__name__, args)`; `FakeJobRunner(raise_exc=...)` replaces the "Redis down"
cases with "runner closed".

**Contract** (`test_repo_contracts.py`): replace the `FakeArqPool.enqueue_job`
shape test with `FakeJobRunner.submit` vs `JobRunner.submit` (sync, same
signature). Remove the deleted `MediaRepository` methods from the media contract.

**Lifespan:** startup runs the sweep once; a sweep that raises doesn't stop
startup; shutdown calls `close()` before `engine.dispose()`.

**Training stale-read:** fresh `processing` plan is untouched; stale one is
failed with `STUCK_PROCESSING_MESSAGE`.

**Delete:** `tests/test_video_optimization.py`, `tests/test_video_transcoder.py`,
optimizer cases in `test_worker_tasks.py` and `test_repositories_integration.py`,
arq pool lifecycle tests in `test_auth_jwt_deps.py`,
`test_every_task_the_api_enqueues_is_registered`,
`test_automatic_retries_stay_disabled`, `test_the_optimize_sweep_does_not_run_at_startup`.

## 9. Risks and trade-offs

| Risk                                                                           | Mitigation                                                                                                                                                                 |
| ------------------------------------------------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A review's memory spike now hits the API container; an OOM takes HTTP down too | `JOBS_MAX_CONCURRENT=2`; client-side compression keeps videos small; Railway restarts the container and the startup sweep fails the orphaned rows. Watch RAM after launch. |
| Blocking code added to a job later stalls HTTP                                 | Rule in §4.3 plus the `/health/live` latency acceptance check.                                                                                                             |
| A burst of reviews: jobs at the back wait out their whole budget and fail as "took too long" | Companion spec: queue limit with an immediate "busy" response, and a separate wait cap. |
| A hung Gemini call keeps its thread (and frames) alive in the API after the job times out | Companion spec: per-attempt HTTP timeout plus a deadline-aware retry loop.  |
| Redeploy interrupts running jobs                                               | Rare at this traffic; ends `failed` with a retry, same as a worker restart today.                                                                                          |
| Cold start after sleep slows the first request                                 | Acceptable trade for ≈ $0 idle; lazy heavy imports.                                                                                                                        |
| Traffic grows past one process                                                 | Bring back a queue then. The task functions stay plain `async def fn(id: str)`, so re-wrapping them for arq (or similar) is mechanical.                                    |

## 10. Open questions

1. Is a ~25 s shutdown grace enough, or should deploys wait for the full
   240 s? (Longer drains slow every deploy for a rare edge case.)
2. Should `JOBS_MAX_CONCURRENT` scale with the container's RAM limit instead of
   being a fixed setting? A fixed setting is simpler; revisit if RAM is resized.
   The companion spec's `job.finished` metrics are the input for that decision.
