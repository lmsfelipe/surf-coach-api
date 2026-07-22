import pytest
from httpx import ASGITransport, AsyncClient

from app.core.deps import db_session
from app.main import app


class FakeDBSession:
    """Simulates a healthy database connection."""

    async def execute(self, stmt):
        return None


class FakeDBSessionUnhealthy:
    """Simulates an unreachable database."""

    async def execute(self, stmt):
        raise ConnectionRefusedError("connection refused")


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _override_db_healthy():
    async def _fake_db():
        yield FakeDBSession()

    app.dependency_overrides[db_session] = _fake_db
    yield
    app.dependency_overrides.pop(db_session, None)


async def test_liveness_returns_200(client):
    async with client as c:
        r = await c.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy"}


async def test_readiness_healthy(client):
    async with client as c:
        r = await c.get("/health/ready")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert body["checks"]["database"]["status"] == "healthy"
    assert "latencyMs" in body["checks"]["database"]


async def test_health_alias_matches_readiness(client):
    async with client as c:
        r = await c.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "healthy"
    assert "database" in body["checks"]


async def test_readiness_unhealthy_returns_503(client):
    async def _unhealthy_db():
        yield FakeDBSessionUnhealthy()

    app.dependency_overrides[db_session] = _unhealthy_db

    async with client as c:
        r = await c.get("/health/ready")
    assert r.status_code == 503
    body = r.json()
    assert body["status"] == "unhealthy"
    assert body["checks"]["database"]["status"] == "unhealthy"
    assert "error" in body["checks"]["database"]


async def test_liveness_works_when_db_unreachable(client):
    async def _unhealthy_db():
        yield FakeDBSessionUnhealthy()

    app.dependency_overrides[db_session] = _unhealthy_db

    async with client as c:
        r = await c.get("/health/live")
    assert r.status_code == 200
    assert r.json() == {"status": "healthy"}


async def test_health_endpoints_require_no_auth(client):
    # Ensure no auth header is needed
    async with client as c:
        live = await c.get("/health/live")
        ready = await c.get("/health/ready")
        alias = await c.get("/health")
    assert live.status_code == 200
    assert ready.status_code == 200
    assert alias.status_code == 200
