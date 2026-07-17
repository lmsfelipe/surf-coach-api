# SPEC — Observability: Structured Logging & Error Tracking

**Status:** Proposed
**Owner:** Backend
**Date:** 2026-07-14

---

## 1. Problem

The API uses Python's `logging` module throughout the codebase, but:

- **No logging configuration exists.** The root logger uses Python's default
  formatter (plain text, no request context, no timestamp in ISO format). In
  production this makes logs hard to parse, filter, and correlate.
- **No error tracking service.** When a `502` from Gemini or an unhandled
  exception occurs in production, there is no alert — the error is written to
  stdout (if captured at all) and lost.
- **No request correlation.** There is no way to trace all log lines belonging
  to a single request or user.

## 2. Goals

- **Structured JSON logs** in production, human-readable logs in development.
- **Request context** in every log line: request ID, user ID, method, path.
- **Error tracking** via Sentry — automatic capture of unhandled exceptions
  and 5xx responses with full context.
- **Performance tracing** for slow endpoints (Sentry transactions).

## 3. Non-goals

- Full APM / distributed tracing (OpenTelemetry — future).
- Custom metrics / Prometheus endpoint (future).
- Log aggregation infrastructure (ELK, Datadog — deployment concern).

---

## 4. Design

### 4.1 Structured logging

Use [`structlog`](https://www.structlog.org/) for structured, context-rich
logging. It integrates well with Python's stdlib `logging` and produces JSON
output in production.

#### Configuration (`app/core/logging.py`)

```python
import logging
import structlog
from app.core.config import get_settings

def setup_logging() -> None:
    settings = get_settings()
    is_dev = settings.is_development

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer() if is_dev
                else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, settings.LOG_LEVEL.upper()),
    )
```

Call `setup_logging()` in `create_app()` before anything else.

### 4.2 Request context middleware

Add a middleware that:

1. Generates a unique **request ID** (or reads `X-Request-ID` from the client).
2. Binds `request_id`, `user_id`, `method`, `path` to structlog's contextvars.
3. Logs request start and completion (with status code and duration).

```python
# app/core/middleware.py
import time
import uuid
import structlog
from starlette.middleware.base import BaseHTTPMiddleware

class RequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        request_id = request.headers.get("X-Request-ID", str(uuid.uuid4()))
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        )
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = round((time.perf_counter() - start) * 1000, 1)
        logger = structlog.get_logger()
        logger.info("request_completed", status=response.status_code, duration_ms=duration_ms)
        response.headers["X-Request-ID"] = request_id
        return response
```

### 4.3 Sentry integration

[`sentry-sdk`](https://docs.sentry.io/platforms/python/integrations/fastapi/)
with the FastAPI integration captures:

- Unhandled exceptions (auto)
- 5xx responses from `AppError` subclasses (manual `capture_exception`)
- Performance transactions for every request

#### Setup

```python
# in create_app()
import sentry_sdk
from sentry_sdk.integrations.fastapi import FastApiIntegration
from sentry_sdk.integrations.sqlalchemy import SqlalchemyIntegration

if not settings.is_development and settings.SENTRY_DSN:
    sentry_sdk.init(
        dsn=settings.SENTRY_DSN,
        environment=settings.APP_ENV,
        traces_sample_rate=0.2,  # 20% of requests traced
        integrations=[FastApiIntegration(), SqlalchemyIntegration()],
    )
```

#### New config fields

```python
SENTRY_DSN: str = Field(default="", description="Sentry DSN (empty = disabled)")
```

### 4.4 Enhanced error handler

Update the 5xx handler in `app/core/errors.py` to report to Sentry:

```python
@app.exception_handler(AppError)
async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
    if exc.status_code >= 500:
        logger.exception("AppError 5xx: %s", exc.code)
        sentry_sdk.capture_exception(exc)
    return JSONResponse(...)
```

The generic `Exception` handler already logs — add `sentry_sdk.capture_exception`
there too (Sentry's auto-capture should handle it, but explicit is safer).

---

## 5. Log output examples

**Development (console):**
```
2026-07-14T12:00:01Z [info] request_completed  request_id=abc-123 method=POST path=/api/v1/reviews/ status=202 duration_ms=45.2
2026-07-14T12:00:02Z [info] review_job_enqueued request_id=abc-123 user_id=usr-456 session_id=sess-789
```

**Production (JSON):**
```json
{"event": "request_completed", "request_id": "abc-123", "method": "POST", "path": "/api/v1/reviews/", "status": 202, "duration_ms": 45.2, "timestamp": "2026-07-14T12:00:01Z", "level": "info"}
```

## 6. New dependencies

```toml
# pyproject.toml
"structlog>=24.0",
"sentry-sdk[fastapi]>=2.0",
```

## 7. Acceptance criteria

- [ ] All log output in production is valid JSON, parseable by any log aggregator.
- [ ] Every log line includes `request_id`, `method`, `path`.
- [ ] Authenticated requests also include `user_id` in log context.
- [ ] `X-Request-ID` response header is present on every response.
- [ ] Unhandled exceptions appear in Sentry within 30 s.
- [ ] 5xx `AppError` responses are reported to Sentry.
- [ ] `SENTRY_DSN` empty or unset → Sentry is disabled, no errors on startup.
- [ ] Development mode shows human-readable colored console output.
- [ ] Request duration is logged for every completed request.

## 8. Future enhancements (out of scope)

- OpenTelemetry traces for distributed tracing across services.
- Prometheus metrics endpoint (`/metrics`) for Grafana dashboards.
- Alerting rules (e.g., Sentry alert on >5% error rate).
- User-facing request ID (return in error responses for support tickets).
