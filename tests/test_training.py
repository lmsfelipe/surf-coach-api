"""Tests for Phase 3: AI-Generated Training Plans."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt
from pydantic import ValidationError

from app.api import ai as ai_api
from app.core.config import get_settings
from app.core.deps import get_arq_pool
from app.core.errors import (
    AIParseFailedError,
)
from app.main import app
from app.models.review import Review
from app.services.ai import (
    TrainingContext,
    TrainingPlanOutput,
    TrainingService,
)
from tests.fake_deps import (
    FakeArqPool,
    FakeAuthRepo,
    FakeGeminiService,
    FakeReviewRepo,
    FakeTrainingPlanRepo,
    decimal,
    make_review_output,
    make_training_plan_output,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


def _make_review(user_id: UUID, session_id: UUID | None = None) -> Review:
    return Review(
        id=uuid4(),
        session_id=session_id or uuid4(),
        profile_id=user_id,
        narrative="Test narrative.",
        improvement_tips=["tip1", "tip2", "tip3"],
        score_flow=decimal(7.0),
        score_drop=decimal(6.5),
        score_balance=decimal(7.5),
        score_wave_selection=decimal(6.0),
        score_maneuvers=decimal(5.5),
        score_arms=decimal(6.5),
        overall_score=decimal(6.6),
        ai_model_version="gemini-1.5-pro",
        created_at=datetime.now(tz=UTC),
    )


# ---------------------------------------------------------------------------
# Unit: TrainingContext construction
# ---------------------------------------------------------------------------


def test_training_context_from_review_and_profile():
    from app.models.profile import Profile

    user_id = uuid4()
    review = _make_review(user_id)
    profile = Profile(
        id=user_id,
        surf_level="intermediate",
        height_cm=175,
        weight_kg=70,
        created_at=datetime.now(tz=UTC),
        updated_at=datetime.now(tz=UTC),
    )

    ctx = TrainingContext(
        surf_level=profile.surf_level,
        improvement_tips=list(review.improvement_tips),
        score_flow=float(review.score_flow) if review.score_flow is not None else None,
        score_balance=float(review.score_balance) if review.score_balance is not None else None,
        score_maneuvers=(
            float(review.score_maneuvers) if review.score_maneuvers is not None else None
        ),
        score_wave_selection=(
            float(review.score_wave_selection) if review.score_wave_selection is not None else None
        ),
        score_drop=float(review.score_drop) if review.score_drop is not None else None,
        score_arms=float(review.score_arms) if review.score_arms is not None else None,
        overall_score=float(review.overall_score) if review.overall_score is not None else None,
        height_cm=profile.height_cm,
        weight_kg=profile.weight_kg,
    )

    assert ctx.surf_level == "intermediate"
    assert ctx.improvement_tips == ["tip1", "tip2", "tip3"]
    assert ctx.score_flow == pytest.approx(7.0)
    assert ctx.score_balance == pytest.approx(7.5)
    assert ctx.score_maneuvers == pytest.approx(5.5)
    assert ctx.score_wave_selection == pytest.approx(6.0)
    assert ctx.score_drop == pytest.approx(6.5)
    assert ctx.score_arms == pytest.approx(6.5)
    assert ctx.overall_score == pytest.approx(6.6)
    assert ctx.height_cm == 175
    assert ctx.weight_kg == 70


# ---------------------------------------------------------------------------
# Unit: TrainingPlanOutput parsing
# ---------------------------------------------------------------------------


def test_training_plan_output_parses_valid_json():
    exercise = json.dumps(
        {"name": "Ex", "description": "D", "sets": 3, "reps": "10", "video_url": None}
    )
    exercises = ",".join([exercise] * 5)
    raw = (
        '{"workouts": ['
        + ",".join(
            f'{{"sequence_number": {i}, "title": "T{i}", "focus_area": "F{i}",'
            f'"exercises": [{exercises}]}}'
            for i in range(1, 4)
        )
        + "]}"
    )
    output = TrainingPlanOutput.model_validate_json(raw)
    assert len(output.workouts) == 3
    for w in output.workouts:
        assert len(w.exercises) == 5


def test_training_plan_output_missing_exercises_raises():
    raw = '{"workouts": [{"sequence_number": 1, "title": "T", "focus_area": "F"}]}'
    with pytest.raises(ValidationError):
        TrainingPlanOutput.model_validate_json(raw)


def test_gemini_service_raises_on_wrong_workout_count():
    """generate_training_plan raises AIParseFailedError when the workout count is wrong."""
    import json

    from app.services.ai import GeminiService

    # Build a fake response with only 2 workouts
    raw_2_workouts = json.dumps(
        {
            "workouts": [
                {
                    "sequence_number": i,
                    "title": f"T{i}",
                    "focus_area": f"F{i}",
                    "exercises": [
                        {
                            "name": "Ex",
                            "description": "D",
                            "sets": 3,
                            "reps": "10",
                            "video_url": None,
                        }
                    ]
                    * 5,
                }
                for i in range(1, 3)
            ]
        }
    )

    class _BadGemini(GeminiService):
        def __init__(self):
            pass

        def generate_training_plan(self, context):
            output = TrainingPlanOutput.model_validate_json(raw_2_workouts)
            if len(output.workouts) != 3:
                raise AIParseFailedError(f"Expected 3 workouts, got {len(output.workouts)}.")
            return output

    svc = _BadGemini()
    ctx = TrainingContext(
        surf_level="beginner",
        improvement_tips=[],
        score_flow=None,
        score_balance=None,
        score_maneuvers=None,
        score_wave_selection=None,
        score_drop=None,
        score_arms=None,
        overall_score=None,
        height_cm=None,
        weight_kg=None,
    )
    with pytest.raises(AIParseFailedError):
        svc.generate_training_plan(ctx)


# ---------------------------------------------------------------------------
# Integration fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def training_ctx():
    user_id = uuid4()
    review = _make_review(user_id)

    auth_repo = FakeAuthRepo()
    review_repo = FakeReviewRepo()
    plan_repo = FakeTrainingPlanRepo()
    gemini = FakeGeminiService(make_review_output())
    gemini._training_output = make_training_plan_output()

    review_repo._store[review.id] = review

    return {
        "user_id": user_id,
        "review": review,
        "auth": auth_repo,
        "reviews": review_repo,
        "plans": plan_repo,
        "gemini": gemini,
        "arq": FakeArqPool(),
    }


def _build_training_service(ctx) -> TrainingService:
    return TrainingService(
        review_repo=ctx["reviews"],
        auth_repo=ctx["auth"],
        training_plan_repo=ctx["plans"],
        gemini=ctx["gemini"],
    )


@pytest.fixture(autouse=True)
def _override_training_deps(training_ctx):
    app.dependency_overrides[ai_api.get_training_service] = lambda: _build_training_service(
        training_ctx
    )
    app.dependency_overrides[get_arq_pool] = lambda: training_ctx["arq"]
    yield
    app.dependency_overrides.pop(ai_api.get_training_service, None)
    app.dependency_overrides.pop(get_arq_pool, None)


async def _create_completed_plan(c: AsyncClient, ctx, user_id: UUID, review_id: UUID) -> dict:
    """POST a plan (202, pending) then run the worker step so workouts exist."""
    r = await c.post(
        "/api/v1/training-plans/",
        json={"reviewId": str(review_id)},
        headers={"Authorization": f"Bearer {_token(user_id)}"},
    )
    assert r.status_code == 202
    plan_id = UUID(r.json()["id"])
    await _build_training_service(ctx).process_training_plan(plan_id)

    get_r = await c.get(
        f"/api/v1/training-plans/{plan_id}",
        headers={"Authorization": f"Bearer {_token(user_id)}"},
    )
    assert get_r.status_code == 200
    return get_r.json()


# ---------------------------------------------------------------------------
# POST /api/v1/training-plans/ — generate plan
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_generate_training_plan_accepts_and_enqueues(client, training_ctx):
    """POST /training-plans/ is async: 202 with a pending row, workouts filled in later."""
    user_id = training_ctx["user_id"]
    review = training_ctx["review"]
    tok = _token(user_id)

    async with client as c:
        r = await c.post(
            "/api/v1/training-plans/",
            json={"reviewId": str(review.id)},
            headers={"Authorization": f"Bearer {tok}"},
        )

    assert r.status_code == 202
    body = r.json()
    assert body["reviewId"] == str(review.id)
    assert body["profileId"] == str(user_id)
    assert body["generatedBy"] == "ai"
    assert body["status"] == "processing"
    assert body["workouts"] == []

    assert training_ctx["arq"].jobs == [("process_training_plan_task", (body["id"],))]


@pytest.mark.anyio
async def test_process_training_plan_fills_in_workouts(client, training_ctx):
    user_id = training_ctx["user_id"]
    review = training_ctx["review"]

    async with client as c:
        body = await _create_completed_plan(c, training_ctx, user_id, review.id)

    assert body["status"] == "completed"
    assert len(body["workouts"]) == 3
    for w in body["workouts"]:
        assert len(w["exercises"]) == 5


@pytest.mark.anyio
async def test_generate_plan_marks_failed_when_enqueue_fails(client, training_ctx):
    training_ctx["arq"] = FakeArqPool(raise_exc=RuntimeError("redis down"))
    user_id = training_ctx["user_id"]
    review = training_ctx["review"]

    async with client as c:
        r = await c.post(
            "/api/v1/training-plans/",
            json={"reviewId": str(review.id)},
            headers={"Authorization": f"Bearer {_token(user_id)}"},
        )

    assert r.status_code == 202
    assert r.json()["status"] == "failed"
    assert r.json()["errorMessage"] == ai_api.ENQUEUE_FAILED_MESSAGE


@pytest.mark.anyio
async def test_generate_plan_wrong_owner_returns_403(client, training_ctx):
    other_id = uuid4()
    review = training_ctx["review"]
    tok = _token(other_id)

    async with client as c:
        r = await c.post(
            "/api/v1/training-plans/",
            json={"reviewId": str(review.id)},
            headers={"Authorization": f"Bearer {tok}"},
        )

    assert r.status_code == 403
    assert r.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.anyio
async def test_generate_plan_missing_review_returns_404(client, training_ctx):
    user_id = training_ctx["user_id"]
    tok = _token(user_id)

    async with client as c:
        r = await c.post(
            "/api/v1/training-plans/",
            json={"reviewId": str(uuid4())},
            headers={"Authorization": f"Bearer {tok}"},
        )

    assert r.status_code == 404
    assert r.json()["error"]["code"] == "REVIEW_NOT_FOUND"


@pytest.mark.anyio
async def test_generate_plan_duplicate_returns_409(client, training_ctx):
    user_id = training_ctx["user_id"]
    review = training_ctx["review"]
    tok = _token(user_id)

    async with client as c:
        r1 = await c.post(
            "/api/v1/training-plans/",
            json={"reviewId": str(review.id)},
            headers={"Authorization": f"Bearer {tok}"},
        )
        assert r1.status_code == 202

        r2 = await c.post(
            "/api/v1/training-plans/",
            json={"reviewId": str(review.id)},
            headers={"Authorization": f"Bearer {tok}"},
        )

    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "TRAINING_PLAN_ALREADY_EXISTS"


# ---------------------------------------------------------------------------
# GET /api/v1/training-plans/{plan_id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_training_plan_by_id(client, training_ctx):
    user_id = training_ctx["user_id"]
    review = training_ctx["review"]

    async with client as c:
        plan = await _create_completed_plan(c, training_ctx, user_id, review.id)

    assert len(plan["workouts"]) == 3


@pytest.mark.anyio
async def test_get_training_plan_wrong_owner_returns_403(client, training_ctx):
    user_id = training_ctx["user_id"]
    other_id = uuid4()
    review = training_ctx["review"]

    async with client as c:
        plan = await _create_completed_plan(c, training_ctx, user_id, review.id)

        get_r = await c.get(
            f"/api/v1/training-plans/{plan['id']}",
            headers={"Authorization": f"Bearer {_token(other_id)}"},
        )

    assert get_r.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/v1/reviews/{review_id}/training-plan
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_training_plan_for_review(client, training_ctx):
    user_id = training_ctx["user_id"]
    review = training_ctx["review"]
    tok = _token(user_id)

    async with client as c:
        plan = await _create_completed_plan(c, training_ctx, user_id, review.id)

        get_r = await c.get(
            f"/api/v1/reviews/{review.id}/training-plan",
            headers={"Authorization": f"Bearer {tok}"},
        )

    assert get_r.status_code == 200
    assert get_r.json()["id"] == plan["id"]


@pytest.mark.anyio
async def test_get_training_plan_for_review_no_plan_returns_404(client, training_ctx):
    user_id = training_ctx["user_id"]
    review = training_ctx["review"]
    tok = _token(user_id)

    async with client as c:
        r = await c.get(
            f"/api/v1/reviews/{review.id}/training-plan",
            headers={"Authorization": f"Bearer {tok}"},
        )

    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/v1/workouts/{workout_id}
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_get_workout_by_id(client, training_ctx):
    user_id = training_ctx["user_id"]
    review = training_ctx["review"]
    tok = _token(user_id)

    async with client as c:
        plan = await _create_completed_plan(c, training_ctx, user_id, review.id)
        workout_id = plan["workouts"][1]["id"]

        get_r = await c.get(
            f"/api/v1/workouts/{workout_id}",
            headers={"Authorization": f"Bearer {tok}"},
        )

    assert get_r.status_code == 200
    assert get_r.json()["id"] == workout_id
    assert len(get_r.json()["exercises"]) == 5


@pytest.mark.anyio
async def test_get_workout_wrong_owner_returns_403(client, training_ctx):
    user_id = training_ctx["user_id"]
    other_id = uuid4()
    review = training_ctx["review"]

    async with client as c:
        plan = await _create_completed_plan(c, training_ctx, user_id, review.id)
        workout_id = plan["workouts"][0]["id"]

        get_r = await c.get(
            f"/api/v1/workouts/{workout_id}",
            headers={"Authorization": f"Bearer {_token(other_id)}"},
        )

    assert get_r.status_code == 403
