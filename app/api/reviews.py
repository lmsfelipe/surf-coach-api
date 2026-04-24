from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import db_session, get_current_user
from app.core.frame_extractor import FrameExtractor
from app.core.security.jwt import AuthUser
from app.core.storage import StorageClient, get_storage_client
from app.repositories.ai import ReviewRepository
from app.repositories.auth import AuthRepository
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository
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
        gemini=gemini,
        frame_extractor=frame_extractor,
        storage=storage,
    )


@router.post(
    "/reviews/",
    response_model=ReviewOut,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_review(
    payload: ReviewCreate,
    user: AuthUser = Depends(get_current_user),
    service: ReviewService = Depends(get_review_service),
) -> ReviewOut:
    review = await service.generate_review(payload.session_id, user)
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
