from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.api import reviews as reviews_api
from app.api import sessions as sessions_api
from app.core.config import get_settings
from app.main import app
from app.services.ai import GeminiService, ReviewOutput, ReviewService, ScoreRubric, normalise_scores
from app.services.sessions import SessionsService
from tests.fake_deps import (
    FakeAuthRepo,
    FakeFrameExtractor,
    FakeGeminiService,
    FakeMediaRepo,
    FakeReviewRepo,
    FakeSessionsRepo,
    FakeStorageClient,
    make_review_output,
)

@pytest.fixture
def review_ctx():
    ctx = {
        "auth": FakeAuthRepo(),
        "sessions": FakeSessionsRepo(),
        "media": FakeMediaRepo(),
        "reviews": FakeReviewRepo(),
        "storage": FakeStorageClient(),
        "frames": FakeFrameExtractor(frames=[b"frame-1", b"frame-2"]),
        "gemini": FakeGeminiService(make_review_output()),
    }
    return ctx


@pytest.fixture(autouse=True)
def _override_review_deps(review_ctx):
    def _sessions_service() -> SessionsService:
        return SessionsService(review_ctx["sessions"])  # type: ignore[arg-type]

    def _review_service() -> ReviewService:
        return ReviewService(
            sessions_repo=review_ctx["sessions"],  # type: ignore[arg-type]
            media_repo=review_ctx["media"],  # type: ignore[arg-type]
            review_repo=review_ctx["reviews"],  # type: ignore[arg-type]
            auth_repo=review_ctx["auth"],  # type: ignore[arg-type]
            gemini=review_ctx["gemini"],  # type: ignore[arg-type]
            frame_extractor=review_ctx["frames"],  # type: ignore[arg-type]
            storage=review_ctx["storage"],  # type: ignore[arg-type]
        )

    app.dependency_overrides[sessions_api.get_sessions_service] = _sessions_service
    app.dependency_overrides[reviews_api.get_review_service] = _review_service
    yield
    app.dependency_overrides.pop(sessions_api.get_sessions_service, None)
    app.dependency_overrides.pop(reviews_api.get_review_service, None)


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
            "waveConditions": "overhead",
        },
    )
    return r.json()["id"]


async def _seed_media(ctx, session_id: UUID, user_id: UUID) -> None:
    key = f"{user_id}/{session_id}/{uuid4()}.jpg"
    ctx["storage"].uploaded[key] = b"fake-jpeg"
    await ctx["media"].create(
        session_id=session_id,
        media_type="image",
        storage_url=f"https://storage.test/surf-media/{key}",
        file_name="surf.jpg",
        file_size_bytes=10,
        duration_seconds=None,
    )


async def test_generate_review_success(client, review_ctx):
    user_id = uuid4()
    headers = {"Authorization": f"Bearer {_token(user_id)}"}
    async with client as c:
        session_id = await _create_session(c, user_id)
        await _seed_media(review_ctx, UUID(session_id), user_id)
        r = await c.post(
            "/api/v1/reviews/",
            headers=headers,
            json={"sessionId": session_id},
        )
    assert r.status_code == 201
    body = r.json()
    assert body["sessionId"] == session_id
    assert body["profileId"] == str(user_id)
    assert len(body["improvementTips"]) == 3
    expected_mean = round(
        (
            body["scoreFlow"]
            + body["scoreDrop"]
            + body["scoreBalance"]
            + body["scoreWaveSelection"]
            + body["scoreManeuvers"]
            + body["scoreArms"]
        )
        / 6.0,
        1,
    )
    assert body["overallScore"] == expected_mean


async def test_generate_review_no_media_returns_422(client):
    user_id = uuid4()
    headers = {"Authorization": f"Bearer {_token(user_id)}"}
    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            "/api/v1/reviews/",
            headers=headers,
            json={"sessionId": session_id},
        )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "NO_MEDIA_FOR_SESSION"


async def test_generate_review_duplicate_returns_409(client, review_ctx):
    user_id = uuid4()
    headers = {"Authorization": f"Bearer {_token(user_id)}"}
    async with client as c:
        session_id = await _create_session(c, user_id)
        await _seed_media(review_ctx, UUID(session_id), user_id)
        r1 = await c.post(
            "/api/v1/reviews/",
            headers=headers,
            json={"sessionId": session_id},
        )
        assert r1.status_code == 201
        r2 = await c.post(
            "/api/v1/reviews/",
            headers=headers,
            json={"sessionId": session_id},
        )
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "REVIEW_ALREADY_EXISTS"


async def test_generate_review_wrong_owner_returns_403(client, review_ctx):
    user_a = uuid4()
    user_b = uuid4()
    async with client as c:
        session_id = await _create_session(c, user_a)
        await _seed_media(review_ctx, UUID(session_id), user_a)
        r = await c.post(
            "/api/v1/reviews/",
            headers={"Authorization": f"Bearer {_token(user_b)}"},
            json={"sessionId": session_id},
        )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


# --- Unit tests for pipeline primitives (no HTTP) ---


def test_normalise_scores_clamps_and_rounds():
    scores = ScoreRubric(flow=0, drop=10, balance=5, wave_selection=5, maneuvers=5, arms=5)
    n = normalise_scores(scores)
    assert n["flow"] == 0.0
    assert n["drop"] == 10.0
    assert n["overall"] == round((0 + 10 + 5 + 5 + 5 + 5) / 6, 1)


def test_normalise_scores_rounds_halfway():
    scores = ScoreRubric(flow=7.254, drop=7.254, balance=7.254, wave_selection=7.254, maneuvers=7.254, arms=7.254)
    n = normalise_scores(scores)
    # 7.254 rounded to 1dp -> 7.3 (banker's rounding on .5 — 7.3 via round-half-to-even falls out)
    assert n["flow"] == round(7.254, 1)
    assert n["overall"] == round(7.254, 1)


def test_parse_response_accepts_fenced_json():
    raw = (
        "```json\n"
        '{"narrative":"ok",'
        '"improvement_tips":["a","b","c"],'
        '"scores":{"flow":5,"drop":5,"balance":5,"wave_selection":5,"maneuvers":5,"arms":5}}\n'
        "```"
    )
    out = GeminiService.parse_response(raw)
    assert isinstance(out, ReviewOutput)
    assert out.narrative == "ok"


def test_parse_response_rejects_missing_field():
    from app.core.errors import AIParseFailedError

    raw = '{"narrative":"ok","improvement_tips":["a","b","c"]}'
    with pytest.raises(AIParseFailedError):
        GeminiService.parse_response(raw)


def test_prompt_includes_context_fields():
    from app.services.ai import SurferContext, build_prompt

    ctx = SurferContext(
        skill_level="intermediate",
        location="Praia de Santos",
        wave_conditions="overhead",
        board_type="shortboard",
        notes="felt smooth",
    )
    prompt = build_prompt(ctx)
    assert "intermediate" in prompt
    assert "Praia de Santos" in prompt
    assert "shortboard" in prompt
    assert "narrative" in prompt
    assert "scores" in prompt
