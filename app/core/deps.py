from collections.abc import AsyncIterator

from arq import ArqRedis, create_pool
from arq.connections import RedisSettings
from fastapi import Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.core.db import get_db
from app.core.errors import MissingTokenError
from app.core.security.jwt import AuthUser, verify_supabase_jwt

_arq_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    global _arq_pool
    if _arq_pool is None:
        settings = get_settings()
        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.REDIS_URL))
    return _arq_pool


async def get_current_user(
    authorization: str | None = Header(default=None),
) -> AuthUser:
    if not authorization:
        raise MissingTokenError()
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise MissingTokenError("Authorization header must use the Bearer scheme.")
    return verify_supabase_jwt(token)


async def db_session() -> AsyncIterator[AsyncSession]:
    async for session in get_db():
        yield session
