from uuid import UUID

from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import db_session, get_current_user
from app.core.security.jwt import AuthUser
from app.repositories.sessions import SessionsRepository
from app.schemas.sessions import SessionCreate, SessionOut
from app.services.sessions import SessionsService

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


def get_sessions_service(db: AsyncSession = Depends(db_session)) -> SessionsService:
    return SessionsService(SessionsRepository(db))


@router.post(
    "/",
    response_model=SessionOut,
    response_model_by_alias=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_session(
    payload: SessionCreate,
    user: AuthUser = Depends(get_current_user),
    service: SessionsService = Depends(get_sessions_service),
) -> SessionOut:
    session = await service.create_session(payload, user)
    return SessionOut.model_validate(session)


@router.get("/", response_model=list[SessionOut], response_model_by_alias=True)
async def list_sessions(
    user: AuthUser = Depends(get_current_user),
    service: SessionsService = Depends(get_sessions_service),
) -> list[SessionOut]:
    sessions = await service.list_sessions(user)
    return [SessionOut.model_validate(s) for s in sessions]


@router.get(
    "/{session_id}",
    response_model=SessionOut,
    response_model_by_alias=True,
)
async def get_session(
    session_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: SessionsService = Depends(get_sessions_service),
) -> SessionOut:
    session = await service.get_session(session_id, user)
    return SessionOut.model_validate(session)


@router.delete("/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_session(
    session_id: UUID,
    user: AuthUser = Depends(get_current_user),
    service: SessionsService = Depends(get_sessions_service),
) -> None:
    await service.delete_session(session_id, user)
