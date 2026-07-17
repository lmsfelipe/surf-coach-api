# SPEC — API Rate Limiting

**Status:** Proposed
**Owner:** Backend
**Date:** 2026-07-14

---

## 1. Problem

The API has no rate limiting. Every AI-powered endpoint (`POST /api/v1/reviews/`,
`POST /api/v1/ai/analyze`, `POST /api/v1/ai/training-plans/`) calls the Gemini
API, which has a real per-request cost. Additionally, the media upload endpoint
writes to Supabase Storage.

Without limits:

- A single user (or leaked token) can trigger hundreds of Gemini calls in
  minutes, running up the bill.
- A burst of uploads can exhaust Supabase Storage quotas.
- A malicious actor can degrade service for all users via resource exhaustion.

## 2. Goals

- Per-user rate limits on expensive endpoints (AI + uploads).
- Global rate limits on auth endpoints to prevent brute-force attacks.
- Limits return standard `429 Too Many Requests` with a `Retry-After` header.
- Configurable via environment variables so limits can be tuned without
  redeployment.

## 3. Non-goals

- IP-based rate limiting (complex with proxies/NAT; user-based is sufficient
  for authenticated endpoints).
- Distributed rate limiting across multiple API instances (can be added later
  with Redis backend).
- Billing-tier-based limits (future feature).

---

## 4. Design

### 4.1 Library — `slowapi`

[`slowapi`](https://github.com/laurentS/slowapi) is the standard rate-limiting
library for FastAPI/Starlette. It wraps `limits` and supports in-memory or
Redis-backed storage.

### 4.2 Rate limit tiers

| Endpoint pattern | Limit | Key | Rationale |
|---|---|---|---|
| `POST /api/v1/reviews/` | **5/hour** per user | `user.id` | Each call triggers Gemini + frame extraction |
| `POST /api/v1/ai/training-plans/` | **5/hour** per user | `user.id` | Each call triggers Gemini |
| `POST /api/v1/ai/analyze` | **10/hour** per user | `user.id` | Direct AI analysis |
| `POST /api/v1/sessions/*/media/` | **30/hour** per user | `user.id` | Storage writes + moderation |
| `POST /api/v1/auth/login` | **10/minute** per IP | IP | Brute-force protection |
| `POST /api/v1/auth/register` | **5/hour** per IP | IP | Spam account prevention |
| All other endpoints | **120/minute** per user | `user.id` | General abuse prevention |

### 4.3 Configuration

New settings in `app/core/config.py`:

```python
RATE_LIMIT_AI: str = Field(default="5/hour", description="Rate limit for AI endpoints per user")
RATE_LIMIT_UPLOAD: str = Field(default="30/hour", description="Rate limit for upload endpoints per user")
RATE_LIMIT_AUTH: str = Field(default="10/minute", description="Rate limit for auth endpoints per IP")
RATE_LIMIT_DEFAULT: str = Field(default="120/minute", description="Default rate limit per user")
```

### 4.4 Response format

Rate-limited requests receive:

```
HTTP/1.1 429 Too Many Requests
Retry-After: 3600
Content-Type: application/json

{
  "error": {
    "code": "RATE_LIMIT_EXCEEDED",
    "message": "Too many requests. Please try again later.",
    "details": {
      "retryAfter": 3600
    }
  }
}
```

### 4.5 Response headers (all requests)

Include standard rate limit headers on every response:

```
X-RateLimit-Limit: 5
X-RateLimit-Remaining: 3
X-RateLimit-Reset: 1720972800
```

### 4.6 Storage backend

- **Phase 1 (now):** In-memory storage. Simple, no extra infra. Limits reset
  on server restart, which is acceptable at current scale.
- **Phase 2 (with async review work):** Redis-backed storage. Shared across
  workers and survives restarts. Use the same Redis instance added for `arq`.

---

## 5. Implementation outline

```python
# app/core/rate_limit.py
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(key_func=get_remote_address)
```

Applied per-router:

```python
@router.post("/reviews/")
@limiter.limit("5/hour", key_func=lambda request: get_current_user_id(request))
async def create_review(request: Request, ...):
    ...
```

Register the limiter in `create_app()`:

```python
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
```

Override the default `_rate_limit_exceeded_handler` to return the standard
error envelope format.

## 6. Acceptance criteria

- [ ] `POST /reviews/` returns `429` after 5 calls in one hour by the same user.
- [ ] `429` response includes `Retry-After` header and error envelope.
- [ ] `X-RateLimit-*` headers are present on all responses.
- [ ] Auth endpoints are rate-limited by IP.
- [ ] Rate limits are configurable via environment variables.
- [ ] Rate limiting does not break existing tests (test client bypasses or
      uses high limits).

## 7. Future enhancements (out of scope)

- Redis-backed distributed rate limiting.
- Per-tier limits (free vs. paid users).
- Rate limit dashboard / alerting.
- Gradual backoff (warn at 80% of limit).
