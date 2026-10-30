from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.api import sessions as sessions_api
from app.core.config import get_settings
from app.main import app
from app.services.sessions import SessionsService
from tests.fake_deps import FakeReviewRepo, FakeSessionsRepo


@pytest.fixture
def reviews() -> FakeReviewRepo:
    return FakeReviewRepo()


@pytest.fixture(autouse=True)
def _override_sessions_service(reviews):
    repo = FakeSessionsRepo(reviews)
    app.dependency_overrides[sessions_api.get_sessions_service] = lambda: SessionsService(repo)  # type: ignore[arg-type]
    yield
    app.dependency_overrides.pop(sessions_api.get_sessions_service, None)


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


async def test_create_session_returns_201_with_profile_id(client):
    user_id = uuid4()
    token = _token(user_id)
    async with client as c:
        r = await c.post(
            "/api/v1/sessions/",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "sessionDate": "2026-04-17",
                "location": "Praia de Santos",
                "waveSize": 1.8,
                "notes": "Felt good",
            },
        )
    assert r.status_code == 201
    body = r.json()
    assert body["profileId"] == str(user_id)
    assert body["location"] == "Praia de Santos"
    assert body["sessionDate"] == "2026-04-17"


async def test_list_sessions_returns_user_sessions(client):
    user_id = uuid4()
    token = _token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    async with client as c:
        await c.post(
            "/api/v1/sessions/",
            headers=headers,
            json={
                "sessionDate": "2026-04-10",
                "location": "Maresias",
                "waveSize": 1.5,
            },
        )
        await c.post(
            "/api/v1/sessions/",
            headers=headers,
            json={
                "sessionDate": "2026-04-17",
                "location": "Itamambuca",
                "waveSize": 1.2,
            },
        )
        r = await c.get("/api/v1/sessions/", headers=headers)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    assert body[0]["sessionDate"] >= body[1]["sessionDate"]


async def test_list_sessions_embeds_each_review_summary(client, reviews):
    """The list carries every row's review so the UI needs no per-session request."""
    user_id = uuid4()
    headers = {"Authorization": f"Bearer {_token(user_id)}"}
    async with client as c:
        ids = []
        for day in ("2026-04-10", "2026-04-11", "2026-04-12"):
            r = await c.post(
                "/api/v1/sessions/",
                headers=headers,
                json={"sessionDate": day, "location": "Maresias", "waveSize": 1.5},
            )
            ids.append(UUID(r.json()["id"]))
        done = await reviews.create_pending(session_id=ids[0], profile_id=user_id)
        done.status, done.overall_score = "completed", Decimal("7.4")
        running = await reviews.create_pending(session_id=ids[1], profile_id=user_id)

        r = await c.get("/api/v1/sessions/", headers=headers)

    assert r.status_code == 200
    by_id = {row["id"]: row["review"] for row in r.json()}
    assert by_id[str(ids[0])] == {"id": str(done.id), "status": "completed", "overallScore": 7.4}
    assert by_id[str(ids[1])] == {
        "id": str(running.id),
        "status": "processing",
        "overallScore": None,
    }
    assert by_id[str(ids[2])] is None


async def test_single_session_response_has_no_review_field(client):
    """Only the list embeds the summary; GET /sessions/{id} keeps its shape."""
    user_id = uuid4()
    headers = {"Authorization": f"Bearer {_token(user_id)}"}
    async with client as c:
        created = await c.post(
            "/api/v1/sessions/",
            headers=headers,
            json={"sessionDate": "2026-04-17", "location": "Maresias", "waveSize": 1.5},
        )
        r = await c.get(f"/api/v1/sessions/{created.json()['id']}", headers=headers)
    assert r.status_code == 200
    assert "review" not in r.json()
    assert "review" not in created.json()


async def test_get_session_forbidden_for_other_user(client):
    user_a = uuid4()
    user_b = uuid4()
    async with client as c:
        r = await c.post(
            "/api/v1/sessions/",
            headers={"Authorization": f"Bearer {_token(user_a)}"},
            json={
                "sessionDate": "2026-04-17",
                "location": "Praia de Santos",
                "waveSize": 2.0,
            },
        )
        session_id = r.json()["id"]
        r2 = await c.get(
            f"/api/v1/sessions/{session_id}",
            headers={"Authorization": f"Bearer {_token(user_b)}"},
        )
    assert r2.status_code == 403
    assert r2.json()["error"]["code"] == "FORBIDDEN"


async def test_delete_session_returns_204(client):
    user_id = uuid4()
    token = _token(user_id)
    headers = {"Authorization": f"Bearer {token}"}
    async with client as c:
        r = await c.post(
            "/api/v1/sessions/",
            headers=headers,
            json={
                "sessionDate": "2026-04-17",
                "location": "Praia de Santos",
                "waveSize": 2.0,
            },
        )
        session_id = r.json()["id"]
        d = await c.delete(f"/api/v1/sessions/{session_id}", headers=headers)
        assert d.status_code == 204
        g = await c.get(f"/api/v1/sessions/{session_id}", headers=headers)
    assert g.status_code == 404


async def test_create_session_validation_error(client):
    user_id = uuid4()
    token = _token(user_id)
    async with client as c:
        r = await c.post(
            "/api/v1/sessions/",
            headers={"Authorization": f"Bearer {token}"},
            json={"sessionDate": "not-a-date"},
        )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "VALIDATION_ERROR"
