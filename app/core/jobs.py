"""In-process job runner — the replacement for the external queue worker.

Background work (reviews, training plans) runs inside the API process now, so
there is no separate worker service and no queue broker to keep awake. See
``docs/SPEC_BACKEND_Merge_Worker_Into_API.md`` and
``docs/SPEC_BACKEND_Job_Queue_Hardening.md``.

The runner owns every background task so it can cap concurrency, reject a burst
up front, bound both the wait for a slot and the run itself, and drain on
shutdown. Jobs share the HTTP event loop: anything blocking or CPU-bound inside
a job must go through ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from contextvars import ContextVar

import sentry_sdk
import structlog

logger = structlog.get_logger(__name__)

# Messages for the two outcomes the runner itself records. The task functions in
# app/jobs.py re-export them next to TIMEOUT_MESSAGE and ERROR_MESSAGES; they
# live here because the runner is what marks a job that never started.
SWEEP_MESSAGE = "Processing was interrupted."
BUSY_MESSAGE = "We're processing a lot of reviews right now. Please try again in a few minutes."

# After cancelling a job on shutdown, how long to let its own bookkeeping (the
# shielded mark-failed write) finish. Railway's draining period must cover
# JOB_SHUTDOWN_GRACE_SEC plus this — see the spec's drain-window note.
CANCEL_DRAIN_SEC = 5.0

# Absolute deadline (``time.monotonic()``) of the job running in this context, or
# None outside a job. ``asyncio.to_thread`` copies the context into the worker
# thread, so blocking code — the Gemini retry loop — can read it and stop on its
# own instead of outliving the job that started it.
JOB_DEADLINE: ContextVar[float | None] = ContextVar("job_deadline", default=None)

# The runner that owns the current job, so a task function can tell a shutdown
# cancellation ("interrupted") from a timeout ("took too long"). None outside a
# job, which reads as "not shutting down".
CURRENT_RUNNER: ContextVar[JobRunner | None] = ContextVar("current_job_runner", default=None)

#: ``fn(job_id)``. Returns True on success, False when it recorded its own failure.
JobFn = Callable[[str], Awaitable[bool | None]]
#: ``on_failure(job_id, message)`` — used only for jobs that never entered ``fn``.
FailureHook = Callable[[str, str], Awaitable[None]]


class JobRunnerClosedError(RuntimeError):
    """The runner is shutting down and no longer accepts work."""


class JobRunnerBusyError(RuntimeError):
    """The wait queue is full; the job was not accepted."""


def shutting_down() -> bool:
    """True when the runner that owns the current job is draining for shutdown."""
    runner = CURRENT_RUNNER.get()
    return runner is not None and runner.shutting_down


class JobRunner:
    """Runs ``fn(job_id)`` coroutines in the background with admission control.

    A submitted job always reaches a terminal state through its own path:
    rejected at submit, failed by ``on_failure`` while it was still waiting for a
    slot, or — once running — failed by ``fn``'s own exception handling. The
    startup sweep and the read-path staleness check stay as a backstop for a hard
    kill, not as the normal route.
    """

    def __init__(
        self,
        max_concurrent: int,
        max_queued: int,
        max_wait_sec: float,
        timeout_sec: float,
        shutdown_grace_sec: float,
    ) -> None:
        self._slots = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._max_queued = max_queued
        self._max_wait_sec = max_wait_sec
        self._timeout_sec = timeout_sec
        self._shutdown_grace_sec = shutdown_grace_sec
        # Strong references: a bare create_task can be garbage-collected mid-run.
        self._tasks: set[asyncio.Task] = set()
        self._running_tasks: set[asyncio.Task] = set()
        self._waiting = 0
        self._closed = False

    @property
    def shutting_down(self) -> bool:
        return self._closed

    @property
    def waiting(self) -> int:
        """Jobs submitted that have not acquired a slot yet."""
        return self._waiting

    @property
    def running(self) -> int:
        return len(self._running_tasks)

    def submit(
        self,
        fn: JobFn,
        job_id: str,
        *,
        on_failure: FailureHook,
    ) -> None:
        """Schedule ``fn(job_id)`` and return immediately.

        Raises ``JobRunnerBusyError`` when the wait queue is full and
        ``JobRunnerClosedError`` once ``close()`` has started. The queue check and
        the increment happen with no ``await`` between them, so two requests on
        the same event loop cannot both slip past the limit.
        """
        if self._closed:
            raise JobRunnerClosedError("Job runner is shutting down.")
        if self._waiting >= self._max_queued:
            self._log_finished(fn, job_id, "rejected_busy", wait_sec=0.0, run_sec=0.0)
            raise JobRunnerBusyError(
                f"{self._waiting} job(s) already waiting for a slot (max {self._max_queued})."
            )

        queued_at_submit = self._waiting
        running_at_submit = self.running
        self._waiting += 1
        task = asyncio.create_task(
            self._run(
                fn,
                job_id,
                on_failure,
                time.monotonic(),
                queued_at_submit=queued_at_submit,
                running_at_submit=running_at_submit,
            ),
            name=f"job:{fn.__name__}:{job_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run(
        self,
        fn: JobFn,
        job_id: str,
        on_failure: FailureHook,
        submitted_at: float,
        *,
        queued_at_submit: int,
        running_at_submit: int,
    ) -> None:
        deadline = submitted_at + self._timeout_sec

        # Phase 1 — wait for a slot, capped separately by max_wait_sec so a job
        # that does start always has timeout_sec - max_wait_sec left to run.
        try:
            async with asyncio.timeout(self._max_wait_sec):
                await self._slots.acquire()
        except (TimeoutError, asyncio.CancelledError) as exc:
            self._waiting -= 1
            interrupted = isinstance(exc, asyncio.CancelledError)
            # fn never ran, so its own except CancelledError cannot mark the row;
            # that is what on_failure is for.
            await self._mark_never_started(
                on_failure,
                job_id,
                SWEEP_MESSAGE if interrupted else BUSY_MESSAGE,
            )
            self._log_finished(
                fn,
                job_id,
                "cancelled_waiting" if interrupted else "wait_timeout",
                wait_sec=time.monotonic() - submitted_at,
                run_sec=0.0,
                queued_at_submit=queued_at_submit,
                running_at_submit=running_at_submit,
            )
            if interrupted:
                raise
            return

        self._waiting -= 1
        task = asyncio.current_task()
        if task is not None:
            self._running_tasks.add(task)
        started_at = time.monotonic()
        outcome = "succeeded"
        deadline_token = JOB_DEADLINE.set(deadline)
        runner_token = CURRENT_RUNNER.set(self)

        # Phase 2 — run with whatever is left of the total budget. fn owns its own
        # failure bookkeeping from here (including cancellation), so the runner
        # only observes the outcome.
        try:
            async with asyncio.timeout(max(0.0, deadline - time.monotonic())):
                succeeded = await fn(job_id)
            outcome = "succeeded" if succeeded is not False else "failed"
        except TimeoutError:
            outcome = "run_timeout"
        except asyncio.CancelledError:
            outcome = "cancelled_running"
            raise
        except Exception:
            # fn is expected to handle its own errors; anything escaping would
            # otherwise surface as "Task exception was never retrieved".
            outcome = "failed"
            logger.exception("job.escaped_exception", job=fn.__name__, job_id=job_id)
        finally:
            JOB_DEADLINE.reset(deadline_token)
            CURRENT_RUNNER.reset(runner_token)
            if task is not None:
                self._running_tasks.discard(task)
            self._slots.release()
            self._log_finished(
                fn,
                job_id,
                outcome,
                wait_sec=started_at - submitted_at,
                run_sec=time.monotonic() - started_at,
                queued_at_submit=queued_at_submit,
                running_at_submit=running_at_submit,
            )

    async def _mark_never_started(self, on_failure: FailureHook, job_id: str, message: str) -> None:
        """Record the failure of a job that never entered ``fn``.

        Shielded: on a shutdown cancellation the task is already cancelled, and an
        unshielded DB write would be cancelled mid-flight and leave the row
        'processing' until the next startup sweep.
        """
        try:
            await asyncio.shield(on_failure(job_id, message))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("job.mark_failed_failed", job_id=job_id, message=message)

    def _log_finished(
        self,
        fn: JobFn,
        job_id: str,
        outcome: str,
        *,
        wait_sec: float,
        run_sec: float,
        queued_at_submit: int | None = None,
        running_at_submit: int | None = None,
    ) -> None:
        """Exactly one event per submitted job — the input for tuning the limits."""
        logger.info(
            "job.finished",
            job=getattr(fn, "__name__", repr(fn)),
            job_id=job_id,
            outcome=outcome,
            wait_sec=round(wait_sec, 3),
            run_sec=round(run_sec, 3),
            queued_at_submit=queued_at_submit if queued_at_submit is not None else self._waiting,
            running_at_submit=running_at_submit if running_at_submit is not None else self.running,
        )
        if outcome in ("rejected_busy", "wait_timeout"):
            # Surfaces a burst in Sentry without grepping logs (no-op when Sentry
            # is not configured).
            sentry_sdk.add_breadcrumb(
                category="job",
                message=f"job {outcome}",
                level="warning",
                data={"job_id": job_id, "outcome": outcome},
            )

    async def close(self) -> None:
        """Stop accepting work, drain running jobs, then cancel what is left.

        Jobs still waiting for a slot are cancelled at once — they have done no
        work worth saving, and their ``on_failure`` marks them interrupted
        immediately instead of leaving them for the next startup sweep.
        """
        self._closed = True
        if not self._tasks:
            return
        # Let tasks created in this same loop iteration take their first step, so a
        # cancel lands inside _run — which records the failure — instead of before
        # the coroutine ever starts, which would record nothing.
        await asyncio.sleep(0)

        tasks = list(self._tasks)
        for task in tasks:
            if task not in self._running_tasks:
                task.cancel()

        running = [t for t in tasks if t in self._running_tasks]
        if running:
            await asyncio.wait(running, timeout=self._shutdown_grace_sec)
            for task in running:
                if not task.done():
                    task.cancel()

        pending = [t for t in tasks if not t.done()]
        if pending:
            # Give the cancelled jobs their shielded mark-failed write.
            await asyncio.wait(pending, timeout=CANCEL_DRAIN_SEC)
