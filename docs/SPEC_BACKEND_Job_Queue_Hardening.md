# SPEC — Job Queue Hardening (Admission Control, Wait vs Run Timeouts, Gemini Deadlines)

**Status:** Implemented (2026-10-29)
**Owner:** Backend
**Date:** 2026-10-29
**Parent spec:** `SPEC_BACKEND_Merge_Worker_Into_API.md`, which already points back
here and has been updated for the `on_failure` hook and queue-limit route handling
**Depends on:** the in-process `JobRunner` from the parent spec. Implement the two
together. Where they disagree, this spec wins.
**Unchanged:** the HTTP contract (202 + poll), the `status` lifecycle, the frontend.

---

## 1. Problem

The parent spec moves review and training-plan jobs into the API process behind a
`JobRunner` that runs `JOBS_MAX_CONCURRENT` (2) jobs at once, with one
`JOB_TIMEOUT_SEC` (240 s) budget that covers waiting for a slot plus running. That
design has four gaps.

### 1.1 Bursts fail late, with the wrong message

Every `POST /reviews/` is accepted and queued without limit. Only 2 run at once, and
the timeout includes the wait, so in a burst the jobs at the back use up their whole
budget waiting. Each one ends `failed` with *"Processing took too long and was
stopped."* four minutes later, even though nothing went wrong except load. The user
waits the full 4 minutes to find out, and the message points at the wrong cause.

Back-of-envelope: with a typical review taking `R` seconds, about `2 × 240 / R` jobs
arriving together can all finish. If `R ≈ 60 s`, that's 8, and the 9th fails. (`R`
isn't measured yet; see §4.5.)

### 1.2 A job that never starts is never marked failed

In the parent design the task function (`process_review_task`) owns failure
handling: its `except CancelledError` marks the row. But a job cancelled **while
waiting for a slot** has not entered the function yet, so nothing marks it:

- timeout fires during the semaphore wait → row stays `processing`
- `JobRunner.close()` on redeploy cancels queued-but-not-started jobs → same

The startup sweep and read-path check eventually fail these rows (after
`STUCK_JOB_THRESHOLD_SEC`, with the generic "interrupted" message), so this is a slow
recovery rather than a permanent lock. It still breaks the parent spec's own
acceptance criteria (§7: timeout → `TIMEOUT_MESSAGE`; shutdown → "interrupted" within
the grace period).

### 1.3 Hung Gemini calls leave threads running

The Gemini client is built with no HTTP timeout (`genai.Client(api_key=api_key)`,
`app/services/ai.py:235`), and every call runs in `asyncio.to_thread`. When the job
timeout fires, the coroutine is cancelled and the row is marked failed correctly, but
**the thread can't be cancelled**. A hung `generate_content` keeps running and holds
the frames it was sent. The retry loop (`_generate_content_with_retry`, up to
`GEMINI_MAX_ATTEMPTS` = 3) and its `time.sleep` backoff also carry on after the job is
dead.

With a separate worker this was isolated. After the merge, those threads sit in the
API process: they use up slots in the default thread pool (shared with storage
downloads, frame extraction and any `to_thread` call on the HTTP path) and hold memory
in the container that serves HTTP.

### 1.4 No data to tune with

Nobody knows `R` (typical run time), how long jobs wait, or how often each timeout
fires. `JOBS_MAX_CONCURRENT`, the queue size and the timeouts are guesses.

## 2. Goals

1. **Fail fast when busy.** When the queue is full, the user learns within the POST
   response, not 4 minutes later.
2. **Truthful messages.** "Busy, try again" when the cause is load; "took too long"
   only when the job itself ran too long.
3. **Every submitted job ends in a terminal state through its own path**, whether it
   is rejected, times out waiting, times out running, or is cancelled on shutdown.
   The sweep and read-path checks remain a backstop, not the normal route.
4. **No thread outlives its job by more than a few seconds.** Gemini work stops at
   the job's deadline.
5. **Measured, tunable limits.** Every job logs wait time, run time and outcome.
6. **No contract change.** The frontend already shows `failed` rows with a Retry
   button; that's how "busy" reaches the user.

## 3. Non-goals

- Durable or cross-process queueing (same as the parent spec).
- Returning `503`/`429` with `Retry-After` from `POST /reviews/`. That's arguably
  cleaner, but it changes the contract and needs frontend work. Revisit if users find
  "failed: busy" confusing (§9).
- Refunding the rate-limit hit for a rejected submit (§9).
- Timeouts on Supabase Storage calls. `storage.download` already uses the supabase
  client's httpx timeout. Verify the effective value while implementing, but it's out
  of scope here.

## 4. Design

### 4.1 Job lifecycle and outcomes

```
submit ──► [queue full?] ──yes──► REJECTED_BUSY       (no task created)
              │ no
              ▼
          WAITING ──(JOB_MAX_WAIT_SEC elapsed / shutdown)──► WAIT_TIMEOUT / CANCELLED_WAITING
              │ slot acquired
              ▼
          RUNNING ──(deadline / shutdown / exception)──► RUN_TIMEOUT / CANCELLED_RUNNING / FAILED
              │
              ▼
          SUCCEEDED
```

| Outcome | Who marks the row | Message (user-facing) |
|---|---|---|
| `rejected_busy` | Route, synchronously | `BUSY_MESSAGE` |
| `wait_timeout` | Runner, via `on_failure` | `BUSY_MESSAGE` |
| `cancelled_waiting` (shutdown) | Runner, via `on_failure` | `SWEEP_MESSAGE` ("Processing was interrupted.") |
| `run_timeout` | Task function (`except CancelledError`) | `TIMEOUT_MESSAGE` |
| `cancelled_running` (shutdown) | Task function | `SWEEP_MESSAGE` |
| `failed` (exception) | Task function | friendly message (`ERROR_MESSAGES`) |
| `succeeded` | Service (`mark_completed`) | — |

New constant, next to the others in `app/jobs.py`:

```python
BUSY_MESSAGE = "We're processing a lot of reviews right now. Please try again in a few minutes."
```

The training-plan path uses the same text; "reviews" reads fine there, or add a
second constant if copy review prefers.

### 4.2 `JobRunner` changes — `app/core/jobs.py`

```python
class JobRunnerBusyError(Exception):
    """The wait queue is full; the job was not accepted."""


class JobRunner:
    def __init__(
        self,
        max_concurrent: int,
        max_queued: int,          # NEW — JOBS_MAX_QUEUED
        max_wait_sec: float,      # NEW — JOB_MAX_WAIT_SEC
        timeout_sec: float,       # JOB_TIMEOUT_SEC: total budget from submit
        shutdown_grace_sec: float,
    ): ...

    def submit(
        self,
        fn: Callable[[str], Awaitable[None]],
        job_id: str,
        *,
        on_failure: Callable[[str, str], Awaitable[None]],  # NEW — (job_id, message)
    ) -> None:
        """Queue fn(job_id). Raises JobRunnerBusyError or JobRunnerClosedError."""
```

**Admission control (fixes §1.1).** `submit` counts jobs that are waiting (submitted,
no slot yet). If `waiting >= max_queued`, raise `JobRunnerBusyError` **before**
creating a task. The check and the increment happen synchronously inside `submit`
with no `await` in between, so two requests on the same event loop can't both slip
past the limit. Running jobs don't count toward `max_queued`.

**Two-phase timeouts (fixes §1.1, §1.2).** The wrapper the runner schedules:

```python
async def _run(self, fn, job_id, on_failure, submitted_at):
    deadline = submitted_at + self._timeout_sec          # loop.time() based
    # Phase 1 — wait for a slot, capped by max_wait_sec.
    try:
        async with asyncio.timeout(self._max_wait_sec):
            await self._slots.acquire()
    except (TimeoutError, asyncio.CancelledError) as exc:
        self._waiting -= 1
        shutting_down = isinstance(exc, asyncio.CancelledError)
        msg = SWEEP_MESSAGE if shutting_down else BUSY_MESSAGE
        await asyncio.shield(on_failure(job_id, msg))
        self._log(job_id, "cancelled_waiting" if shutting_down else "wait_timeout", ...)
        if shutting_down:
            raise
        return
    self._waiting -= 1
    # Phase 2 — run with whatever budget remains.
    token = JOB_DEADLINE.set(deadline)                   # §4.3
    try:
        async with asyncio.timeout_at(deadline):
            await fn(job_id)                             # task fn handles its own failures
    finally:
        JOB_DEADLINE.reset(token)
        self._slots.release()
        self._log(job_id, outcome, wait_sec=..., run_sec=...)
```

- **Total budget is still `JOB_TIMEOUT_SEC` from submit**, so the parent invariant
  `STUCK_JOB_THRESHOLD_SEC > JOB_TIMEOUT_SEC` still holds.
- **The wait is capped separately** by `JOB_MAX_WAIT_SEC`, so a job that does get a
  slot always has at least `JOB_TIMEOUT_SEC − JOB_MAX_WAIT_SEC` to run. A job that
  waits a long time fails as "busy" instead of starting with too little time and then
  failing as "took too long".
- **`on_failure` is the runner's hook for jobs that never started.** Once `fn` is
  running, `fn`'s own `except CancelledError` / `except Exception` handle failure
  exactly as in the parent spec, and the runner doesn't call `on_failure` (no
  double-marking).
- `close()` cancels both waiting and running tasks after the grace period. Waiting
  ones go through phase 1's `CancelledError` branch, so they get `SWEEP_MESSAGE` right
  away instead of waiting for the next startup sweep.

`on_failure` targets are the existing helpers, renamed to drop the leading underscore
since routes now pass them in: `mark_review_failed(review_id, message)`,
`mark_plan_failed(plan_id, message)`. They open their own `SessionLocal()` as today.

### 4.3 Gemini deadline and per-attempt timeout (fixes §1.3)

Two parts, both in `app/services/ai.py`.

**(a) Per-attempt HTTP timeout.** Each `generate_content` call gets an explicit
timeout through the per-request config:

```python
config = types.GenerateContentConfig(
    **config_kwargs,
    http_options=types.HttpOptions(timeout=attempt_timeout_ms),  # milliseconds
)
```

`google-genai` is pinned at 2.27.0 in `uv.lock`. Confirm `GenerateContentConfig`
accepts `http_options` in that version. If it doesn't, set
`HttpOptions(timeout=...)` on the cached `genai.Client` instead, and use only the
fixed per-attempt value without the deadline clamp in (b).

The timeout is passed to httpx, which applies it to each phase (connect / read /
write), not as a total. For a non-streaming `generate_content` the server sends
nothing until the answer is ready, so the read timeout is effectively the wait for
the response. That's good enough; no extra wall-clock guard is needed.

**(b) Deadline-aware retry loop.** The runner publishes the job's absolute deadline
in a `ContextVar`:

```python
# app/core/jobs.py
JOB_DEADLINE: ContextVar[float | None] = ContextVar("job_deadline", default=None)
```

`asyncio.to_thread` copies the current context into the worker thread, so
`_generate_content_with_retry` can read `JOB_DEADLINE.get()` without passing anything
through `ReviewService` / `GeminiService` signatures. Use `time.monotonic()` both for
the deadline and in the thread. `loop.time()` is monotonic too, but don't mix the two
clocks: compute the deadline with `time.monotonic()` in `_run`.

Loop rules, per attempt:

```python
remaining = deadline - time.monotonic() - GEMINI_DEADLINE_MARGIN_SEC   # None deadline → ∞
if remaining < GEMINI_MIN_ATTEMPT_SEC:
    raise <last error> or AIGenerationFailedError   # don't start an attempt that can't finish
attempt_timeout = min(GEMINI_REQUEST_TIMEOUT_SEC, remaining)
# … call with attempt_timeout …
# before backoff sleep:
if delay >= deadline - time.monotonic() - GEMINI_MIN_ATTEMPT_SEC:
    raise                                            # no time left for another attempt
time.sleep(delay)
```

What this guarantees:

- A Gemini thread never runs past `deadline − GEMINI_DEADLINE_MARGIN_SEC + (one
  httpx phase of slack)`. It stops on its own seconds after the job's coroutine is
  cancelled, instead of minutes later.
- Retries stop when there's no time left to finish one, and backoff never sleeps past
  the deadline.
- **Outside a job** (`JOB_DEADLINE` unset), e.g. upload-time content moderation on
  the HTTP path, only the fixed `GEMINI_REQUEST_TIMEOUT_SEC` applies. That's still a
  strict improvement over no timeout.
- The two-pass review flow (media call, then refine call) shares one deadline, so the
  refine call automatically gets whatever the media call left.

A timeout raised by httpx (`ReadTimeout`, possibly wrapped) is already in
`_RETRYABLE_EXC_NAMES`, so a slow attempt is retried while budget remains. That's the
intended behaviour.

### 4.4 Route changes — `app/api/reviews.py`, `app/api/ai.py`

Building on the parent spec's guard, four call sites:

```python
review = await service.enqueue_review(payload.session_id, user)
try:
    runner.submit(process_review_task, str(review.id), on_failure=mark_review_failed)
except JobRunnerBusyError:
    logger.warning("Job queue full; review rejected", review_id=str(review.id))
    review = await service.review_repo.mark_failed(review.id, BUSY_MESSAGE)
except Exception:  # JobRunnerClosedError during shutdown
    logger.exception("Failed to enqueue review %s", review.id)
    review = await service.review_repo.mark_failed(review.id, ENQUEUE_FAILED_MESSAGE)
return ReviewOut.model_validate(review)
```

The response is still `202` with the row; `status` is `failed` and `error_message`
is `BUSY_MESSAGE`. The frontend renders its existing failed state with Retry.
`/retry` goes through the same code, so a retry during a burst is also rejected
quickly rather than queued behind everyone.

Why create the row and then fail it, instead of checking capacity before inserting:
it keeps one code path, an existing contract, and a visible record of the rejection.
A pre-check would also race: capacity can change between the check and `submit`.

### 4.5 Observability (fixes §1.4)

The runner logs exactly one structlog event per job when it reaches a terminal
state:

```
event="job.finished" job=process_review_task job_id=<uuid>
      outcome=succeeded|failed|run_timeout|wait_timeout|rejected_busy|cancelled_waiting|cancelled_running
      wait_sec=12.4 run_sec=48.1 queued_at_submit=1 running_at_submit=2
```

`rejected_busy` is logged by `submit` itself, since no task exists. `outcome=failed`
covers exceptions that `fn` catches and records on the row. Because `fn` swallows
those, the wrapper can't see them, so task functions return `True` on success and
`False` when they marked the row failed, and the wrapper logs from that. Keep the
existing per-task `logger.info` / `logger.exception` lines as well.

Also add a `Sentry` breadcrumb or tag for `rejected_busy` and `wait_timeout` so a
burst shows up without searching logs (one line; uses the existing Sentry setup).

How to tune after a week of traffic: take p95 of `run_sec` as `R`. Choose
`JOBS_MAX_CONCURRENT` from RAM headroom (peak container RAM ÷ peak per-job RAM, minus
the idle baseline), then `JOBS_MAX_QUEUED ≈ JOBS_MAX_CONCURRENT × JOB_MAX_WAIT_SEC / R`.

### 4.6 Config — `app/core/config.py`

| Setting | Default | Notes |
|---|---|---|
| `JOBS_MAX_QUEUED` | `4` | Jobs allowed to wait for a slot. Beyond this, submit is rejected. With `JOBS_MAX_CONCURRENT=2` and `R≈30 s`, 4 queued jobs start within ~60 s. |
| `JOB_MAX_WAIT_SEC` | `60` | Longest a job may wait for a slot. Must be `< JOB_TIMEOUT_SEC`. |
| `GEMINI_REQUEST_TIMEOUT_SEC` | `90` | Per-attempt cap. A vision call with frames plus thinking is usually well under this. |
| `GEMINI_MIN_ATTEMPT_SEC` | `10` | Don't start (or retry into) an attempt with less budget than this. |
| `GEMINI_DEADLINE_MARGIN_SEC` | `5` | Stop Gemini work this long before the job deadline, so the thread is done around the time the coroutine is cancelled. |

Validators (`model_validator`, next to `_photo_bounds_are_satisfiable`):

- `JOB_MAX_WAIT_SEC < JOB_TIMEOUT_SEC`
- `JOB_TIMEOUT_SEC − JOB_MAX_WAIT_SEC ≥ GEMINI_MIN_ATTEMPT_SEC + GEMINI_DEADLINE_MARGIN_SEC`
  (a job that starts at the last moment can still make one Gemini attempt)
- `GEMINI_MIN_ATTEMPT_SEC ≤ GEMINI_REQUEST_TIMEOUT_SEC`

Add the new variables to `.env.example` and `docs/DEPLOY.md` §13.

### 4.7 DB pool interplay (this branch)

`fix/pool-supabase-limit` sets `DB_POOL_SIZE=5` and makes jobs release their
connection during download and Gemini (`release_connection()`). After the merge
there's one process instead of two, so the 5 connections cover HTTP **and**
`JOBS_MAX_CONCURRENT` jobs. Jobs only hold a connection at the start and end, so 2
concurrent jobs fit easily. If `JOBS_MAX_CONCURRENT` is raised later, keep
`JOBS_MAX_CONCURRENT + expected concurrent HTTP DB use ≤ DB_POOL_SIZE`, and remember
the worker's former share of the Supavisor cap is now free.

## 5. File-by-file change list

| File | Change |
|---|---|
| `app/core/jobs.py` | `JobRunnerBusyError`; `max_queued`, `max_wait_sec`; waiting counter; two-phase `_run`; `on_failure`; `JOB_DEADLINE` ContextVar; `job.finished` logging. |
| `app/jobs.py` | `BUSY_MESSAGE`; public `mark_review_failed` / `mark_plan_failed`; task functions return success bool. |
| `app/api/reviews.py`, `app/api/ai.py` | Pass `on_failure`; handle `JobRunnerBusyError` (4 call sites). |
| `app/main.py` | Pass the new settings to `JobRunner`. |
| `app/services/ai.py` | Deadline-aware `_generate_content_with_retry`; per-attempt `http_options` timeout. |
| `app/core/config.py` | §4.6 settings + validators. |
| `tests/fake_deps.py` | `FakeJobRunner.submit` accepts `on_failure`; `FakeJobRunner(busy=True)` raises `JobRunnerBusyError`. |
| `.env.example`, `docs/DEPLOY.md` | New variables; watch-list entry for `rejected_busy` / `wait_timeout`. |

## 6. Rollout

Ships with (or immediately after) the parent merge, same deploy steps. No migration,
no Railway variables required (defaults are safe). After a week, read the
`job.finished` logs and tune per §4.5.

## 7. Acceptance criteria

**Admission**
- [ ] With `JOBS_MAX_CONCURRENT=2` and `JOBS_MAX_QUEUED=4`, the 7th simultaneous
      `POST /reviews/` returns `202` with `status: failed` and `BUSY_MESSAGE` in
      under 1 s; the first 6 are `processing`.
- [ ] `POST /reviews/{id}/retry` when the queue is full behaves the same way.

**Timeouts**
- [ ] A job that can't get a slot within `JOB_MAX_WAIT_SEC` ends `failed` with
      `BUSY_MESSAGE`, without ever calling the task function.
- [ ] A job whose run passes the deadline ends `failed` with `TIMEOUT_MESSAGE`.
- [ ] On shutdown, queued jobs end `failed` with `SWEEP_MESSAGE` immediately, and
      running jobs do so after the grace period. No row needs the startup sweep in
      either case.

**Gemini**
- [ ] With a stubbed client that hangs, the Gemini thread returns within
      `GEMINI_DEADLINE_MARGIN_SEC + 5 s` of the job deadline.
- [ ] No retry or backoff sleep starts when less than `GEMINI_MIN_ATTEMPT_SEC`
      remains.
- [ ] Outside a job (no deadline), calls still time out at `GEMINI_REQUEST_TIMEOUT_SEC`.

**Observability**
- [ ] Every submitted job produces exactly one `job.finished` event with `outcome`,
      `wait_sec`, `run_sec`.

## 8. Test plan

**`tests/test_jobs.py`** (extends the parent spec's suite; use `asyncio.Event` gates
and small timeouts, no real sleeps longer than ~0.2 s):

- Queue cap: with the slots full and `max_queued` waiting, the next `submit` raises
  `JobRunnerBusyError` and creates no task. After one job finishes, `submit` works
  again.
- The counter stays correct across every exit path: wait timeout, shutdown while
  waiting, run success, run failure, run timeout (assert `waiting == 0` after each).
- Wait timeout: `on_failure(job_id, BUSY_MESSAGE)` called once; `fn` never called.
- Shutdown while waiting: `on_failure(job_id, SWEEP_MESSAGE)` called once.
- Run-phase failure: `on_failure` **not** called (the task function owns it).
- `JOB_DEADLINE` is set inside `fn` and visible from a function run via
  `asyncio.to_thread`; it's reset after the job.
- `job.finished` emitted once per job with the right `outcome` (use structlog's
  `capture_logs`).

**`tests/test_gemini_service.py`:**
- `_generate_content_with_retry` with `JOB_DEADLINE` set: clamps the per-attempt
  timeout to the remaining budget; refuses to start an attempt below
  `GEMINI_MIN_ATTEMPT_SEC`; skips a backoff sleep that would cross the deadline.
- Without a deadline: uses `GEMINI_REQUEST_TIMEOUT_SEC`.
- The timeout reaches the client: assert the `config.http_options.timeout` passed to
  the fake client's `generate_content` (milliseconds).

**Endpoint tests** (`test_reviews.py`, `test_training.py`, `test_retry_endpoints.py`):
- `FakeJobRunner(busy=True)` → 202, `status: failed`, `BUSY_MESSAGE`, no job recorded.
- The happy path records `on_failure` as the matching `mark_*_failed` helper.

**Contract** (`test_repo_contracts.py`): `FakeJobRunner.submit` signature matches
`JobRunner.submit`, including the keyword-only `on_failure`.

**Config:** each validator in §4.6 rejects a bad combination.

## 9. Risks and trade-offs

| Risk | Mitigation |
|---|---|
| "Failed: busy" on a fresh request looks odd in the UI | Truthful copy plus the existing Retry button. If it confuses users, move to `503 + Retry-After` (needs a frontend change; see Non-goals). |
| A rejected submit still uses up one of the user's `RATE_LIMIT_AI` (5/hour) requests | Rejections only happen during bursts and the copy says to wait a few minutes. slowapi has no refund; revisit only if logs show users hitting both limits. |
| Defaults are guesses until `R` is measured | §4.5 logging plus a one-week tuning pass; the validators keep any tuned combination consistent. |
| httpx timeout is per phase, not total | Fine for non-streaming calls (§4.3a). If streaming is added later, add a wall-clock check in the loop. |
| `http_options` per request not supported in the pinned SDK | Fallback: client-level `HttpOptions(timeout=...)` (§4.3a). The deadline clamp on retries and backoff still works. |

## 10. Open questions

1. Should training plans share the queue with reviews, or get a separate slot? Plans
   are text-only and fast, so one could squeeze in while two reviews run. Left out for
   simplicity; reconsider if `wait_sec` for plans is high.
2. Should `BUSY_MESSAGE` give a concrete wait ("in about a minute") based on queue
   depth? Nice to have; needs `R` first.
