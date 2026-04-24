from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.profile import Profile


class AuthRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_profile(self, user_id: UUID) -> Profile | None:
        result = await self.db.execute(select(Profile).where(Profile.id == user_id))
        return result.scalar_one_or_none()

    async def create_profile(
        self,
        user_id: UUID,
        *,
        surf_level: str = "beginner",
    ) -> Profile:
        profile = Profile(id=user_id, surf_level=surf_level)
        self.db.add(profile)
        await self.db.commit()
        await self.db.refresh(profile)
        return profile

    async def update_profile(self, profile: Profile, fields: dict) -> Profile:
        for key, value in fields.items():
            setattr(profile, key, value)
        await self.db.commit()
        await self.db.refresh(profile)
        return profile

    async def ensure_dev_auth_user(self, user_id: UUID, email: str) -> None:
        """Dev-only: insert a row into the shim `auth.users` so the FK resolves.

        In production, Supabase owns `auth.users` and this is a no-op at the call site.
        Safe to call repeatedly — `ON CONFLICT DO NOTHING`.
        """
        await self.db.execute(
            text(
                "INSERT INTO auth.users (id, email) VALUES (:id, :email) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": str(user_id), "email": email},
        )
        await self.db.commit()
