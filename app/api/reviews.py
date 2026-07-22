from uuid import UUID

import structlog
from arq import ArqRedis
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.deps import db_session, get_arq_pool, get_current_user
from app.core.rate_limit import limiter

logger = structlog.get_logger(__name__)

ENQUEUE_FAILED_MESSAGE = "Processing could not be started. Please try again."
from app.core.frame_extractor import FrameExtractor
from app.core.security.jwt import AuthUser
from app.core.storage import StorageClient, get_storage_client
from app.repositories.ai import ReviewRepository
from app.repositories.auth import AuthRepository
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository
from app.repositories.surfboards import SurfboardRepository
from app.schemas.reviews import ReviewCreate, ReviewOut
from app.services.ai import GeminiService, ReviewService

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
    arq_pool: ArqRedis = Depends(get_arq_pool),
) -> ReviewOut:
    review = await service.enqueue_review(payload.session_id, user)
    try:
        await arq_pool.enqueue_job("process_review_task", str(review.id))
    except Exception:
        logger.exception("Failed to enqueue review %s", review.id)
        review = await service.review_repo.mark_failed(review.id, ENQUEUE_FAILED_MESSAGE)
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
    arq_pool: ArqRedis = Depends(get_arq_pool),
) -> ReviewOut:
    review = await service.retry_review(review_id, user)
    try:
        await arq_pool.enqueue_job("process_review_task", str(review.id))
    except Exception:
        logger.exception("Failed to enqueue review %s", review.id)
        review = await service.review_repo.mark_failed(review.id, ENQUEUE_FAILED_MESSAGE)
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
