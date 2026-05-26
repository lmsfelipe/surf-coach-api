from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import db_session, get_current_user
from app.core.security.jwt import AuthUser
from app.repositories.surfboards import SurfboardRepository
from app.schemas.surfboard import SurfboardCreate, SurfboardOut, SurfboardUpdate
from app.services.surfboards import SurfboardService

router = APIRouter(prefix="/api/v1/surfboards", tags=["surfboards"])


def get_surfboard_service(db: AsyncSession = Depends(db_session)) -> SurfboardService:
    return SurfboardService(SurfboardRepository(db))


@router.get("/", response_model=list[SurfboardOut], response_model_by_alias=True)
async def list_surfboards(
    user: AuthUser = Depends(get_current_user),
    service: SurfboardService = Depends(get_surfboard_service),
) -> list[SurfboardOut]:
    boards = await service.list_boards(user)
    return [SurfboardOut.model_validate(b) for b in boards]


@router.post(
    "/",
    response_model=SurfboardOut,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_surfboard(
    payload: SurfboardCreate,
    user: AuthUser = Depends(get_current_user),
    service: SurfboardService = Depends(get_surfboard_service),
) -> SurfboardOut:
    board = await service.create_board(payload, user)
    return SurfboardOut.model_validate(board)


@router.get("/{surfboard_id}", response_model=SurfboardOut, response_model_by_alias=True)
async def get_surfboard(
    surfboard_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: SurfboardService = Depends(get_surfboard_service),
) -> SurfboardOut:
    board = await service.get_board(surfboard_id, user)
    return SurfboardOut.model_validate(board)


@router.patch("/{surfboard_id}", response_model=SurfboardOut, response_model_by_alias=True)
async def update_surfboard(
    surfboard_id: UUID,
    payload: SurfboardUpdate,
    user: AuthUser = Depends(get_current_user),
    service: SurfboardService = Depends(get_surfboard_service),
) -> SurfboardOut:
    board = await service.update_board(surfboard_id, payload, user)
    return SurfboardOut.model_validate(board)


@router.delete("/{surfboard_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_surfboard(
    surfboard_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: SurfboardService = Depends(get_surfboard_service),
) -> None:
    await service.delete_board(surfboard_id, user)
