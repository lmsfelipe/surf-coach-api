"""Tests for GET /api/v1/media/{media_id}/content — media proxy / streaming."""

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.api import media as media_api
from app.api import sessions as sessions_api
from app.core.config import get_settings
from app.core.security.media_token import mint_media_token
from app.core.storage import get_storage_client
from app.main import app
from app.services.media import MediaService
from app.services.sessions import SessionsService
from tests.fake_deps import (
    FakeFrameExtractor,
    FakeMediaRepo,
    FakeSessionsRepo,
    FakeStorageClient,
)

FIXTURES = Path(__file__).parent / "fixtures"
JPEG_PATH = FIXTURES / "surf_sample.jpg"


@pytest.fixture(autouse=True)
def _override_media_deps():
    sessions_repo = FakeSessionsRepo()
    media_repo = FakeMediaRepo()
    storage = FakeStorageClient()
    frames = FakeFrameExtractor(duration=5.0)

    def _sessions_service() -> SessionsService:
        return SessionsService(sessions_repo)  # type: ignore[arg-type]

    def _media_service() -> MediaService:
        return MediaService(
            media_repo=media_repo,  # type: ignore[arg-type]
            sessions_repo=sessions_repo,  # type: ignore[arg-type]
            storage=storage,  # type: ignore[arg-type]
            frame_extractor=frames,  # type: ignore[arg-type]
        )

    app.dependency_overrides[sessions_api.get_sessions_service] = _sessions_service
    app.dependency_overrides[media_api.get_media_service] = _media_service
    app.dependency_overrides[get_storage_client] = lambda: storage
    yield
    app.dependency_overrides.pop(sessions_api.get_sessions_service, None)
    app.dependency_overrides.pop(media_api.get_media_service, None)
    app.dependency_overrides.pop(get_storage_client, None)


def _token(user_id: UUID, email: str = "surfer@example.com") -> str:
    settings = get_settings()
    payload = {
        "sub": str(user_id),
        "email": email,
        "aud": "authenticated",
        "exp": datetime.now(tz=timezone.utc) + timedelta(hours=1),
    }
    return jwt.encode(payload, settings.SUPABASE_JWT_SECRET, algorithm="HS256")


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create_session(c: AsyncClient, user_id: UUID) -> str:
    r = await c.post(
        "/api/v1/sessions/",
        headers={"Authorization": f"Bearer {_token(user_id)}"},
        json={
            "sessionDate": "2026-04-17",
            "location": "Praia de Santos",
            "waveSize": 1.5,
        },
    )
    assert r.status_code == 201
    return r.json()["id"]


async def _upload_image(c: AsyncClient, user_id: UUID, session_id: str) -> dict:
    headers = {"Authorization": f"Bearer {_token(user_id)}"}
    with open(JPEG_PATH, "rb") as f:
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers=headers,
            files={"file": ("surf.jpg", f, "image/jpeg")},
        )
    assert r.status_code == 201
    return r.json()[0]


# ---- Success cases --------------------------------------------------------


async def test_stream_returns_200_with_full_content(client):
    pytest.importorskip("magic")
    user_id = uuid4()
    async with client as c:
        session_id = await _create_session(c, user_id)
        body = await _upload_image(c, user_id, session_id)
        content_url = body["contentUrl"]

        r = await c.get(content_url)

    assert r.status_code == 200
    assert r.headers["accept-ranges"] == "bytes"
    assert "content-length" in r.headers
    assert int(r.headers["content-length"]) > 0


async def test_stream_returns_206_with_range(client):
    pytest.importorskip("magic")
    user_id = uuid4()
    async with client as c:
        session_id = await _create_session(c, user_id)
        body = await _upload_image(c, user_id, session_id)
        content_url = body["contentUrl"]

        r = await c.get(content_url, headers={"Range": "bytes=0-99"})

    assert r.status_code == 206
    assert "content-range" in r.headers
    assert r.headers["content-range"].startswith("bytes 0-99/")
    assert len(r.content) == 100


# ---- Auth / token error cases --------------------------------------------


async def test_stream_missing_token_returns_400(client):
    media_id = uuid4()
    async with client as c:
        r = await c.get(f"/api/v1/media/{media_id}/content")
    assert r.status_code == 400


async def test_stream_invalid_token_returns_401(client):
    media_id = uuid4()
    async with client as c:
        r = await c.get(f"/api/v1/media/{media_id}/content?token=garbage")
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "INVALID_TOKEN"


async def test_stream_expired_token_returns_401(client):
    pytest.importorskip("magic")
    user_id = uuid4()
    settings = get_settings()
    async with client as c:
        session_id = await _create_session(c, user_id)
        body = await _upload_image(c, user_id, session_id)
        media_id = body["id"]

        # Mint an already-expired token
        expired_payload = {
            "media_id": media_id,
            "sub": str(user_id),
            "aud": "media",
            "iat": int(time.time()) - 3600,
            "exp": int(time.time()) - 1800,
        }
        expired_token = jwt.encode(
            expired_payload, settings.SUPABASE_JWT_SECRET, algorithm="HS256",
        )
        r = await c.get(f"/api/v1/media/{media_id}/content?token={expired_token}")

    assert r.status_code == 401
    assert r.json()["error"]["code"] == "TOKEN_EXPIRED"


async def test_stream_token_for_different_media_returns_403(client):
    pytest.importorskip("magic")
    user_id = uuid4()
    async with client as c:
        session_id = await _create_session(c, user_id)
        body = await _upload_image(c, user_id, session_id)

        # Mint a token for a different (random) media_id
        other_media_id = uuid4()
        token = mint_media_token(other_media_id, user_id)

        r = await c.get(f"/api/v1/media/{body['id']}/content?token={token}")

    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


async def test_stream_other_users_media_returns_403(client):
    pytest.importorskip("magic")
    user_a = uuid4()
    user_b = uuid4()
    async with client as c:
        session_id = await _create_session(c, user_a)
        body = await _upload_image(c, user_a, session_id)
        media_id = body["id"]

        # Mint a valid token but for user_b
        token = mint_media_token(UUID(media_id), user_b)
        r = await c.get(f"/api/v1/media/{media_id}/content?token={token}")

    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


async def test_stream_nonexistent_media_returns_404(client):
    user_id = uuid4()
    media_id = uuid4()
    token = mint_media_token(media_id, user_id)
    async with client as c:
        r = await c.get(f"/api/v1/media/{media_id}/content?token={token}")
    assert r.status_code == 404


# ---- Response shape: no supabase URL leak ---------------------------------


async def test_media_out_never_contains_supabase_url(client):
    pytest.importorskip("magic")
    user_id = uuid4()
    async with client as c:
        session_id = await _create_session(c, user_id)
        body = await _upload_image(c, user_id, session_id)

    # Ensure no field leaks the Supabase host
    raw = str(body)
    assert "supabase.co" not in raw
    assert "storageUrl" not in body
    assert "contentUrl" in body
