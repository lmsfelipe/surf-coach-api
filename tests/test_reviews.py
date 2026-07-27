from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.api import reviews as reviews_api
from app.api import sessions as sessions_api
from app.core.config import get_settings
from app.core.deps import get_arq_pool
from app.main import app
from app.services.ai import (
    GeminiService,
    ReviewOutput,
    ReviewService,
    ScoreRubric,
    normalise_scores,
)
from app.services.sessions import SessionsService
from tests.fake_deps import (
    FakeArqPool,
    FakeAuthRepo,
    FakeFrameExtractor,
    FakeGeminiService,
    FakeMediaRepo,
    FakeReviewRepo,
    FakeSessionsRepo,
    FakeStorageClient,
    FakeSurfboardRepo,
    make_review_output,
)


@pytest.fixture
def review_ctx():
    ctx = {
        "auth": FakeAuthRepo(),
        "sessions": FakeSessionsRepo(),
        "media": FakeMediaRepo(),
        "reviews": FakeReviewRepo(),
        "surfboards": FakeSurfboardRepo(),
        "storage": FakeStorageClient(),
        "frames": FakeFrameExtractor(frames=[b"frame-1", b"frame-2"]),
        "gemini": FakeGeminiService(make_review_output()),
        "arq": FakeArqPool(),
    }
    return ctx


def _build_review_service(ctx) -> ReviewService:
    return ReviewService(
        sessions_repo=ctx["sessions"],  # type: ignore[arg-type]
        media_repo=ctx["media"],  # type: ignore[arg-type]
        review_repo=ctx["reviews"],  # type: ignore[arg-type]
        auth_repo=ctx["auth"],  # type: ignore[arg-type]
        surfboard_repo=ctx["surfboards"],  # type: ignore[arg-type]
        gemini=ctx["gemini"],  # type: ignore[arg-type]
        frame_extractor=ctx["frames"],  # type: ignore[arg-type]
        storage=ctx["storage"],  # type: ignore[arg-type]
    )


@pytest.fixture(autouse=True)
def _override_review_deps(review_ctx):
    def _sessions_service() -> SessionsService:
        return SessionsService(review_ctx["sessions"])  # type: ignore[arg-type]

    app.dependency_overrides[sessions_api.get_sessions_service] = _sessions_service
    app.dependency_overrides[reviews_api.get_review_service] = lambda: _build_review_service(
        review_ctx
    )
    app.dependency_overrides[get_arq_pool] = lambda: review_ctx["arq"]
    yield
    app.dependency_overrides.pop(sessions_api.get_sessions_service, None)
    app.dependency_overrides.pop(reviews_api.get_review_service, None)
    app.dependency_overrides.pop(get_arq_pool, None)


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
            "waveSize": 2.0,
        },
    )
    assert r.status_code == 201
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


async def test_generate_review_accepts_and_enqueues(client, review_ctx):
    """POST /reviews/ is async: it returns 202 with a pending row and queues the job."""
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
    assert r.status_code == 202
    body = r.json()
    assert body["sessionId"] == session_id
    assert body["profileId"] == str(user_id)
    assert body["status"] == "processing"
    assert body["narrative"] is None

    assert review_ctx["arq"].jobs == [("process_review_task", (body["id"],))]


async def test_generate_review_marks_failed_when_enqueue_fails(client, review_ctx):
    """If Redis is unreachable the row must not be left stuck in 'processing'."""
    review_ctx["arq"] = FakeArqPool(raise_exc=RuntimeError("redis down"))
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
    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "failed"
    assert body["errorMessage"] == reviews_api.ENQUEUE_FAILED_MESSAGE


async def test_process_review_completes_with_normalised_scores(review_ctx):
    """The worker path fills in narrative, tips and scores."""
    user_id = uuid4()
    session = await review_ctx["sessions"].create(
        profile_id=user_id,
        session_date=date(2026, 4, 17),
        location="Praia de Santos",
        wave_size=2.0,
        notes=None,
    )
    await _seed_media(review_ctx, session.id, user_id)

    service = _build_review_service(review_ctx)
    pending = await review_ctx["reviews"].create_pending(session_id=session.id, profile_id=user_id)
    review = await service.process_review(pending.id)

    assert review.status == "completed"
    assert len(review.improvement_tips) == 3
    scores = [
        review.score_flow,
        review.score_drop,
        review.score_balance,
        review.score_wave_selection,
        review.score_maneuvers,
        review.score_arms,
    ]
    assert all(s is not None for s in scores)
    expected_mean = round(sum(float(s) for s in scores) / 6.0, 1)
    assert float(review.overall_score) == expected_mean


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
        assert r1.status_code == 202
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
    scores = ScoreRubric(
        flow=7.254,
        drop=7.254,
        balance=7.254,
        wave_selection=7.254,
        maneuvers=7.254,
        arms=7.254,
    )
    n = normalise_scores(scores)
    # normalise_scores returns Decimal — compare against Decimal, since
    # Decimal("7.3") != the float 7.3.
    assert n["flow"] == Decimal("7.3")
    assert n["overall"] == Decimal("7.3")


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


def test_parse_response_accepts_trailing_commas():
    """Gemini occasionally emits a trailing comma; it must not cost the whole review."""
    raw = (
        '{"narrative":"ok",'
        '"improvement_tips":["a","b","c",],'
        '"scores":{"flow":5,"drop":5,"balance":5,'
        '"wave_selection":5,"maneuvers":5,"arms":5,},}'
    )
    out = GeminiService.parse_response(raw)
    assert isinstance(out, ReviewOutput)
    assert out.improvement_tips == ["a", "b", "c"]
    assert out.scores.arms == 5


def test_parse_response_keeps_commas_inside_strings():
    raw = (
        '{"narrative":"drop, bottom turn, and cutback",'
        '"improvement_tips":["a","b","c"],'
        '"scores":{"flow":5,"drop":5,"balance":5,"wave_selection":5,"maneuvers":5,"arms":5}}'
    )
    out = GeminiService.parse_response(raw)
    assert out.narrative == "drop, bottom turn, and cutback"


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
    )
    prompt = build_prompt(ctx)
    assert "intermediate" in prompt
    assert "Praia de Santos" in prompt
    assert "shortboard" in prompt
    assert "narrative" in prompt
    assert "scores" in prompt


def test_scoring_pass_never_sees_the_surfer_description():
    """The surfer's free-text description must not reach the scoring context —
    that is what used to bias the scores toward the surfer's own tone."""
    from app.services.ai import SurferContext, build_prompt, build_refinement_prompt

    # SurferContext has no field for the description at all.
    assert "notes" not in SurferContext.model_fields
    ctx = SurferContext(skill_level="beginner", location="Maresias")
    assert "felt amazing" not in build_prompt(ctx)

    # The description only appears in the second-pass refinement prompt.
    review = make_review_output()
    refine_prompt = build_refinement_prompt(review, ctx, "felt amazing, best session ever")
    assert "felt amazing" in refine_prompt


async def test_process_review_scores_are_isolated_from_description():
    """A glowing description must not change the media-only scores, and the
    refinement pass must be invoked with that description."""
    user_id = uuid4()
    ctx = {
        "auth": FakeAuthRepo(),
        "sessions": FakeSessionsRepo(),
        "media": FakeMediaRepo(),
        "reviews": FakeReviewRepo(),
        "surfboards": FakeSurfboardRepo(),
        "storage": FakeStorageClient(),
        "frames": FakeFrameExtractor(frames=[b"frame-1", b"frame-2"]),
        "gemini": FakeGeminiService(make_review_output()),
        "arq": FakeArqPool(),
    }
    session = await ctx["sessions"].create(
        profile_id=user_id,
        session_date=date(2026, 4, 17),
        location="Praia de Santos",
        wave_size=2.0,
        notes="Foi a melhor sessão da minha vida, mandei muito bem!",
    )
    await _seed_media(ctx, session.id, user_id)

    service = _build_review_service(ctx)
    pending = await ctx["reviews"].create_pending(session_id=session.id, profile_id=user_id)
    review = await service.process_review(pending.id)

    # The description drove exactly one refinement call...
    assert len(ctx["gemini"].refine_calls) == 1
    assert ctx["gemini"].refine_calls[0][2].startswith("Foi a melhor")
    # ...and it never leaked into the scoring pass's context.
    _, scoring_context = ctx["gemini"].calls[0]
    assert not hasattr(scoring_context, "notes")

    # Scores match the media-only output regardless of the glowing description.
    expected = normalise_scores(make_review_output().scores)
    assert review.score_flow == expected["flow"]
    assert review.overall_score == expected["overall"]


async def test_process_review_without_description_skips_refinement():
    user_id = uuid4()
    ctx = {
        "auth": FakeAuthRepo(),
        "sessions": FakeSessionsRepo(),
        "media": FakeMediaRepo(),
        "reviews": FakeReviewRepo(),
        "surfboards": FakeSurfboardRepo(),
        "storage": FakeStorageClient(),
        "frames": FakeFrameExtractor(frames=[b"frame-1", b"frame-2"]),
        "gemini": FakeGeminiService(make_review_output()),
        "arq": FakeArqPool(),
    }
    session = await ctx["sessions"].create(
        profile_id=user_id,
        session_date=date(2026, 4, 17),
        location="Praia de Santos",
        wave_size=2.0,
        notes=None,
    )
    await _seed_media(ctx, session.id, user_id)

    service = _build_review_service(ctx)
    pending = await ctx["reviews"].create_pending(session_id=session.id, profile_id=user_id)
    await service.process_review(pending.id)

    assert ctx["gemini"].refine_calls == []
