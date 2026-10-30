from uuid import UUID

import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import db_session, get_current_user, get_job_runner
from app.core.frame_extractor import FrameExtractor
from app.core.jobs import JobRunner, JobRunnerBusyError
from app.core.rate_limit import limiter
from app.core.security.jwt import AuthUser
from app.core.storage import StorageClient, get_storage_client
from app.jobs import BUSY_MESSAGE, mark_review_failed, process_review_task
from app.models.review import Review
from app.repositories.ai import ReviewRepository
from app.repositories.auth import AuthRepository
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository
from app.repositories.surfboards import SurfboardRepository
from app.schemas.reviews import ReviewCreate, ReviewOut
from app.services.ai import GeminiService, ReviewService

logger = structlog.get_logger(__name__)

ENQUEUE_FAILED_MESSAGE = "Processing could not be started. Please try again."

router = APIRouter(prefix="/api/v1", tags=["reviews"])


def get_gemini_service() -> GeminiService:
    return GeminiService()


def get_frame_extractor() -> FrameExtractor:
    return FrameExtractor()


def get_review_service(
    db: AsyncSession = Depends(db_session),
    storage: StorageClient = Depends(get_storage_client),
    gemini: GeminiService = Depends(get_gemini_service),
    frame_extractor: FrameExtractor = Depends(get_frame_extractor),
) -> ReviewService:
    return ReviewService(
        sessions_repo=SessionsRepository(db),
        media_repo=MediaRepository(db),
        review_repo=ReviewRepository(db),
        auth_repo=AuthRepository(db),
        surfboard_repo=SurfboardRepository(db),
        gemini=gemini,
        frame_extractor=frame_extractor,
        storage=storage,
    )


async def _submit_review_job(runner: JobRunner, service: ReviewService, review: Review) -> Review:
    """Hand the review to the runner, or fail the row if it was not accepted.

    A row must never be left 'processing' with no job behind it, or the client
    polls until the stuck-job threshold. A rejected burst says so ("busy, try
    again") rather than timing out four minutes later with the wrong reason.
    """
    try:
        runner.submit(process_review_task, str(review.id), on_failure=mark_review_failed)
    except JobRunnerBusyError:
        logger.warning("Job queue full; review rejected", review_id=str(review.id))
        return await service.review_repo.mark_failed(review.id, BUSY_MESSAGE)
    except Exception:  # JobRunnerClosedError during shutdown
        logger.exception("Failed to enqueue review %s", review.id)
        return await service.review_repo.mark_failed(review.id, ENQUEUE_FAILED_MESSAGE)
    return review


@router.post(
    "/reviews/",
    response_model=ReviewOut,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_AI)
async def create_review(
    request: Request,
    payload: ReviewCreate,
    user: AuthUser = Depends(get_current_user),
    service: ReviewService = Depends(get_review_service),
    runner: JobRunner = Depends(get_job_runner),
) -> ReviewOut:
    review = await service.enqueue_review(payload.session_id, user)
    review = await _submit_review_job(runner, service, review)
    return ReviewOut.model_validate(review)


@router.post(
    "/reviews/{review_id}/retry",
    response_model=ReviewOut,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_AI)
async def retry_review(
    request: Request,
    review_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: ReviewService = Depends(get_review_service),
    runner: JobRunner = Depends(get_job_runner),
) -> ReviewOut:
    review = await service.retry_review(review_id, user)
    review = await _submit_review_job(runner, service, review)
    return ReviewOut.model_validate(review)


@router.get(
    "/reviews/{review_id}",
    response_model=ReviewOut,
    response_model_by_alias=True,
)
async def get_review(
    review_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: ReviewService = Depends(get_review_service),
) -> ReviewOut:
    review = await service.get_review(review_id, user)
    return ReviewOut.model_validate(review)


@router.get(
    "/sessions/{session_id}/review",
    response_model=ReviewOut,
    response_model_by_alias=True,
)
async def get_review_for_session(
    session_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: ReviewService = Depends(get_review_service),
) -> ReviewOut:
    review = await service.get_review_for_session(session_id, user)
    return ReviewOut.model_validate(review)
