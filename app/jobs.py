"""Background tasks, run inside the API process (no queue, no broker).

These coroutines share the API's event loop, so every blocking or CPU-bound step
MUST go through ``asyncio.to_thread`` — storage downloads, frame extraction and
Gemini calls already do. Anything added here that blocks the loop stalls HTTP for
every user of the process. See ``app/core/jobs.py`` for the runner that owns
them, and ``docs/SPEC_BACKEND_Merge_Worker_Into_API.md``.

Each task returns True on success and False when it recorded its own failure, so
the runner can log the outcome it cannot otherwise see.
"""

import asyncio
from uuid import UUID

import structlog
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.frame_extractor import FrameExtractor
from app.core.jobs import BUSY_MESSAGE, SWEEP_MESSAGE, shutting_down
from app.core.storage import get_storage_client
from app.repositories.ai import ReviewRepository, TrainingPlanRepository
from app.repositories.auth import AuthRepository
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository
from app.repositories.surfboards import SurfboardRepository
from app.services.ai import GeminiService, ReviewService, TrainingService

logger = structlog.get_logger(__name__)

ERROR_MESSAGES = {
    "AIGenerationFailedError": "AI service is temporarily unavailable.",
    "AIParseFailedError": "AI returned an unexpected response format.",
    "StorageDownloadError": "Could not retrieve your media files.",
    "NoMediaForSessionError": "No media found for this session.",
    "InvalidMediaError": "Video could not be processed.",
}

TIMEOUT_MESSAGE = "Processing took too long and was stopped."

# Re-exported from app.core.jobs, where the runner records them: SWEEP_MESSAGE for
# a job interrupted by a shutdown, BUSY_MESSAGE for one rejected or timed out
# waiting for a slot.
__all__ = [
    "BUSY_MESSAGE",
    "ERROR_MESSAGES",
    "SWEEP_MESSAGE",
    "TIMEOUT_MESSAGE",
    "mark_plan_failed",
    "mark_review_failed",
    "process_review_task",
    "process_training_plan_task",
    "sweep_stuck_jobs",
]


def _friendly_message(exc: Exception) -> str:
    cls_name = type(exc).__name__
    return ERROR_MESSAGES.get(cls_name, SWEEP_MESSAGE)


def _cancellation_message() -> str:
    """Why was this job cancelled? Shutdown reads as interrupted, else timeout."""
    return SWEEP_MESSAGE if shutting_down() else TIMEOUT_MESSAGE


async def _build_review_service() -> ReviewService:
    db = SessionLocal()
    storage = get_storage_client()
    return ReviewService(
        sessions_repo=SessionsRepository(db),
        media_repo=MediaRepository(db),
        review_repo=ReviewRepository(db),
        auth_repo=AuthRepository(db),
        surfboard_repo=SurfboardRepository(db),
        gemini=GeminiService(),
        frame_extractor=FrameExtractor(),
        storage=storage,
    )


async def _build_training_service() -> TrainingService:
    db = SessionLocal()
    return TrainingService(
        review_repo=ReviewRepository(db),
        auth_repo=AuthRepository(db),
        training_plan_repo=TrainingPlanRepository(db),
        gemini=GeminiService(),
    )


async def mark_review_failed(review_id: str, message: str) -> None:
    """Fail a review row from its own DB session (also the runner's on_failure hook)."""
    db = SessionLocal()
    try:
        repo = ReviewRepository(db)
        await repo.mark_failed(UUID(review_id), message)
    finally:
        await db.close()


async def mark_plan_failed(plan_id: str, message: str) -> None:
    """Fail a training plan row from its own DB session (the runner's on_failure hook)."""
    db = SessionLocal()
    try:
        repo = TrainingPlanRepository(db)
        await repo.mark_failed(UUID(plan_id), message)
    finally:
        await db.close()


async def process_review_task(review_id: str) -> bool:
    """Generate a review from the session's media using Gemini."""
    logger.info("Job: processing review %s", review_id)
    # Its own session: the request's session is closed once the response is sent.
    service = await _build_review_service()
    try:
        await service.process_review(UUID(review_id))
        logger.info("Job: review %s completed", review_id)
        return True
    except asyncio.CancelledError:
        # The run deadline fired, or the app is shutting down. Record the failure,
        # then let the runner see the cancellation. The DB write is shielded so it
        # is not itself cancelled mid-flight.
        message = _cancellation_message()
        logger.warning("Job: review %s cancelled", review_id, reason=message)
        await asyncio.shield(mark_review_failed(review_id, message))
        raise
    except Exception as exc:
        logger.exception("Job: review %s failed", review_id)
        await mark_review_failed(review_id, _friendly_message(exc))
        return False
    finally:
        await service.sessions_repo.db.close()


async def process_training_plan_task(plan_id: str) -> bool:
    """Generate a training plan from a completed review using Gemini."""
    logger.info("Job: processing training plan %s", plan_id)
    service = await _build_training_service()
    try:
        await service.process_training_plan(UUID(plan_id))
        logger.info("Job: training plan %s completed", plan_id)
        return True
    except asyncio.CancelledError:
        message = _cancellation_message()
        logger.warning("Job: training plan %s cancelled", plan_id, reason=message)
        await asyncio.shield(mark_plan_failed(plan_id, message))
        raise
    except Exception as exc:
        logger.exception("Job: training plan %s failed", plan_id)
        await mark_plan_failed(plan_id, _friendly_message(exc))
        return False
    finally:
        await service.review_repo.db.close()


async def sweep_stuck_jobs() -> None:
    """Fail any rows left in 'processing' beyond the threshold.

    Runs once at startup. Every other failure path marks its own row, so this
    only catches what a hard kill (OOM, SIGKILL, host crash) left behind — the
    container always restarts, so booting is the right moment to look.
    """
    threshold = get_settings().STUCK_JOB_THRESHOLD_SEC
    db = SessionLocal()
    try:
        for table in ("reviews", "training_plans"):
            result = await db.execute(
                text(
                    f"""
                    UPDATE public.{table}
                    SET status = 'failed', error_message = :msg
                    WHERE status = 'processing'
                      AND processing_started_at < now() - make_interval(secs => :threshold)
                    """
                ),
                {"msg": SWEEP_MESSAGE, "threshold": threshold},
            )
            if result.rowcount:
                logger.warning("Sweeper: marked %d stuck %s as failed", result.rowcount, table)
        await db.commit()
    finally:
        await db.close()
