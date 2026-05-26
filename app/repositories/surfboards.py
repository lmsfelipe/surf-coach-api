from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.surfboard import Surfboard


class SurfboardRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_all_by_profile(self, profile_id: UUID) -> list[Surfboard]:
        result = await self.db.execute(
            select(Surfboard)
            .where(Surfboard.profile_id == profile_id)
            .order_by(Surfboard.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_by_id(self, surfboard_id: UUID) -> Surfboard | None:
        result = await self.db.execute(
            select(Surfboard).where(Surfboard.id == surfboard_id)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        profile_id: UUID,
        board_type: str,
        board_size: float,
        volume: float | None,
        label: str | None,
    ) -> Surfboard:
        board = Surfboard(
            profile_id=profile_id,
            board_type=board_type,
            board_size=board_size,
            volume=volume,
            label=label,
        )
        self.db.add(board)
        await self.db.commit()
        await self.db.refresh(board)
        return board

    async def update(self, board: Surfboard, fields: dict) -> Surfboard:
        for key, value in fields.items():
            setattr(board, key, value)
        await self.db.commit()
        await self.db.refresh(board)
        return board

    async def delete(self, board: Surfboard) -> None:
        await self.db.delete(board)
        await self.db.commit()
