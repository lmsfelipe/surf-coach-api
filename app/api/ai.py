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
from app.core.security.jwt import AuthUser
from app.repositories.ai import ReviewRepository, TrainingPlanRepository
from app.repositories.auth import AuthRepository
from app.schemas.training import (
    GenerateTrainingPlanRequest,
    TrainingPlanListResponse,
    TrainingPlanResponse,
    WorkoutResponse,
)
from app.services.ai import GeminiService, TrainingService

router = APIRouter(prefix="/api/v1", tags=["training"])


def get_training_service(
    db: AsyncSession = Depends(db_session),
    gemini: GeminiService = Depends(GeminiService),
) -> TrainingService:
    return TrainingService(
        review_repo=ReviewRepository(db),
        auth_repo=AuthRepository(db),
        training_plan_repo=TrainingPlanRepository(db),
        gemini=gemini,
    )


@router.get(
    "/training-plans/",
    response_model=TrainingPlanListResponse,
    response_model_by_alias=True,
)
async def list_training_plans(
    user: AuthUser = Depends(get_current_user),
    service: TrainingService = Depends(get_training_service),
) -> TrainingPlanListResponse:
    plans = await service.list_plans(user)
    return TrainingPlanListResponse(
        items=[TrainingPlanResponse.model_validate(p) for p in plans],
        total=len(plans),
    )


@router.post(
    "/training-plans/",
    response_model=TrainingPlanResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_AI)
async def generate_training_plan(
    request: Request,
    payload: GenerateTrainingPlanRequest,
    user: AuthUser = Depends(get_current_user),
    service: TrainingService = Depends(get_training_service),
    arq_pool: ArqRedis = Depends(get_arq_pool),
) -> TrainingPlanResponse:
    plan = await service.enqueue_training_plan(payload.review_id, user)
    try:
        await arq_pool.enqueue_job("process_training_plan_task", str(plan.id))
    except Exception:
        logger.exception("Failed to enqueue training plan %s", plan.id)
        plan = await service.training_plan_repo.mark_failed(plan.id, ENQUEUE_FAILED_MESSAGE)
    return TrainingPlanResponse.model_validate(plan)


@router.post(
    "/training-plans/{plan_id}/retry",
    response_model=TrainingPlanResponse,
    response_model_by_alias=True,
    status_code=status.HTTP_202_ACCEPTED,
)
@limiter.limit(lambda: get_settings().RATE_LIMIT_AI)
async def retry_training_plan(
    request: Request,
    plan_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: TrainingService = Depends(get_training_service),
    arq_pool: ArqRedis = Depends(get_arq_pool),
) -> TrainingPlanResponse:
    plan = await service.retry_training_plan(plan_id, user)
    try:
        await arq_pool.enqueue_job("process_training_plan_task", str(plan.id))
    except Exception:
        logger.exception("Failed to enqueue training plan %s", plan.id)
        plan = await service.training_plan_repo.mark_failed(plan.id, ENQUEUE_FAILED_MESSAGE)
    return TrainingPlanResponse.model_validate(plan)


@router.get(
    "/training-plans/{plan_id}",
    response_model=TrainingPlanResponse,
    response_model_by_alias=True,
)
async def get_training_plan(
    plan_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: TrainingService = Depends(get_training_service),
) -> TrainingPlanResponse:
    plan = await service.get_plan_by_id(plan_id, user)
    return TrainingPlanResponse.model_validate(plan)


@router.get(
    "/reviews/{review_id}/training-plan",
    response_model=TrainingPlanResponse,
    response_model_by_alias=True,
)
async def get_training_plan_for_review(
    review_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: TrainingService = Depends(get_training_service),
) -> TrainingPlanResponse:
    plan = await service.get_plan_by_review_id(review_id, user)
    return TrainingPlanResponse.model_validate(plan)


@router.get(
    "/workouts/{workout_id}",
    response_model=WorkoutResponse,
    response_model_by_alias=True,
)
async def get_workout(
    workout_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: TrainingService = Depends(get_training_service),
) -> WorkoutResponse:
    workout = await service.get_workout_by_id(workout_id, user)
    return WorkoutResponse.model_validate(workout)
