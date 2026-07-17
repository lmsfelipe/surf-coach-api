# SPEC — Deep Health Check

**Status:** Proposed
**Owner:** Backend
**Date:** 2026-07-14

---

## 1. Problem

The current health endpoint (`GET /health`) returns `{"status": "ok"}`
unconditionally. It does not verify that the application can actually serve
requests — the database could be unreachable, and the health check would still
pass.

This matters because:

- **Container orchestrators** (Docker Compose, ECS, Kubernetes) use health
  checks to decide whether to route traffic to an instance. A shallow check
  means traffic is sent to instances that cannot reach Postgres, resulting in
  5xx errors for users.
- **Load balancers** use health probes for the same purpose.

## 2. Goals

- Health endpoint verifies **database connectivity** by executing a trivial
  query.
- Distinguish between **liveness** (process is running) and **readiness**
  (process can serve requests).
- Response includes component-level status so operators can quickly identify
  which dependency is down.

## 3. Non-goals

- Checking Supabase Storage or Gemini API availability (these are external
  services with their own health — failing a readiness check because Gemini is
  slow would unnecessarily pull instances out of rotation).
- Authentication on health endpoints.

---

## 4. Design

### 4.1 Two endpoints

| Endpoint | Purpose | Checks | Used by |
|---|---|---|---|
| `GET /health/live` | Liveness probe | None (returns 200 if process is running) | Orchestrator liveness probe |
| `GET /health/ready` | Readiness probe | Database `SELECT 1` | Orchestrator readiness probe, load balancer |

Keep the existing `GET /health` as an alias for `/health/ready` for backwards
compatibility.

### 4.2 Response format

**Healthy:**
```json
{
  "status": "healthy",
  "checks": {
    "database": { "status": "healthy", "latencyMs": 2.3 }
  }
}
```

**Unhealthy (HTTP 503):**
```json
{
  "status": "unhealthy",
  "checks": {
    "database": { "status": "unhealthy", "error": "connection refused" }
  }
}
```

### 4.3 Implementation

```python
# app/api/health.py
import time
from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.deps import db_session

router = APIRouter(tags=["health"])

@router.get("/health/live")
async def liveness():
    return {"status": "healthy"}

@router.get("/health/ready")
@router.get("/health")
async def readiness(db: AsyncSession = Depends(db_session)):
    checks = {}
    overall = "healthy"

    # Database check
    try:
        start = time.perf_counter()
        await db.execute(text("SELECT 1"))
        latency = round((time.perf_counter() - start) * 1000, 1)
        checks["database"] = {"status": "healthy", "latencyMs": latency}
    except Exception as e:
        checks["database"] = {"status": "unhealthy", "error": str(e)}
        overall = "unhealthy"

    code = status.HTTP_200_OK if overall == "healthy" else status.HTTP_503_SERVICE_UNAVAILABLE
    return JSONResponse(
        status_code=code,
        content={"status": overall, "checks": checks},
    )
```

### 4.4 Docker Compose healthcheck

```yaml
# docker-compose.yml — api service
healthcheck:
  test: ["CMD", "curl", "-f", "http://localhost:8000/health/ready"]
  interval: 10s
  timeout: 5s
  retries: 3
  start_period: 15s
```

### 4.5 Timeout

The database check should have a short timeout (3 s). If the DB takes longer
than 3 s to respond to `SELECT 1`, the instance should be considered unhealthy.

---

## 5. Acceptance criteria

- [ ] `GET /health/live` returns `200 {"status": "healthy"}` even when the
      database is unreachable.
- [ ] `GET /health/ready` returns `200` with database latency when the database
      is reachable.
- [ ] `GET /health/ready` returns `503` with error details when the database
      is unreachable.
- [ ] `GET /health` behaves identically to `GET /health/ready` (backwards
      compatible).
- [ ] Docker Compose healthcheck uses the readiness endpoint.
- [ ] No authentication required on any health endpoint.

## 6. Future enhancements (out of scope)

- Redis connectivity check (when Redis is added for task queue).
- Startup probe (for slow-starting instances).
- `/health/ready` response cached for 1 s to prevent probe storms.
