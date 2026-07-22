"""Rate limiting setup using slowapi."""

from __future__ import annotations

from fastapi import Request, status
from fastapi.responses import JSONResponse
from slowapi import Limiter
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address


def _get_user_id_or_ip(request: Request) -> str:
    """Extract the authenticated user ID from the JWT, falling back to IP.

    slowapi calls key_func(request) — we parse the Authorization header the
    same way ``get_current_user`` does, but without raising on failure so that
    unauthenticated requests still get IP-based limiting.
    """
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() == "bearer" and token:
        try:
            from app.core.security.jwt import verify_supabase_jwt

            user = verify_supabase_jwt(token)
            return str(user.id)
        except Exception:
            pass
    return get_remote_address(request)


limiter = Limiter(
    key_func=_get_user_id_or_ip,
    default_limits=[],
)


async def rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded) -> JSONResponse:
    """Return a 429 response in the project's standard error envelope."""
    retry_after_secs = getattr(exc, "retry_after", None)
    headers: dict[str, str] = {}
    if retry_after_secs is not None:
        headers["Retry-After"] = str(retry_after_secs)

    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={
            "error": {
                "code": "RATE_LIMIT_EXCEEDED",
                "message": "Too many requests. Please try again later.",
                "details": {"retryAfter": retry_after_secs},
            }
        },
        headers=headers,
    )
