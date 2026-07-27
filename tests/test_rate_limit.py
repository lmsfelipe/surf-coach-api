"""Tests for rate limiting."""

import datetime
from datetime import timedelta
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.core.config import get_settings
from app.core.deps import db_session, get_arq_pool
from app.core.rate_limit import limiter
from app.main import app


class _FakeArqPool:
    async def enqueue_job(self, *args, **kwargs):
        pass


async def _fake_db_session():
    yield None


def _token(user_id=None, email="surfer@example.com"):
    user_id = user_id or uuid4()
    settings = get_settings()
    payload = {
        "sub": str(user_id),
        "email": email,
        "aud": "authenticated",
        "exp": datetime.datetime.now(tz=datetime.UTC) + timedelta(hours=1),
    }
    return jwt.encode(payload, settings.SUPABASE_JWT_SECRET, algorithm="HS256"), user_id


@pytest.fixture(autouse=True)
def _override_infra():
    """Override infra deps to avoid Redis/Postgres connections."""
    app.dependency_overrides[get_arq_pool] = _FakeArqPool
    app.dependency_overrides[db_session] = _fake_db_session
    yield
    app.dependency_overrides.pop(get_arq_pool, None)
    app.dependency_overrides.pop(db_session, None)


@pytest.fixture
def _enable_rate_limit():
    """Temporarily enable rate limiting for a specific test."""
    limiter.enabled = True
    limiter.reset()
    yield
    limiter.enabled = False


@pytest.fixture
def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.usefixtures("_enable_rate_limit")
async def test_rate_limit_returns_429_on_reviews(client):
    """POST /reviews/ returns 429 after exceeding the 5/hour AI limit.

    The handler errors (500) because there's no real DB, but the rate limiter
    still increments its counter per request. After 5 calls the 6th is blocked.
    """
    user_id = uuid4()
    token, _ = _token(user_id)
    headers = {"Authorization": f"Bearer {token}"}

    async with client as c:
        for _ in range(5):
            r = await c.post(
                "/api/v1/reviews/",
                headers=headers,
                json={"sessionId": str(uuid4())},
            )
            assert r.status_code == 500  # handler fails, but counter increments

        # 6th request is rate-limited before the handler runs
        r = await c.post(
            "/api/v1/reviews/",
            headers=headers,
            json={"sessionId": str(uuid4())},
        )
        assert r.status_code == 429
        body = r.json()
        assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert body["error"]["message"] == "Too many requests. Please try again later."


@pytest.mark.usefixtures("_enable_rate_limit")
async def test_rate_limit_disabled_allows_all(client):
    """When rate limiting is disabled, no 429 is returned."""
    limiter.enabled = False

    user_id = uuid4()
    token, _ = _token(user_id)
    headers = {"Authorization": f"Bearer {token}"}

    async with client as c:
        for _ in range(10):
            r = await c.post(
                "/api/v1/reviews/",
                headers=headers,
                json={"sessionId": str(uuid4())},
            )
            assert r.status_code != 429


@pytest.mark.usefixtures("_enable_rate_limit")
async def test_default_limit_applies_to_undecorated_routes(client, monkeypatch):
    """Routes without an explicit @limiter.limit still get RATE_LIMIT_DEFAULT."""
    monkeypatch.setenv("RATE_LIMIT_DEFAULT", "3/minute")
    get_settings.cache_clear()
    limiter.reset()
    token, _ = _token()
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with client as c:
            for _ in range(3):
                r = await c.get("/api/v1/sessions/", headers=headers)
                assert r.status_code != 429

            r = await c.get("/api/v1/sessions/", headers=headers)
            assert r.status_code == 429
            assert r.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    finally:
        get_settings.cache_clear()


@pytest.mark.usefixtures("_enable_rate_limit")
async def test_health_endpoints_are_exempt_from_default_limit(client, monkeypatch):
    """A throttled health check would read as an outage — they must never 429."""
    monkeypatch.setenv("RATE_LIMIT_DEFAULT", "2/minute")
    get_settings.cache_clear()
    limiter.reset()

    try:
        async with client as c:
            for _ in range(10):
                assert (await c.get("/health/live")).status_code == 200
    finally:
        get_settings.cache_clear()


@pytest.mark.usefixtures("_enable_rate_limit")
async def test_decorated_route_limit_stacks_with_default(client, monkeypatch):
    """Explicit route limits stack with the default; the stricter one governs.

    Because this project passes *callable* limit values, slowapi registers them
    in ``_dynamic_route_limits`` rather than ``_route_limits`` — and the
    middleware's exempt check only consults the latter. So the default limit is
    evaluated on these routes too. That is safe as long as RATE_LIMIT_DEFAULT
    stays looser than every per-route limit.
    """
    monkeypatch.setenv("RATE_LIMIT_DEFAULT", "1000/minute")
    get_settings.cache_clear()
    limiter.reset()
    token, _ = _token()
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with client as c:
            # A loose default leaves the 5/hour AI limit in charge.
            for _ in range(5):
                r = await c.post(
                    "/api/v1/reviews/",
                    headers=headers,
                    json={"sessionId": str(uuid4())},
                )
                assert r.status_code != 429

            r = await c.post(
                "/api/v1/reviews/",
                headers=headers,
                json={"sessionId": str(uuid4())},
            )
            assert r.status_code == 429
            assert r.json()["error"]["code"] == "RATE_LIMIT_EXCEEDED"
    finally:
        get_settings.cache_clear()


@pytest.mark.usefixtures("_enable_rate_limit")
async def test_default_limit_429_uses_error_envelope(client, monkeypatch):
    """A 429 raised by the middleware must use the same envelope as the decorator path."""
    monkeypatch.setenv("RATE_LIMIT_DEFAULT", "1/minute")
    get_settings.cache_clear()
    limiter.reset()
    token, _ = _token()
    headers = {"Authorization": f"Bearer {token}"}

    try:
        async with client as c:
            await c.get("/api/v1/sessions/", headers=headers)
            r = await c.get("/api/v1/sessions/", headers=headers)

        assert r.status_code == 429
        body = r.json()
        assert body["error"]["code"] == "RATE_LIMIT_EXCEEDED"
        assert body["error"]["message"] == "Too many requests. Please try again later."
    finally:
        get_settings.cache_clear()


@pytest.mark.usefixtures("_enable_rate_limit")
async def test_rate_limit_per_user_isolation(client):
    """Different users have independent rate limit counters."""
    user_a = uuid4()
    user_b = uuid4()
    token_a, _ = _token(user_a)
    token_b, _ = _token(user_b)

    async with client as c:
        # Exhaust user A's limit
        for _ in range(5):
            await c.post(
                "/api/v1/reviews/",
                headers={"Authorization": f"Bearer {token_a}"},
                json={"sessionId": str(uuid4())},
            )

        # User A is blocked
        r = await c.post(
            "/api/v1/reviews/",
            headers={"Authorization": f"Bearer {token_a}"},
            json={"sessionId": str(uuid4())},
        )
        assert r.status_code == 429

        # User B still has quota
        r = await c.post(
            "/api/v1/reviews/",
            headers={"Authorization": f"Bearer {token_b}"},
            json={"sessionId": str(uuid4())},
        )
        assert r.status_code != 429
