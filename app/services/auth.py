from app.core.config import get_settings
from app.core.security.jwt import AuthUser
from app.models.profile import Profile
from app.repositories.auth import AuthRepository
from app.schemas.auth import ProfileUpdate


class AuthService:
    def __init__(self, repo: AuthRepository) -> None:
        self.repo = repo
        self.settings = get_settings()

    async def get_or_create_profile(self, user: AuthUser) -> Profile:
        if self.settings.is_development:
            await self.repo.ensure_dev_auth_user(user.id, user.email)

        profile = await self.repo.get_profile(user.id)
        if profile is None:
            profile = await self.repo.create_profile(user.id)
        return profile

    async def update_profile(self, user: AuthUser, payload: ProfileUpdate) -> Profile:
        profile = await self.get_or_create_profile(user)
        fields = payload.model_dump(exclude_unset=True)
        if not fields:
            return profile
        return await self.repo.update_profile(profile, fields)
