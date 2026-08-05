from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy import insert, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.media import Media

if TYPE_CHECKING:
    from app.services.media import _PreparedMedia


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

    async def create_many(self, *, session_id: UUID, items: list[_PreparedMedia]) -> list[Media]:
        """Insert N media rows in one round-trip, preserving input order.

        Replaces N × (add + commit + refresh) with a single INSERT … RETURNING.
        ``sort_by_parameter_order=True`` is REQUIRED: without it, RETURNING with
        executemany does not guarantee returned-row order matches input order,
        which would scramble ``succeeded[]`` vs upload order.
        """
        if not items:
            return []
        result = await self.db.execute(
            insert(Media).returning(Media, sort_by_parameter_order=True),
            [
                {
                    "session_id": session_id,
                    "media_type": it.media_type,
                    "storage_url": it.storage_url,
                    "file_name": it.file_name,
                    "file_size_bytes": it.file_size_bytes,
                    "duration_seconds": it.duration_seconds,
                }
                for it in items
            ],
        )
        await self.db.commit()
        return list(result.scalars().all())

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

    async def mark_optimized(self, media_id: UUID, size_bytes: int) -> None:
        await self.db.execute(
            text(
                "UPDATE public.media "
                "SET optimized_at = now(), file_size_bytes = :size "
                "WHERE id = :id"
            ),
            {"id": str(media_id), "size": size_bytes},
        )
        await self.db.commit()

    async def increment_optimize_attempts(self, media_id: UUID) -> None:
        """Record one failed optimization attempt so the sweep can eventually give up."""
        await self.db.execute(
            text(
                "UPDATE public.media "
                "SET optimize_attempts = optimize_attempts + 1 "
                "WHERE id = :id"
            ),
            {"id": str(media_id)},
        )
        await self.db.commit()

    async def list_unoptimized_videos(
        self, older_than_sec: int, limit: int, max_attempts: int
    ) -> list[Media]:
        result = await self.db.execute(
            select(Media)
            .where(
                Media.media_type == "video",
                Media.optimized_at.is_(None),
                Media.optimize_attempts < max_attempts,
                Media.created_at < text("now() - make_interval(secs => :older_than_sec)"),
            )
            .order_by(Media.created_at)
            .limit(limit),
            {"older_than_sec": older_than_sec},
        )
        return list(result.scalars().all())
