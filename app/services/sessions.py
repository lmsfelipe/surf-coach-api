from uuid import UUID

from app.core.errors import ForbiddenError, NotFoundError, SurfboardForbiddenError, SurfboardNotFoundError
from app.core.security.jwt import AuthUser
from app.models.session import Session
from app.repositories.sessions import SessionsRepository
from app.repositories.surfboards import SurfboardRepository
from app.schemas.sessions import SessionCreate


class SessionsService:
    def __init__(
        self,
        repo: SessionsRepository,
        surfboard_repo: SurfboardRepository | None = None,
    ) -> None:
        self.repo = repo
        self.surfboard_repo = surfboard_repo

    async def create_session(self, payload: SessionCreate, user: AuthUser) -> Session:
        if payload.surfboard_id is not None and self.surfboard_repo is not None:
            board = await self.surfboard_repo.get_by_id(payload.surfboard_id)
            if board is None:
                raise SurfboardNotFoundError()
            if board.profile_id != user.id:
                raise SurfboardForbiddenError()

        return await self.repo.create(
            profile_id=user.id,
            session_date=payload.session_date,
            location=payload.location,
            wave_size=payload.wave_size,
            surfboard_id=payload.surfboard_id,
            notes=payload.notes,
        )

    async def list_sessions(self, user: AuthUser) -> list[Session]:
        return await self.repo.list_for_profile(user.id)

    async def get_session(self, session_id: UUID, user: AuthUser) -> Session:
        session = await self.repo.get(session_id)
        if session is None:
            raise NotFoundError("Session not found.")
        if session.profile_id != user.id:
            raise ForbiddenError()
        return session

    async def delete_session(self, session_id: UUID, user: AuthUser) -> None:
        session = await self.get_session(session_id, user)
        await self.repo.delete(session)
