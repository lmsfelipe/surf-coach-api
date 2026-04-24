from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import db_session, get_current_user
from app.core.security.jwt import AuthUser
from app.repositories.auth import AuthRepository
from app.schemas.auth import ProfileOut, ProfileUpdate
from app.services.auth import AuthService

router = APIRouter(tags=["auth"])


def get_auth_service(db: AsyncSession = Depends(db_session)) -> AuthService:
    return AuthService(AuthRepository(db))


def _to_out(user: AuthUser, profile) -> ProfileOut:
    return ProfileOut(
        id=profile.id,
        email=user.email,
        surf_level=profile.surf_level,
        height_cm=profile.height_cm,
        weight_kg=profile.weight_kg,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


@router.get("/me", response_model=ProfileOut, response_model_by_alias=True)
async def get_me(
    user: AuthUser = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
) -> ProfileOut:
    profile = await service.get_or_create_profile(user)
    return _to_out(user, profile)


@router.patch("/me", response_model=ProfileOut, response_model_by_alias=True)
async def patch_me(
    payload: ProfileUpdate,
    user: AuthUser = Depends(get_current_user),
    service: AuthService = Depends(get_auth_service),
) -> ProfileOut:
    profile = await service.update_profile(user, payload)
    return _to_out(user, profile)
