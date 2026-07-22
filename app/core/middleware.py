import time
import uuid

import structlog
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.core.security.jwt import verify_supabase_jwt


class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))

        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )

        # Best-effort user_id binding from Authorization header
        auth = request.headers.get("authorization", "")
        if auth.lower().startswith("bearer "):
            try:
                user = verify_supabase_jwt(auth.split(" ", 1)[1])
                structlog.contextvars.bind_contextvars(user_id=str(user.id))
            except Exception:
                pass  # unauthenticated or invalid — skip

        logger = structlog.get_logger()

        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)

        logger.info("request_completed", status=response.status_code, duration_ms=duration_ms)
        response.headers["X-Request-ID"] = request_id
        return response
