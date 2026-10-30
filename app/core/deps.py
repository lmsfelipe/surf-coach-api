from collections.abc import AsyncIterator

from fastapi import Header, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.errors import MissingTokenError
from app.core.jobs import JobRunner
from app.core.security.jwt import AuthUser, verify_supabase_jwt


def get_job_runner(request: Request) -> JobRunner:
    """The process-wide runner built in the lifespan (app/main.py)."""
    return request.app.state.job_runner


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
