from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.api import auth as auth_api
from app.core.config import get_settings
from app.main import app
from app.services.auth import AuthService
from tests.fake_deps import FakeAuthRepo


@pytest.fixture(autouse=True)
def _override_auth_service():
    """Scope the override to each test.

    Installing it at import time would leak the fake into every module that
    pytest collects afterwards, and a module-level repo instance would let one
    test's profile survive into the next.
    """
    repo = FakeAuthRepo()
    app.dependency_overrides[auth_api.get_auth_service] = lambda: AuthService(repo)  # type: ignore[arg-type]
    yield
    app.dependency_overrides.pop(auth_api.get_auth_service, None)


def _token(user_id: UUID, email: str = "surfer@example.com") -> str:
    settings = get_settings()
    payload = {
        "sub": str(user_id),
        "email": email,
        "aud": "authenticated",
        "exp": datetime.now(tz=UTC) + timedelta(hours=1),
    }
    return jwt.encode(payload, settings.SUPABASE_JWT_SECRET, algorithm="HS256")


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_me_missing_token_returns_401_missing_token(client):
    async with client as c:
        r = await c.get("/me")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "MISSING_TOKEN"


async def test_me_tampered_token_returns_401_invalid_token(client):
    async with client as c:
        r = await c.get("/me", headers={"Authorization": "Bearer not-a-real-token"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_TOKEN"


async def test_me_auto_provisions_profile_on_first_call(client):
    user_id = uuid4()
    token = _token(user_id)
    async with client as c:
        r = await c.get("/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == str(user_id)
    assert body["email"] == "surfer@example.com"
    assert body["surfLevel"] == "beginner"
    assert body["heightCm"] is None
    assert body["weightKg"] is None


async def test_patch_me_updates_profile_and_persists(client):
    user_id = uuid4()
    token = _token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    async with client as c:
        await c.get("/me", headers=headers)
        r = await c.patch(
            "/me",
            headers=headers,
            json={"surfLevel": "intermediate", "heightCm": 180, "weightKg": 75},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["surfLevel"] == "intermediate"
        assert body["heightCm"] == 180
        assert body["weightKg"] == 75

        r2 = await c.get("/me", headers=headers)
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["surfLevel"] == "intermediate"
    assert body2["heightCm"] == 180
    assert body2["weightKg"] == 75


async def test_patch_me_validation_error_envelope(client):
    user_id = uuid4()
    token = _token(user_id)
    async with client as c:
        r = await c.patch(
            "/me",
            headers={"Authorization": f"Bearer {token}"},
            json={"heightCm": 10},
        )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
