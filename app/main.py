import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sentry_sdk
import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import ai, auth, health, media, reviews, sessions, surfboards
from app.core.config import get_settings
from app.core.db import engine
from app.core.errors import register_exception_handlers
from app.core.jobs import JobRunner
from app.core.logging import setup_logging
from app.core.middleware import BodySizeLimitMiddleware, RequestContextMiddleware
from app.core.rate_limit import limiter, rate_limit_exceeded_handler
from app.jobs import sweep_stuck_jobs

logger = structlog.get_logger(__name__)

# The sweep is two indexed UPDATEs. Capped so a slow or unreachable database
# delays boot (and the first request after a cold start) by seconds, not minutes.
STARTUP_SWEEP_TIMEOUT_SEC = 10.0


async def _sweep_on_startup() -> None:
    """Fail rows a hard-killed process left in 'processing'.

    A container killed with SIGKILL (OOM, host crash) runs no handler, so its
    rows are only cleaned up here — and Railway always restarts it. Never blocks
    boot: readiness reports the database separately, and the read-path staleness
    check covers these rows in the meantime.
    """
    try:
        async with asyncio.timeout(STARTUP_SWEEP_TIMEOUT_SEC):
            await sweep_stuck_jobs()
    except Exception:
        logger.exception("Startup sweep failed; continuing without it")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.job_runner = JobRunner(
        max_concurrent=settings.JOBS_MAX_CONCURRENT,
        max_queued=settings.JOBS_MAX_QUEUED,
        max_wait_sec=settings.JOB_MAX_WAIT_SEC,
        timeout_sec=settings.JOB_TIMEOUT_SEC,
        shutdown_grace_sec=settings.JOB_SHUTDOWN_GRACE_SEC,
    )
    await _sweep_on_startup()
    yield
    # Drain the jobs before the DB pool goes away: a cancelled job still needs a
    # connection to mark its row failed.
    await app.state.job_runner.close()
    await engine.dispose()


def create_app() -> FastAPI:
    settings = get_settings()

    setup_logging()

    if not settings.is_development and settings.SENTRY_DSN:
        sentry_sdk.init(
            dsn=settings.SENTRY_DSN,
            environment=settings.APP_ENV,
            traces_sample_rate=0.2,
            integrations=[FastApiIntegration(), SqlalchemyIntegration()],
        )

    app = FastAPI(
        title="Surf Coach API",
        version="0.1.0",
        description="Surf Coaching Platform — Phase 1 (auth + profile).",
        lifespan=lifespan,
    )

    # In prod, restrict Host headers to the configured allow-list; fall back to
    # permissive "*" when unset so a missing env var can't silently 400 every
    # request (host control then relies on the upstream proxy).
    if settings.is_development:
        allowed_hosts = ["*"]
    else:
        allowed_hosts = settings.ALLOWED_HOSTS or ["*"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    if settings.CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.CORS_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    @app.middleware("http")
    async def _security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    register_exception_handlers(app)

    limiter.enabled = settings.RATE_LIMIT_ENABLED
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    # Added last, so it sits just inside RequestContextMiddleware: rejections are
    # still logged with a request id, but the cap is applied before the body
    # reaches the multipart parser.
    app.add_middleware(BodySizeLimitMiddleware)

    app.add_middleware(RequestContextMiddleware)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(sessions.router)
    app.include_router(media.router)
    app.include_router(reviews.router)
    app.include_router(ai.router)
    app.include_router(surfboards.router)

    return app


app = create_app()
