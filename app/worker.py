"""arq worker — background tasks for review and training plan generation."""

import asyncio
from uuid import UUID

import structlog
from arq import cron
from arq.connections import RedisSettings
from sqlalchemy import text

from app.core.config import get_settings
from app.core.db import SessionLocal
from app.core.frame_extractor import FrameExtractor
from app.core.storage import get_storage_client
from app.core.video_transcoder import VideoTranscoder
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


def _key_from_storage_url(url: str, bucket: str) -> str | None:
    """Extract the storage key from a Supabase Storage public URL."""
    marker = f"/{bucket}/"
    idx = url.find(marker)
    if idx < 0:
        return None
    return url[idx + len(marker) :].split("?", 1)[0]


SWEEP_MESSAGE = "Processing was interrupted."


def _friendly_message(exc: Exception) -> str:
    cls_name = type(exc).__name__
    return ERROR_MESSAGES.get(cls_name, "Processing was interrupted.")


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


async def _mark_review_failed(review_id: str, message: str) -> None:
    db = SessionLocal()
    try:
        repo = ReviewRepository(db)
        await repo.mark_failed(UUID(review_id), message)
    finally:
        await db.close()


async def _mark_plan_failed(plan_id: str, message: str) -> None:
    db = SessionLocal()
    try:
        repo = TrainingPlanRepository(db)
        await repo.mark_failed(UUID(plan_id), message)
    finally:
        await db.close()


async def process_review_task(ctx: dict, review_id: str) -> None:
    """arq task: generate a review from media using Gemini."""
    logger.info("Worker: processing review %s", review_id)
    service = await _build_review_service()
    try:
        await service.process_review(UUID(review_id))
        logger.info("Worker: review %s completed", review_id)
    except asyncio.CancelledError:
        # job_timeout fired (or worker shutdown) — record the failure, then
        # let arq see the cancellation. Shield the DB write so it is not
        # itself cancelled mid-flight.
        logger.warning("Worker: review %s timed out or was cancelled", review_id)
        await asyncio.shield(_mark_review_failed(review_id, TIMEOUT_MESSAGE))
        raise
    except Exception as exc:
        logger.exception("Worker: review %s failed", review_id)
        await _mark_review_failed(review_id, _friendly_message(exc))
    finally:
        await service.sessions_repo.db.close()


async def process_training_plan_task(ctx: dict, plan_id: str) -> None:
    """arq task: generate a training plan using Gemini."""
    logger.info("Worker: processing training plan %s", plan_id)
    service = await _build_training_service()
    try:
        await service.process_training_plan(UUID(plan_id))
        logger.info("Worker: training plan %s completed", plan_id)
    except asyncio.CancelledError:
        logger.warning("Worker: training plan %s timed out or was cancelled", plan_id)
        await asyncio.shield(_mark_plan_failed(plan_id, TIMEOUT_MESSAGE))
        raise
    except Exception as exc:
        logger.exception("Worker: training plan %s failed", plan_id)
        await _mark_plan_failed(plan_id, _friendly_message(exc))
    finally:
        await service.review_repo.db.close()


async def sweep_stuck_jobs(ctx: dict) -> None:
    """Cron job: fail any rows stuck in 'processing' beyond the threshold."""
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


async def optimize_media_task(ctx: dict, media_id: str) -> None:
    """arq task: transcode one video and replace the raw object in storage."""
    settings = get_settings()
    if not settings.VIDEO_OPTIMIZE_ENABLED:
        return

    db = SessionLocal()
    try:
        media_repo = MediaRepository(db)
        media = await media_repo.get(UUID(media_id))
        if media is None or media.media_type != "video" or media.optimized_at is not None:
            return

        storage = get_storage_client()
        key = _key_from_storage_url(media.storage_url, settings.SUPABASE_BUCKET)
        if not key:
            logger.warning("optimize: could not derive key for media %s", media_id)
            return

        raw = await asyncio.to_thread(storage.download, key)
        compressed = await VideoTranscoder().transcode(raw)

        if len(compressed) >= len(raw):
            await media_repo.mark_optimized(media.id, len(raw))
            return

        await asyncio.to_thread(storage.upload, key, compressed, "video/mp4")
        await media_repo.mark_optimized(media.id, len(compressed))
        logger.info(
            "optimize: media %s %d -> %d bytes (-%.0f%%)",
            media_id,
            len(raw),
            len(compressed),
            100 * (1 - len(compressed) / len(raw)),
        )
    except Exception:
        logger.exception("optimize: media %s failed; raw left intact", media_id)
    finally:
        await db.close()


async def sweep_unoptimized_media(ctx: dict) -> None:
    """Cron job: enqueue optimize tasks for eligible videos."""
    settings = get_settings()
    if not settings.VIDEO_OPTIMIZE_ENABLED:
        return
    db = SessionLocal()
    try:
        repo = MediaRepository(db)
        rows = await repo.list_unoptimized_videos(
            older_than_sec=settings.VIDEO_OPTIMIZE_GRACE_SEC,
            limit=settings.VIDEO_OPTIMIZE_BATCH,
        )
        for m in rows:
            await ctx["redis"].enqueue_job("optimize_media_task", str(m.id))
        if rows:
            logger.info("optimize sweep: enqueued %d video(s)", len(rows))
    finally:
        await db.close()


class WorkerSettings:
    """arq worker configuration."""

    functions = [process_review_task, process_training_plan_task, optimize_media_task]
    cron_jobs = [
        cron(sweep_stuck_jobs, minute=set(range(60)), run_at_startup=True),
        cron(
            sweep_unoptimized_media,
            minute={0, 5, 10, 15, 20, 25, 30, 35, 40, 45, 50, 55},
            run_at_startup=True,
        ),
    ]
    redis_settings = RedisSettings.from_dsn(get_settings().REDIS_URL)
    max_jobs = get_settings().WORKER_MAX_JOBS
    job_timeout = get_settings().WORKER_JOB_TIMEOUT_SEC
    retry_jobs = False  # spec: no automatic retry (Phase 2)
