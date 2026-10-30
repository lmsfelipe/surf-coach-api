"""The in-process job runner and the background tasks it runs.

Two layers are covered here. ``JobRunner`` (app/core/jobs.py) must admit, delay,
time out and drain jobs so that every submitted job reaches a terminal state
through its own path — never left 'processing' for the startup sweep to find. The
task functions (app/jobs.py) must record the right user-facing message for each
cause and never leak a DB session.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from structlog.testing import capture_logs

from app.core.errors import (
    AIGenerationFailedError,
    AIParseFailedError,
    InvalidMediaError,
    NoMediaForSessionError,
    StorageDownloadError,
)
from app.core.jobs import (
    JOB_DEADLINE,
    JobRunner,
    JobRunnerBusyError,
    JobRunnerClosedError,
    shutting_down,
)
from app.jobs import (
    BUSY_MESSAGE,
    ERROR_MESSAGES,
    SWEEP_MESSAGE,
    TIMEOUT_MESSAGE,
    _friendly_message,
    mark_plan_failed,
    mark_review_failed,
    process_review_task,
    process_training_plan_task,
    sweep_stuck_jobs,
)

# ---------------------------------------------------------------------------
# JobRunner helpers
# ---------------------------------------------------------------------------


def make_runner(
    *,
    max_concurrent: int = 1,
    max_queued: int = 2,
    max_wait_sec: float = 0.5,
    timeout_sec: float = 1.0,
    shutdown_grace_sec: float = 0.5,
) -> JobRunner:
    return JobRunner(
        max_concurrent=max_concurrent,
        max_queued=max_queued,
        max_wait_sec=max_wait_sec,
        timeout_sec=timeout_sec,
        shutdown_grace_sec=shutdown_grace_sec,
    )


class _Recorder:
    """Collects the (job_id, message) pairs the runner reports via on_failure."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, job_id: str, message: str) -> None:
        self.calls.append((job_id, message))


async def drain(runner: JobRunner) -> None:
    """Wait for every task the runner still holds, ignoring cancellations."""
    tasks = list(runner._tasks)
    if tasks:
        await asyncio.wait(tasks, timeout=2)


# ---------------------------------------------------------------------------
# JobRunner — scheduling
# ---------------------------------------------------------------------------


async def test_submit_returns_immediately_and_the_job_runs():
    runner = make_runner()
    started = asyncio.Event()
    finished = asyncio.Event()

    async def job(job_id: str) -> bool:
        started.set()
        finished.set()
        return True

    runner.submit(job, "a", on_failure=_Recorder())
    assert not started.is_set()  # nothing ran during submit itself

    await asyncio.wait_for(finished.wait(), 1)
    await drain(runner)
    assert runner._tasks == set()  # the done callback released the reference
    assert runner.waiting == 0
    assert runner.running == 0


async def test_no_more_than_max_concurrent_jobs_run_at_once():
    runner = make_runner(max_concurrent=2, max_queued=4)
    release = asyncio.Event()
    peak = 0
    live = 0

    async def job(job_id: str) -> bool:
        nonlocal peak, live
        live += 1
        peak = max(peak, live)
        await release.wait()
        live -= 1
        return True

    for i in range(4):
        runner.submit(job, str(i), on_failure=_Recorder())

    await asyncio.sleep(0.05)
    assert peak == 2
    assert runner.waiting == 2
    release.set()
    await drain(runner)
    assert peak == 2
    assert runner.waiting == 0


async def test_an_exception_escaping_a_job_is_contained():
    runner = make_runner(max_concurrent=2, max_queued=2)
    survivor_ran = asyncio.Event()

    async def boom(job_id: str) -> bool:
        raise RuntimeError("escaped")

    async def survivor(job_id: str) -> bool:
        survivor_ran.set()
        return True

    with capture_logs() as logs:
        runner.submit(boom, "boom", on_failure=_Recorder())
        runner.submit(survivor, "ok", on_failure=_Recorder())
        await asyncio.wait_for(survivor_ran.wait(), 1)
        await drain(runner)

    assert [log["outcome"] for log in logs if log["event"] == "job.finished"] == [
        "failed",
        "succeeded",
    ]
    assert runner.running == 0


# ---------------------------------------------------------------------------
# JobRunner — admission control
# ---------------------------------------------------------------------------


async def test_submit_is_rejected_when_the_wait_queue_is_full():
    runner = make_runner(max_concurrent=1, max_queued=2)
    release = asyncio.Event()
    calls: list[str] = []

    async def job(job_id: str) -> bool:
        calls.append(job_id)
        await release.wait()
        return True

    runner.submit(job, "running", on_failure=_Recorder())
    await asyncio.sleep(0)  # let it take the only slot
    runner.submit(job, "queued-1", on_failure=_Recorder())
    runner.submit(job, "queued-2", on_failure=_Recorder())

    task_count = len(runner._tasks)
    with pytest.raises(JobRunnerBusyError):
        runner.submit(job, "rejected", on_failure=_Recorder())
    assert len(runner._tasks) == task_count  # no task was created

    release.set()
    await drain(runner)
    assert "rejected" not in calls


async def test_a_slot_freed_by_a_finished_job_reopens_the_queue():
    runner = make_runner(max_concurrent=1, max_queued=1)
    release = asyncio.Event()

    async def job(job_id: str) -> bool:
        await release.wait()
        return True

    runner.submit(job, "running", on_failure=_Recorder())
    await asyncio.sleep(0)
    runner.submit(job, "queued", on_failure=_Recorder())
    with pytest.raises(JobRunnerBusyError):
        runner.submit(job, "rejected", on_failure=_Recorder())

    release.set()
    await drain(runner)

    async def quick(job_id: str) -> bool:
        return True

    runner.submit(quick, "later", on_failure=_Recorder())  # must not raise
    await drain(runner)


async def test_a_burst_fills_the_slots_then_the_queue_then_is_rejected():
    """The acceptance case: 2 running + 4 queued are accepted, the 7th is not.

    Requests arrive with awaits between them (each one hits the database first),
    so the running jobs have taken their slots and no longer count as waiting.
    """
    runner = make_runner(max_concurrent=2, max_queued=4)
    release = asyncio.Event()
    accepted: list[str] = []

    async def job(job_id: str) -> bool:
        await release.wait()
        return True

    rejected: list[str] = []
    for i in range(7):
        try:
            runner.submit(job, str(i), on_failure=_Recorder())
            accepted.append(str(i))
        except JobRunnerBusyError:
            rejected.append(str(i))
        await asyncio.sleep(0)  # stands in for the request's own awaits

    assert len(accepted) == 6
    assert rejected == ["6"]
    assert runner.running == 2
    assert runner.waiting == 4

    release.set()
    await drain(runner)


async def test_a_rejected_submit_is_logged_as_rejected_busy():
    runner = make_runner(max_concurrent=1, max_queued=0)

    async def job(job_id: str) -> bool:
        return True

    with capture_logs() as logs:
        with pytest.raises(JobRunnerBusyError):
            runner.submit(job, "rejected", on_failure=_Recorder())

    finished = [log for log in logs if log["event"] == "job.finished"]
    assert len(finished) == 1
    assert finished[0]["outcome"] == "rejected_busy"
    assert finished[0]["job_id"] == "rejected"


# ---------------------------------------------------------------------------
# JobRunner — wait timeout and run timeout
# ---------------------------------------------------------------------------


async def test_a_job_that_waits_too_long_is_failed_as_busy_without_running():
    runner = make_runner(max_concurrent=1, max_queued=2, max_wait_sec=0.05, timeout_sec=1.0)
    release = asyncio.Event()
    ran: list[str] = []
    failures = _Recorder()

    async def job(job_id: str) -> bool:
        ran.append(job_id)
        await release.wait()
        return True

    runner.submit(job, "running", on_failure=failures)
    await asyncio.sleep(0)
    runner.submit(job, "waiting", on_failure=failures)

    with capture_logs() as logs:
        await asyncio.sleep(0.15)

    assert failures.calls == [("waiting", BUSY_MESSAGE)]
    assert ran == ["running"]  # the waiting job never entered fn
    assert runner.waiting == 0
    outcomes = [log["outcome"] for log in logs if log["event"] == "job.finished"]
    assert outcomes == ["wait_timeout"]

    release.set()
    await drain(runner)


async def test_the_run_deadline_counts_time_spent_waiting_for_a_slot():
    runner = make_runner(max_concurrent=1, max_queued=2, max_wait_sec=0.3, timeout_sec=0.25)
    cancelled: list[str] = []
    first_done = asyncio.Event()

    async def hog(job_id: str) -> bool:
        await asyncio.sleep(0.15)
        first_done.set()
        return True

    async def slow(job_id: str) -> bool:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            cancelled.append(job_id)
            raise
        return True

    runner.submit(hog, "hog", on_failure=_Recorder())
    await asyncio.sleep(0)
    runner.submit(slow, "slow", on_failure=_Recorder())

    with capture_logs() as logs:
        await drain(runner)

    # 'slow' waited ~0.15s of its 0.25s budget, so it was cancelled well before
    # its own 5s sleep could finish — the wait is not a free allowance.
    assert cancelled == ["slow"]
    finished = {log["job_id"]: log["outcome"] for log in logs if log["event"] == "job.finished"}
    assert finished == {"hog": "succeeded", "slow": "run_timeout"}
    assert runner.waiting == 0
    assert runner.running == 0


async def test_a_job_that_marked_itself_failed_is_logged_as_failed():
    runner = make_runner()

    async def job(job_id: str) -> bool:
        return False  # the task function recorded the failure on the row itself

    failures = _Recorder()
    with capture_logs() as logs:
        runner.submit(job, "j", on_failure=failures)
        await drain(runner)

    assert failures.calls == []  # on_failure is only for jobs that never started
    assert [log["outcome"] for log in logs if log["event"] == "job.finished"] == ["failed"]


# ---------------------------------------------------------------------------
# JobRunner — the job deadline
# ---------------------------------------------------------------------------


async def test_the_job_deadline_is_visible_inside_the_job_and_in_a_thread():
    runner = make_runner(timeout_sec=5.0)
    seen: dict[str, float | None] = {}

    async def job(job_id: str) -> bool:
        seen["coroutine"] = JOB_DEADLINE.get()
        seen["thread"] = await asyncio.to_thread(JOB_DEADLINE.get)
        return True

    runner.submit(job, "j", on_failure=_Recorder())
    await drain(runner)

    assert seen["coroutine"] is not None
    assert seen["thread"] == seen["coroutine"]
    assert JOB_DEADLINE.get() is None  # reset once the job is done


async def test_shutting_down_is_false_outside_a_shutdown():
    runner = make_runner()
    seen: list[bool] = []

    async def job(job_id: str) -> bool:
        seen.append(shutting_down())
        return True

    runner.submit(job, "j", on_failure=_Recorder())
    await drain(runner)

    assert seen == [False]
    assert shutting_down() is False


# ---------------------------------------------------------------------------
# JobRunner — shutdown
# ---------------------------------------------------------------------------


async def test_close_waits_for_a_job_that_finishes_within_the_grace_period():
    runner = make_runner(shutdown_grace_sec=1.0)
    completed: list[str] = []

    async def job(job_id: str) -> bool:
        await asyncio.sleep(0.05)
        completed.append(job_id)
        return True

    runner.submit(job, "j", on_failure=_Recorder())
    await asyncio.sleep(0)
    await runner.close()

    assert completed == ["j"]


async def test_close_cancels_a_job_that_outlasts_the_grace_period():
    runner = make_runner(shutdown_grace_sec=0.05)
    seen_shutdown: list[bool] = []

    async def job(job_id: str) -> bool:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            # This is what makes the task function pick SWEEP_MESSAGE over
            # TIMEOUT_MESSAGE: the cause was a shutdown, not a slow job.
            seen_shutdown.append(shutting_down())
            raise
        return True

    runner.submit(job, "j", on_failure=_Recorder())
    await asyncio.sleep(0)
    await runner.close()

    assert seen_shutdown == [True]
    assert runner.running == 0


async def test_close_fails_a_queued_job_immediately_with_the_interrupted_message():
    runner = make_runner(max_concurrent=1, max_queued=2, shutdown_grace_sec=0.05)
    failures = _Recorder()
    ran: list[str] = []

    async def job(job_id: str) -> bool:
        ran.append(job_id)
        await asyncio.sleep(5)
        return True

    runner.submit(job, "running", on_failure=failures)
    await asyncio.sleep(0)
    runner.submit(job, "queued", on_failure=failures)

    with capture_logs() as logs:
        await runner.close()

    assert failures.calls == [("queued", SWEEP_MESSAGE)]
    assert ran == ["running"]
    assert runner.waiting == 0
    finished = {log["job_id"]: log["outcome"] for log in logs if log["event"] == "job.finished"}
    assert finished == {"queued": "cancelled_waiting", "running": "cancelled_running"}


async def test_a_failing_on_failure_hook_does_not_break_the_runner():
    """If the mark-failed write itself fails, the row is left for the sweep — but
    the runner must stay usable for every other job."""
    runner = make_runner(max_concurrent=1, max_queued=2, max_wait_sec=0.05)
    release = asyncio.Event()

    async def broken_hook(job_id: str, message: str) -> None:
        raise RuntimeError("database gone")

    async def job(job_id: str) -> bool:
        await release.wait()
        return True

    runner.submit(job, "running", on_failure=broken_hook)
    await asyncio.sleep(0)
    runner.submit(job, "waiting", on_failure=broken_hook)
    await asyncio.sleep(0.15)  # the waiting job gives up

    assert runner.waiting == 0
    release.set()
    await drain(runner)

    async def quick(job_id: str) -> bool:
        return True

    runner.submit(quick, "later", on_failure=_Recorder())  # still accepting work
    await drain(runner)


async def test_submit_after_close_is_refused():
    runner = make_runner()
    await runner.close()

    async def job(job_id: str) -> bool:
        return True

    with pytest.raises(JobRunnerClosedError):
        runner.submit(job, "j", on_failure=_Recorder())
    assert runner.shutting_down is True


async def test_closing_an_idle_runner_is_a_no_op():
    await make_runner().close()  # must not raise


async def test_every_submitted_job_logs_exactly_one_finished_event():
    runner = make_runner(max_concurrent=2, max_queued=4)

    async def job(job_id: str) -> bool:
        return True

    with capture_logs() as logs:
        for i in range(3):
            runner.submit(job, str(i), on_failure=_Recorder())
        await drain(runner)

    finished = [log for log in logs if log["event"] == "job.finished"]
    assert sorted(log["job_id"] for log in finished) == ["0", "1", "2"]
    for log in finished:
        assert log["outcome"] == "succeeded"
        assert log["wait_sec"] >= 0
        assert log["run_sec"] >= 0


# ---------------------------------------------------------------------------
# _friendly_message
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (AIGenerationFailedError(), ERROR_MESSAGES["AIGenerationFailedError"]),
        (AIParseFailedError(), ERROR_MESSAGES["AIParseFailedError"]),
        (StorageDownloadError(), ERROR_MESSAGES["StorageDownloadError"]),
        (NoMediaForSessionError(), ERROR_MESSAGES["NoMediaForSessionError"]),
        (InvalidMediaError(), ERROR_MESSAGES["InvalidMediaError"]),
    ],
)
def test_known_failures_get_a_user_facing_message(exc, expected):
    assert _friendly_message(exc) == expected


def test_unknown_failures_fall_back_to_the_generic_message():
    assert _friendly_message(RuntimeError("internal detail")) == SWEEP_MESSAGE


def test_internal_details_never_reach_the_user_message():
    assert "psycopg" not in _friendly_message(RuntimeError("psycopg2.OperationalError at 0x7f"))


def test_a_subclass_does_not_inherit_its_parents_message():
    """Messages are keyed by exact class name, so an unexpected media failure reads
    as interrupted rather than borrowing its parent's wording."""

    class _OddMediaError(InvalidMediaError):
        pass

    assert _friendly_message(_OddMediaError()) == SWEEP_MESSAGE


# ---------------------------------------------------------------------------
# process_review_task / process_training_plan_task
# ---------------------------------------------------------------------------


class _StubService:
    """Stands in for ReviewService/TrainingService with a closeable db handle."""

    def __init__(self, *, raises: BaseException | None = None) -> None:
        self._raises = raises
        self.db = AsyncMock()
        self.sessions_repo = type("R", (), {"db": self.db})()
        self.review_repo = self.sessions_repo
        self.processed: list = []

    async def process_review(self, review_id):
        self.processed.append(review_id)
        if self._raises:
            raise self._raises

    async def process_training_plan(self, plan_id):
        self.processed.append(plan_id)
        if self._raises:
            raise self._raises


@pytest.fixture
def review_job(monkeypatch):
    """Wire the task functions to a stub service and capture mark_failed calls."""
    failures: list[tuple[str, str]] = []

    def _install(*, raises: BaseException | None = None) -> _StubService:
        service = _StubService(raises=raises)
        monkeypatch.setattr("app.jobs._build_review_service", AsyncMock(return_value=service))
        monkeypatch.setattr("app.jobs._build_training_service", AsyncMock(return_value=service))

        async def _mark(row_id, message):
            failures.append((row_id, message))

        monkeypatch.setattr("app.jobs.mark_review_failed", _mark)
        monkeypatch.setattr("app.jobs.mark_plan_failed", _mark)
        return service

    _install.failures = failures  # type: ignore[attr-defined]
    return _install


async def test_successful_review_records_no_failure(review_job):
    service = review_job()
    review_id = str(uuid4())

    assert await process_review_task(review_id) is True

    assert service.processed == [UUID(review_id)]
    assert review_job.failures == []
    service.db.close.assert_awaited()


async def test_failed_review_is_marked_with_a_friendly_message(review_job):
    review_job(raises=AIGenerationFailedError())
    review_id = str(uuid4())

    assert await process_review_task(review_id) is False

    assert review_job.failures == [(review_id, ERROR_MESSAGES["AIGenerationFailedError"])]


async def test_a_failing_review_does_not_re_raise(review_job):
    """There are no automatic retries; the row is failed and the user taps Retry."""
    review_job(raises=RuntimeError("boom"))
    await process_review_task(str(uuid4()))  # must not raise


async def test_cancellation_marks_the_review_as_timed_out_and_propagates(review_job):
    review_job(raises=asyncio.CancelledError())
    review_id = str(uuid4())

    with pytest.raises(asyncio.CancelledError):
        await process_review_task(review_id)

    assert review_job.failures == [(review_id, TIMEOUT_MESSAGE)]


async def test_cancellation_during_shutdown_reads_as_interrupted(review_job):
    """Same cancellation, different cause: a redeploy must not be reported as
    "took too long", which would send the user chasing a slow video."""
    review_job(raises=asyncio.CancelledError())
    review_id = str(uuid4())
    runner = make_runner(shutdown_grace_sec=0.05)
    await runner.close()  # now shutting down

    async def _job(job_id: str) -> bool:
        return await process_review_task(job_id)

    from app.core.jobs import CURRENT_RUNNER

    token = CURRENT_RUNNER.set(runner)
    try:
        with pytest.raises(asyncio.CancelledError):
            await _job(review_id)
    finally:
        CURRENT_RUNNER.reset(token)

    assert review_job.failures == [(review_id, SWEEP_MESSAGE)]


async def test_the_db_session_is_closed_even_when_processing_fails(review_job):
    service = review_job(raises=RuntimeError("boom"))
    await process_review_task(str(uuid4()))
    service.db.close.assert_awaited()


async def test_the_db_session_is_closed_on_cancellation(review_job):
    service = review_job(raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await process_review_task(str(uuid4()))
    service.db.close.assert_awaited()


async def test_successful_training_plan_records_no_failure(review_job):
    service = review_job()
    assert await process_training_plan_task(str(uuid4())) is True
    assert review_job.failures == []
    service.db.close.assert_awaited()


async def test_failed_training_plan_is_marked_with_a_friendly_message(review_job):
    review_job(raises=AIParseFailedError())
    plan_id = str(uuid4())

    assert await process_training_plan_task(plan_id) is False

    assert review_job.failures == [(plan_id, ERROR_MESSAGES["AIParseFailedError"])]


async def test_training_plan_cancellation_marks_timeout_and_propagates(review_job):
    review_job(raises=asyncio.CancelledError())
    plan_id = str(uuid4())

    with pytest.raises(asyncio.CancelledError):
        await process_training_plan_task(plan_id)

    assert review_job.failures == [(plan_id, TIMEOUT_MESSAGE)]


# ---------------------------------------------------------------------------
# sweep_stuck_jobs
# ---------------------------------------------------------------------------


class _FakeResult:
    def __init__(self, rowcount: int) -> None:
        self.rowcount = rowcount


class _FakeDb:
    def __init__(self, rowcount: int = 0) -> None:
        self.statements: list[tuple[str, dict]] = []
        self._rowcount = rowcount
        self.committed = False
        self.closed = False

    async def execute(self, stmt, params=None):
        self.statements.append((str(stmt), params or {}))
        return _FakeResult(self._rowcount)

    async def commit(self):
        self.committed = True

    async def close(self):
        self.closed = True


async def test_sweeper_covers_both_queues_and_commits(monkeypatch):
    db = _FakeDb(rowcount=2)
    monkeypatch.setattr("app.jobs.SessionLocal", lambda: db)

    await sweep_stuck_jobs()

    tables = " ".join(sql for sql, _ in db.statements)
    assert "reviews" in tables
    assert "training_plans" in tables
    assert db.committed is True
    assert db.closed is True


async def test_sweeper_only_touches_processing_rows(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr("app.jobs.SessionLocal", lambda: db)

    await sweep_stuck_jobs()

    for sql, params in db.statements:
        assert "status = 'processing'" in sql
        assert params["msg"] == SWEEP_MESSAGE
        assert params["threshold"] > 0


async def test_sweeper_closes_the_session_even_if_a_statement_fails(monkeypatch):
    db = _FakeDb()

    async def _boom(stmt, params=None):
        raise RuntimeError("db gone")

    db.execute = _boom  # type: ignore[assignment]
    monkeypatch.setattr("app.jobs.SessionLocal", lambda: db)

    with pytest.raises(RuntimeError):
        await sweep_stuck_jobs()

    assert db.closed is True


# ---------------------------------------------------------------------------
# mark_review_failed / mark_plan_failed (the runner's on_failure targets)
# ---------------------------------------------------------------------------


async def test_mark_review_failed_uses_its_own_session_and_closes_it(monkeypatch):
    db = _FakeDb()
    recorded: list[tuple[UUID, str]] = []

    class _Repo:
        def __init__(self, session) -> None:
            assert session is db

        async def mark_failed(self, review_id, message):
            recorded.append((review_id, message))

    monkeypatch.setattr("app.jobs.SessionLocal", lambda: db)
    monkeypatch.setattr("app.jobs.ReviewRepository", _Repo)

    review_id = uuid4()
    await mark_review_failed(str(review_id), BUSY_MESSAGE)

    assert recorded == [(review_id, BUSY_MESSAGE)]
    assert db.closed is True


async def test_mark_plan_failed_closes_its_session_even_when_the_write_fails(monkeypatch):
    db = _FakeDb()

    class _Repo:
        def __init__(self, session) -> None:
            pass

        async def mark_failed(self, plan_id, message):
            raise RuntimeError("database gone")

    monkeypatch.setattr("app.jobs.SessionLocal", lambda: db)
    monkeypatch.setattr("app.jobs.TrainingPlanRepository", _Repo)

    with pytest.raises(RuntimeError):
        await mark_plan_failed(str(uuid4()), SWEEP_MESSAGE)

    assert db.closed is True


# ---------------------------------------------------------------------------
# Configuration invariants
# ---------------------------------------------------------------------------


def test_the_stuck_threshold_outlives_the_job_timeout():
    """Sweeping sooner than the total job budget would fail jobs still running.

    JOB_TIMEOUT_SEC already covers the queue wait, so it is the whole wall-clock
    life of a job from submit.
    """
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.STUCK_JOB_THRESHOLD_SEC > settings.JOB_TIMEOUT_SEC
