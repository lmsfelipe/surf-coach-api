"""arq task orchestration: failure bookkeeping, timeout handling, session cleanup.

The services themselves are covered elsewhere; what matters here is that a task
never leaves a row stuck in 'processing' and never leaks a DB session.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.errors import (
    AIGenerationFailedError,
    AIParseFailedError,
    InvalidMediaError,
    MediaProcessingKilledError,
    NoMediaForSessionError,
    StorageDownloadError,
)
from app.worker import (
    ERROR_MESSAGES,
    SWEEP_MESSAGE,
    TIMEOUT_MESSAGE,
    WorkerSettings,
    _friendly_message,
    process_review_task,
    process_training_plan_task,
    sweep_stuck_jobs,
    sweep_unoptimized_media,
)

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
    """Messages are keyed by exact class name, so an OOM kill reads as interrupted
    rather than blaming the video file."""
    assert _friendly_message(MediaProcessingKilledError()) == SWEEP_MESSAGE


# ---------------------------------------------------------------------------
# process_review_task
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
def review_worker(monkeypatch):
    """Wire process_review_task to a stub service and capture mark_failed calls."""
    failures: list[tuple[str, str]] = []

    def _install(*, raises: BaseException | None = None) -> _StubService:
        service = _StubService(raises=raises)
        monkeypatch.setattr("app.worker._build_review_service", AsyncMock(return_value=service))
        monkeypatch.setattr("app.worker._build_training_service", AsyncMock(return_value=service))

        async def _mark(row_id, message):
            failures.append((row_id, message))

        monkeypatch.setattr("app.worker._mark_review_failed", _mark)
        monkeypatch.setattr("app.worker._mark_plan_failed", _mark)
        return service

    _install.failures = failures  # type: ignore[attr-defined]
    return _install


async def test_successful_review_records_no_failure(review_worker):
    service = review_worker()
    review_id = str(uuid4())

    await process_review_task({}, review_id)

    assert service.processed == [__import__("uuid").UUID(review_id)]
    assert review_worker.failures == []
    service.db.close.assert_awaited()


async def test_failed_review_is_marked_with_a_friendly_message(review_worker):
    review_worker(raises=AIGenerationFailedError())
    review_id = str(uuid4())

    await process_review_task({}, review_id)

    assert review_worker.failures == [(review_id, ERROR_MESSAGES["AIGenerationFailedError"])]


async def test_a_failing_review_does_not_re_raise(review_worker):
    """arq is configured with retry_jobs=False; swallowing keeps the queue clean."""
    review_worker(raises=RuntimeError("boom"))
    await process_review_task({}, str(uuid4()))  # must not raise


async def test_cancellation_marks_the_review_as_timed_out_and_propagates(review_worker):
    review_worker(raises=asyncio.CancelledError())
    review_id = str(uuid4())

    with pytest.raises(asyncio.CancelledError):
        await process_review_task({}, review_id)

    assert review_worker.failures == [(review_id, TIMEOUT_MESSAGE)]


async def test_the_db_session_is_closed_even_when_processing_fails(review_worker):
    service = review_worker(raises=RuntimeError("boom"))
    await process_review_task({}, str(uuid4()))
    service.db.close.assert_awaited()


async def test_the_db_session_is_closed_on_cancellation(review_worker):
    service = review_worker(raises=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await process_review_task({}, str(uuid4()))
    service.db.close.assert_awaited()


# ---------------------------------------------------------------------------
# process_training_plan_task
# ---------------------------------------------------------------------------


async def test_successful_training_plan_records_no_failure(review_worker):
    service = review_worker()
    await process_training_plan_task({}, str(uuid4()))
    assert review_worker.failures == []
    service.db.close.assert_awaited()


async def test_failed_training_plan_is_marked_with_a_friendly_message(review_worker):
    review_worker(raises=AIParseFailedError())
    plan_id = str(uuid4())

    await process_training_plan_task({}, plan_id)

    assert review_worker.failures == [(plan_id, ERROR_MESSAGES["AIParseFailedError"])]


async def test_training_plan_cancellation_marks_timeout_and_propagates(review_worker):
    review_worker(raises=asyncio.CancelledError())
    plan_id = str(uuid4())

    with pytest.raises(asyncio.CancelledError):
        await process_training_plan_task({}, plan_id)

    assert review_worker.failures == [(plan_id, TIMEOUT_MESSAGE)]


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
    monkeypatch.setattr("app.worker.SessionLocal", lambda: db)

    await sweep_stuck_jobs({})

    tables = " ".join(sql for sql, _ in db.statements)
    assert "reviews" in tables
    assert "training_plans" in tables
    assert db.committed is True
    assert db.closed is True


async def test_sweeper_only_touches_processing_rows(monkeypatch):
    db = _FakeDb()
    monkeypatch.setattr("app.worker.SessionLocal", lambda: db)

    await sweep_stuck_jobs({})

    for sql, params in db.statements:
        assert "status = 'processing'" in sql
        assert params["msg"] == SWEEP_MESSAGE
        assert params["threshold"] > 0


async def test_sweeper_closes_the_session_even_if_a_statement_fails(monkeypatch):
    db = _FakeDb()

    async def _boom(stmt, params=None):
        raise RuntimeError("db gone")

    db.execute = _boom  # type: ignore[assignment]
    monkeypatch.setattr("app.worker.SessionLocal", lambda: db)

    with pytest.raises(RuntimeError):
        await sweep_stuck_jobs({})

    assert db.closed is True


# ---------------------------------------------------------------------------
# sweep_unoptimized_media
# ---------------------------------------------------------------------------


class _FakeOptimizeRepo:
    def __init__(self, rows) -> None:
        self._rows = rows
        self.query: dict = {}

    async def list_unoptimized_videos(self, older_than_sec, limit, max_attempts):
        self.query = {
            "older_than_sec": older_than_sec,
            "limit": limit,
            "max_attempts": max_attempts,
        }
        return self._rows


class _FakeRedis:
    def __init__(self) -> None:
        self.jobs: list[tuple[str, str]] = []

    async def enqueue_job(self, name, *args):
        self.jobs.append((name, *args))


async def test_sweep_enqueues_one_job_per_eligible_video(monkeypatch):
    rows = [type("M", (), {"id": uuid4()})() for _ in range(3)]
    repo = _FakeOptimizeRepo(rows)
    db = _FakeDb()
    monkeypatch.setattr("app.worker.SessionLocal", lambda: db)
    monkeypatch.setattr("app.worker.MediaRepository", lambda _db: repo)
    redis = _FakeRedis()

    await sweep_unoptimized_media({"redis": redis})

    assert [name for name, _ in redis.jobs] == ["optimize_media_task"] * 3
    assert {job_id for _, job_id in redis.jobs} == {str(r.id) for r in rows}
    assert db.closed is True


async def test_sweep_passes_the_configured_batch_and_attempt_caps(monkeypatch):
    from app.core.config import get_settings

    repo = _FakeOptimizeRepo([])
    monkeypatch.setattr("app.worker.SessionLocal", lambda: _FakeDb())
    monkeypatch.setattr("app.worker.MediaRepository", lambda _db: repo)

    await sweep_unoptimized_media({"redis": _FakeRedis()})

    settings = get_settings()
    assert repo.query["limit"] == settings.VIDEO_OPTIMIZE_BATCH
    assert repo.query["max_attempts"] == settings.VIDEO_OPTIMIZE_MAX_ATTEMPTS
    assert repo.query["older_than_sec"] == settings.VIDEO_OPTIMIZE_GRACE_SEC


async def test_sweep_enqueues_nothing_when_optimization_is_disabled(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "VIDEO_OPTIMIZE_ENABLED", False, raising=False)
    redis = _FakeRedis()

    def _no_session():
        raise AssertionError("must not open a session when disabled")

    monkeypatch.setattr("app.worker.SessionLocal", _no_session)

    await sweep_unoptimized_media({"redis": redis})

    assert redis.jobs == []


# ---------------------------------------------------------------------------
# WorkerSettings
# ---------------------------------------------------------------------------


def test_every_task_the_api_enqueues_is_registered():
    """A name mismatch here means jobs queue forever without ever running."""
    registered = {fn.__name__ for fn in WorkerSettings.functions}
    assert {
        "process_review_task",
        "process_training_plan_task",
        "optimize_media_task",
    } <= registered


def test_automatic_retries_stay_disabled():
    """Re-running a half-finished AI job would double-charge and duplicate writes."""
    assert WorkerSettings.retry_jobs is False


def test_the_stuck_threshold_outlives_the_job_timeout():
    """Sweeping sooner than the timeout would fail jobs that are still running."""
    from app.core.config import get_settings

    settings = get_settings()
    assert settings.STUCK_JOB_THRESHOLD_SEC > settings.WORKER_JOB_TIMEOUT_SEC


def test_the_optimize_sweep_does_not_run_at_startup():
    """A crash-loop would otherwise re-enqueue a full batch on every restart."""
    sweeps = [c for c in WorkerSettings.cron_jobs if "unoptimized" in c.name]
    assert sweeps and all(c.run_at_startup is False for c in sweeps)
