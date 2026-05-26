from uuid import UUID

from app.core.errors import SurfboardForbiddenError, SurfboardNotFoundError
from app.core.security.jwt import AuthUser
from app.models.surfboard import Surfboard
from app.repositories.surfboards import SurfboardRepository
from app.schemas.surfboard import SurfboardCreate, SurfboardUpdate


class SurfboardService:
    def __init__(self, repo: SurfboardRepository) -> None:
        self.repo = repo

    async def list_boards(self, user: AuthUser) -> list[Surfboard]:
        return await self.repo.get_all_by_profile(user.id)

    async def get_board(self, surfboard_id: UUID, user: AuthUser) -> Surfboard:
        board = await self.repo.get_by_id(surfboard_id)
        if board is None:
            raise SurfboardNotFoundError()
        if board.profile_id != user.id:
            raise SurfboardForbiddenError()
        return board

    async def create_board(self, payload: SurfboardCreate, user: AuthUser) -> Surfboard:
        return await self.repo.create(
            profile_id=user.id,
            board_type=payload.board_type,
            board_size=payload.board_size,
            volume=payload.volume,
            label=payload.label,
        )

    async def update_board(
        self, surfboard_id: UUID, payload: SurfboardUpdate, user: AuthUser
    ) -> Surfboard:
        board = await self.get_board(surfboard_id, user)
        fields = payload.model_dump(exclude_unset=True)
        if not fields:
            return board
        return await self.repo.update(board, fields)

    async def delete_board(self, surfboard_id: UUID, user: AuthUser) -> None:
        board = await self.get_board(surfboard_id, user)
        await self.repo.delete(board)
