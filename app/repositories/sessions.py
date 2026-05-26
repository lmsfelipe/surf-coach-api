from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.session import Session


class SessionsRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        profile_id: UUID,
        session_date: date,
        location: str,
        wave_size: float,
        surfboard_id: UUID | None,
        notes: str | None,
    ) -> Session:
        session = Session(
            profile_id=profile_id,
            session_date=session_date,
            location=location,
            wave_size=wave_size,
            surfboard_id=surfboard_id,
            notes=notes,
        )
        self.db.add(session)
        await self.db.commit()
        await self.db.refresh(session)
        return session

    async def get(self, session_id: UUID) -> Session | None:
        result = await self.db.execute(select(Session).where(Session.id == session_id))
        return result.scalar_one_or_none()

    async def list_for_profile(self, profile_id: UUID) -> list[Session]:
        result = await self.db.execute(
            select(Session)
            .where(Session.profile_id == profile_id)
            .order_by(Session.session_date.desc())
        )
        return list(result.scalars().all())

    async def delete(self, session: Session) -> None:
        await self.db.delete(session)
        await self.db.commit()
