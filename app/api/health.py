import time

from fastapi import APIRouter, Depends, status
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import db_session
from app.core.rate_limit import limiter

router = APIRouter(tags=["health"])

DB_CHECK_TIMEOUT = 3.0


# Exempt from RATE_LIMIT_DEFAULT: the platform polls these continuously and a
# throttled health check would read as an outage.
@router.get("/health/live")
@limiter.exempt
async def liveness():
    return {"status": "healthy"}


@router.get("/health/ready")
@router.get("/health")
@limiter.exempt
async def readiness(db: AsyncSession = Depends(db_session)):
    checks = {}
    overall = "healthy"

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
