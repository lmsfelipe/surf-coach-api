from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.api import media as media_api
from app.api import sessions as sessions_api
from app.core.config import get_settings
from app.main import app
from app.services.media import MediaService
from app.services.sessions import SessionsService
from tests.fake_deps import (
    FakeFrameExtractor,
    FakeGeminiService,
    FakeMediaRepo,
    FakeSessionsRepo,
    FakeStorageClient,
    make_moderation_output,
    make_review_output,
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
    yield
    app.dependency_overrides.pop(sessions_api.get_sessions_service, None)
    app.dependency_overrides.pop(media_api.get_media_service, None)


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


async def test_upload_image_returns_201_and_storage_url(client):
    pytest.importorskip("magic")
    user_id = uuid4()
    headers = {"Authorization": f"Bearer {_token(user_id)}"}
    image_bytes = JPEG_PATH.read_bytes()
    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers=headers,
            files=[
                ("file", ("surf.jpg", image_bytes, "image/jpeg")),
                ("file", ("surf2.jpg", image_bytes, "image/jpeg")),
                ("file", ("surf3.jpg", image_bytes, "image/jpeg")),
            ],
        )
    assert r.status_code == 201
    assert len(r.json()) == 3
    body = r.json()[0]
    assert body["mediaType"] == "image"
    assert "contentUrl" in body
    assert body["contentUrl"].startswith("/api/v1/media/")
    assert "token=" in body["contentUrl"]
    assert "storageUrl" not in body
    assert body["fileName"] == "surf.jpg"
    assert body["sessionId"] == session_id


async def test_upload_invalid_mime_type_returns_422(client):
    pytest.importorskip("magic")
    user_id = uuid4()
    headers = {"Authorization": f"Bearer {_token(user_id)}"}
    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers=headers,
            files={"file": ("notes.txt", b"hello world", "text/plain")},
        )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_MEDIA_TYPE"


async def test_upload_forbidden_for_other_user(client):
    pytest.importorskip("magic")
    user_a = uuid4()
    user_b = uuid4()
    image_bytes = JPEG_PATH.read_bytes()
    async with client as c:
        session_id = await _create_session(c, user_a)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_b)}"},
            files=[
                ("file", ("surf.jpg", image_bytes, "image/jpeg")),
                ("file", ("surf2.jpg", image_bytes, "image/jpeg")),
                ("file", ("surf3.jpg", image_bytes, "image/jpeg")),
            ],
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


# ---------------------------------------------------------------------------
# Content moderation tests
# ---------------------------------------------------------------------------


@pytest.fixture
def _moderation_media_service(monkeypatch):
    """Enables content moderation with a controllable FakeGeminiService."""
    sessions_repo = FakeSessionsRepo()
    media_repo = FakeMediaRepo()
    storage = FakeStorageClient()
    frames = FakeFrameExtractor(duration=5.0)
    gemini = FakeGeminiService(output=make_review_output())

    def _sessions_service() -> SessionsService:
        return SessionsService(sessions_repo)  # type: ignore[arg-type]

    def _media_service() -> MediaService:
        return MediaService(
            media_repo=media_repo,  # type: ignore[arg-type]
            sessions_repo=sessions_repo,  # type: ignore[arg-type]
            storage=storage,  # type: ignore[arg-type]
            frame_extractor=frames,  # type: ignore[arg-type]
            gemini=gemini,
        )

    app.dependency_overrides[sessions_api.get_sessions_service] = _sessions_service
    app.dependency_overrides[media_api.get_media_service] = _media_service
    monkeypatch.setenv("CONTENT_MODERATION_ENABLED", "true")
    get_settings.cache_clear()
    yield gemini
    app.dependency_overrides.pop(sessions_api.get_sessions_service, None)
    app.dependency_overrides.pop(media_api.get_media_service, None)
    get_settings.cache_clear()


async def test_upload_non_surf_content_returns_422(client, _moderation_media_service):
    pytest.importorskip("magic")
    gemini = _moderation_media_service
    gemini._moderation_output = make_moderation_output(
        surf_related=False, reason="Shows a soccer game."
    )
    user_id = uuid4()
    image_bytes = JPEG_PATH.read_bytes()
    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=[
                ("file", ("img1.jpg", image_bytes, "image/jpeg")),
                ("file", ("img2.jpg", image_bytes, "image/jpeg")),
                ("file", ("img3.jpg", image_bytes, "image/jpeg")),
            ],
        )
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "MEDIA_NOT_SURF_RELATED"
    assert body["details"]["reason"] == "Shows a soccer game."


async def test_upload_explicit_content_returns_422(client, _moderation_media_service):
    pytest.importorskip("magic")
    gemini = _moderation_media_service
    gemini._moderation_output = make_moderation_output(
        surf_related=True, explicit_content=True, reason="Contains nudity."
    )
    user_id = uuid4()
    image_bytes = JPEG_PATH.read_bytes()
    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=[
                ("file", ("img1.jpg", image_bytes, "image/jpeg")),
                ("file", ("img2.jpg", image_bytes, "image/jpeg")),
                ("file", ("img3.jpg", image_bytes, "image/jpeg")),
            ],
        )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "EXPLICIT_CONTENT"


async def test_upload_surf_content_passes_moderation(client, _moderation_media_service):
    pytest.importorskip("magic")
    gemini = _moderation_media_service
    gemini._moderation_output = make_moderation_output(surf_related=True)
    user_id = uuid4()
    image_bytes = JPEG_PATH.read_bytes()
    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=[
                ("file", ("img1.jpg", image_bytes, "image/jpeg")),
                ("file", ("img2.jpg", image_bytes, "image/jpeg")),
                ("file", ("img3.jpg", image_bytes, "image/jpeg")),
            ],
        )
    assert r.status_code == 201
    assert len(gemini.moderation_calls) == 3
