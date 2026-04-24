from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.media import Media


class MediaRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        session_id: UUID,
        media_type: str,
        storage_url: str,
        file_name: str,
        file_size_bytes: int | None,
        duration_seconds: Decimal | None,
    ) -> Media:
        media = Media(
            session_id=session_id,
            media_type=media_type,
            storage_url=storage_url,
            file_name=file_name,
            file_size_bytes=file_size_bytes,
            duration_seconds=duration_seconds,
        )
        self.db.add(media)
        await self.db.commit()
        await self.db.refresh(media)
        return media

    async def get(self, media_id: UUID) -> Media | None:
        result = await self.db.execute(select(Media).where(Media.id == media_id))
        return result.scalar_one_or_none()

    async def list_for_session(self, session_id: UUID) -> list[Media]:
        result = await self.db.execute(
            select(Media).where(Media.session_id == session_id).order_by(Media.created_at.asc())
        )
        return list(result.scalars().all())

    async def delete(self, media: Media) -> None:
        await self.db.delete(media)
        await self.db.commit()
