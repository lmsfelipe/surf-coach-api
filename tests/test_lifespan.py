"""App startup and shutdown.

Jobs now live in this process, so the lifespan is what makes a hard-killed
container clean up after itself (the startup sweep) and a redeploy end its jobs
in a terminal state (draining the runner before the DB pool goes away).
"""

from __future__ import annotations

import asyncio

import pytest

from app.core.config import get_settings
from app.core.jobs import JobRunner
from app.main import lifespan


@pytest.fixture
def app_stub():
    class _State:
        pass

    class _App:
        def __init__(self) -> None:
            self.state = _State()

    return _App()


async def test_startup_runs_the_sweep_once_and_builds_the_runner(app_stub, monkeypatch):
    sweeps: list[int] = []

    async def _sweep() -> None:
        sweeps.append(1)

    monkeypatch.setattr("app.main.sweep_stuck_jobs", _sweep)
    monkeypatch.setattr("app.main.engine", _FakeEngine())

    async with lifespan(app_stub):
        assert sweeps == [1]
        runner = app_stub.state.job_runner
        assert isinstance(runner, JobRunner)
        settings = get_settings()
        assert runner._max_concurrent == settings.JOBS_MAX_CONCURRENT
        assert runner._max_queued == settings.JOBS_MAX_QUEUED
        assert runner._timeout_sec == settings.JOB_TIMEOUT_SEC


async def test_a_failing_sweep_does_not_stop_startup(app_stub, monkeypatch):
    async def _boom() -> None:
        raise RuntimeError("database unreachable")

    monkeypatch.setattr("app.main.sweep_stuck_jobs", _boom)
    monkeypatch.setattr("app.main.engine", _FakeEngine())

    async with lifespan(app_stub):
        assert app_stub.state.job_runner is not None  # the app still booted


async def test_a_hanging_sweep_does_not_stop_startup(app_stub, monkeypatch):
    async def _hang() -> None:
        await asyncio.sleep(60)

    monkeypatch.setattr("app.main.sweep_stuck_jobs", _hang)
    monkeypatch.setattr("app.main.STARTUP_SWEEP_TIMEOUT_SEC", 0.05)
    monkeypatch.setattr("app.main.engine", _FakeEngine())

    async with lifespan(app_stub):
        assert app_stub.state.job_runner is not None


class _FakeEngine:
    def __init__(self) -> None:
        self.disposed = False

    async def dispose(self) -> None:
        self.disposed = True


async def test_shutdown_drains_the_runner_before_disposing_the_db_pool(app_stub, monkeypatch):
    order: list[str] = []

    async def _sweep() -> None:
        return None

    engine = _FakeEngine()

    async def _dispose() -> None:
        order.append("engine")
        engine.disposed = True

    engine.dispose = _dispose  # type: ignore[method-assign]
    monkeypatch.setattr("app.main.sweep_stuck_jobs", _sweep)
    monkeypatch.setattr("app.main.engine", engine)

    async with lifespan(app_stub):
        runner = app_stub.state.job_runner
        original_close = runner.close

        async def _close() -> None:
            order.append("runner")
            await original_close()

        runner.close = _close  # type: ignore[method-assign]

    # A cancelled job still needs a connection to mark its row failed, so the
    # pool must outlive the drain.
    assert order == ["runner", "engine"]
    assert engine.disposed is True
    assert runner.shutting_down is True
