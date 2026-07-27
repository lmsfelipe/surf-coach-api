import time
import uuid

import structlog
from starlette.datastructures import Headers
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.core.config import get_settings
from app.core.errors import RequestTooLargeError
from app.core.security.jwt import verify_supabase_jwt

BODY_METHODS = frozenset({"POST", "PUT", "PATCH"})


class BodySizeLimitMiddleware:
    """Reject oversized request bodies before anything downstream buffers them.

    Written as raw ASGI rather than ``BaseHTTPMiddleware`` for two reasons: it
    can answer on the ``Content-Length`` header without the body ever being
    pulled off the wire, and it can wrap ``receive`` to enforce the same cap on
    chunked bodies that declare no length. Both matter because FastAPI parses
    the whole multipart body before the route — and therefore before any
    dependency or route-level validation — gets a say.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("method") not in BODY_METHODS:
            await self.app(scope, receive, send)
            return

        limit = get_settings().max_request_body_bytes

        declared = Headers(scope=scope).get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await self._reject(scope, receive, send, limit)
            return

        received = 0
        exceeded = False
        forwarded = False

        async def limited_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    # Signal a disconnect rather than raising: FastAPI turns any
                    # exception escaping the body read into a bare 400, so an
                    # exception would never make it back up to here.
                    exceeded = True
                    return {"type": "http.disconnect"}
            return message

        async def limited_send(message: Message) -> None:
            nonlocal forwarded
            if exceeded and not forwarded:
                # Drop whatever the app made of the truncated body; the 413 below
                # is the real answer. Once any byte has gone out we must not
                # interfere, so `forwarded` latches the normal path open.
                return
            forwarded = True
            await send(message)

        await self.app(scope, limited_receive, limited_send)

        if exceeded and not forwarded:
            await self._reject(scope, receive, send, limit)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, limit: int) -> None:
        err = RequestTooLargeError()
        response = JSONResponse(
            status_code=err.status_code,
            content={
                "error": {
                    "code": err.code,
                    "message": err.message,
                    "details": {"maxBodyBytes": limit},
                }
            },
        )
        await response(scope, receive, send)


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
