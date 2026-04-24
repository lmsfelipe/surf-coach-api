from fastapi import FastAPI
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.api import ai, auth, health, media, reviews, sessions
from app.core.config import get_settings
from app.core.errors import register_exception_handlers


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(
        title="Surf Coach API",
        version="0.1.0",
        description="Surf Coaching Platform — Phase 1 (auth + profile).",
    )

    allowed_hosts = ["*"] if settings.is_development else ["localhost", "127.0.0.1"]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=allowed_hosts)

    @app.middleware("http")
    async def _security_headers(request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        return response

    register_exception_handlers(app)

    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(sessions.router)
    app.include_router(media.router)
    app.include_router(reviews.router)
    app.include_router(ai.router)

    return app


app = create_app()
