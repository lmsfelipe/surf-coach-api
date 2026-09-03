"""Retry endpoints for reviews and training plans.

The retry routes reset a failed row to 'processing' and then enqueue the job. If
the enqueue itself fails, the row must be put back to 'failed' — otherwise it
sits in 'processing' with no job behind it and the client polls forever.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

import pytest

from app.api import ai as ai_api
from app.api import reviews as reviews_api
from app.core.deps import get_arq_pool
from app.main import app
from app.services.ai import ReviewService, TrainingService
from tests.conftest import make_token
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
    FakeTrainingPlanRepo,
    make_review_output,
    make_training_plan_output,
)

ENQUEUE_FAILED = "Processing could not be started. Please try again."


@pytest.fixture
def ctx():
    gemini = FakeGeminiService(make_review_output())
    gemini._training_output = make_training_plan_output()
    return {
        "auth": FakeAuthRepo(),
        "sessions": FakeSessionsRepo(),
        "media": FakeMediaRepo(),
        "reviews": FakeReviewRepo(),
        "plans": FakeTrainingPlanRepo(),
        "surfboards": FakeSurfboardRepo(),
        "storage": FakeStorageClient(),
        "frames": FakeFrameExtractor(frames=[b"f1"]),
        "gemini": gemini,
        "arq": FakeArqPool(),
    }


@pytest.fixture
def arq_fails(ctx):
    """Swap in a pool whose enqueue raises, as if Redis were unreachable."""
    ctx["arq"] = FakeArqPool(raise_exc=ConnectionError("redis unreachable"))
    return ctx


@pytest.fixture(autouse=True)
def _override(ctx):
    app.dependency_overrides[reviews_api.get_review_service] = lambda: ReviewService(
        sessions_repo=ctx["sessions"],  # type: ignore[arg-type]
        media_repo=ctx["media"],  # type: ignore[arg-type]
        review_repo=ctx["reviews"],  # type: ignore[arg-type]
        auth_repo=ctx["auth"],  # type: ignore[arg-type]
        surfboard_repo=ctx["surfboards"],  # type: ignore[arg-type]
        gemini=ctx["gemini"],  # type: ignore[arg-type]
        frame_extractor=ctx["frames"],  # type: ignore[arg-type]
        storage=ctx["storage"],  # type: ignore[arg-type]
    )
    app.dependency_overrides[ai_api.get_training_service] = lambda: TrainingService(
        review_repo=ctx["reviews"],  # type: ignore[arg-type]
        auth_repo=ctx["auth"],  # type: ignore[arg-type]
        training_plan_repo=ctx["plans"],  # type: ignore[arg-type]
        gemini=ctx["gemini"],  # type: ignore[arg-type]
    )
    app.dependency_overrides[get_arq_pool] = lambda: ctx["arq"]
    yield
    for dep in (reviews_api.get_review_service, ai_api.get_training_service, get_arq_pool):
        app.dependency_overrides.pop(dep, None)


async def _failed_review(ctx, user_id):
    session = await ctx["sessions"].create(
        profile_id=user_id,
        session_date=date(2026, 4, 17),
        location="Maresias",
        wave_size=1.5,
    )
    review = await ctx["reviews"].create_pending(session_id=session.id, profile_id=user_id)
    await ctx["reviews"].mark_failed(review.id, "AI service is temporarily unavailable.")
    return review


async def _failed_plan(ctx, user_id):
    review = await _failed_review(ctx, user_id)
    plan = await ctx["plans"].create_pending(review_id=review.id, profile_id=user_id)
    await ctx["plans"].mark_failed(plan.id, "AI returned an unexpected response format.")
    return plan


# ---------------------------------------------------------------------------
# POST /api/v1/reviews/{id}/retry
# ---------------------------------------------------------------------------


async def test_retry_review_resets_to_processing_and_enqueues(client, ctx, auth_headers, user_id):
    review = await _failed_review(ctx, user_id)

    async with client as c:
        r = await c.post(f"/api/v1/reviews/{review.id}/retry", headers=auth_headers)

    assert r.status_code == 202
    assert r.json()["status"] == "processing"
    assert r.json()["errorMessage"] is None
    assert ctx["arq"].jobs == [("process_review_task", (str(review.id),))]


async def test_retry_review_marks_failed_when_the_enqueue_fails(
    client, arq_fails, auth_headers, user_id
):
    """No worker will ever pick this up, so the row must not be left processing."""
    ctx = arq_fails
    review = await _failed_review(ctx, user_id)

    async with client as c:
        r = await c.post(f"/api/v1/reviews/{review.id}/retry", headers=auth_headers)

    assert r.status_code == 202
    body = r.json()
    assert body["status"] == "failed"
    assert body["errorMessage"] == ENQUEUE_FAILED
    assert (await ctx["reviews"].get(review.id)).status == "failed"


@pytest.mark.parametrize("status_value", ["completed", "processing"])
async def test_retry_review_is_refused_unless_it_failed(
    client, ctx, auth_headers, user_id, status_value
):
    review = await _failed_review(ctx, user_id)
    review.status = status_value

    async with client as c:
        r = await c.post(f"/api/v1/reviews/{review.id}/retry", headers=auth_headers)

    assert r.status_code == 409
    assert ctx["arq"].jobs == []


async def test_retry_review_rejects_another_user(client, ctx, auth_headers, user_id):
    review = await _failed_review(ctx, uuid4())  # owned by someone else

    async with client as c:
        r = await c.post(f"/api/v1/reviews/{review.id}/retry", headers=auth_headers)

    assert r.status_code == 403
    assert ctx["arq"].jobs == []


async def test_retry_unknown_review_returns_404(client, ctx, auth_headers):
    async with client as c:
        r = await c.post(f"/api/v1/reviews/{uuid4()}/retry", headers=auth_headers)
    assert r.status_code == 404


async def test_retry_review_requires_auth(client, ctx, user_id):
    review = await _failed_review(ctx, user_id)
    async with client as c:
        r = await c.post(f"/api/v1/reviews/{review.id}/retry")
    assert r.status_code == 401


# ---------------------------------------------------------------------------
# POST /api/v1/training-plans/{id}/retry
# ---------------------------------------------------------------------------


async def test_retry_plan_resets_to_processing_and_enqueues(client, ctx, auth_headers, user_id):
    plan = await _failed_plan(ctx, user_id)

    async with client as c:
        r = await c.post(f"/api/v1/training-plans/{plan.id}/retry", headers=auth_headers)

    assert r.status_code == 202
    assert r.json()["status"] == "processing"
    assert ctx["arq"].jobs == [("process_training_plan_task", (str(plan.id),))]


async def test_retry_plan_marks_failed_when_the_enqueue_fails(
    client, arq_fails, auth_headers, user_id
):
    ctx = arq_fails
    plan = await _failed_plan(ctx, user_id)

    async with client as c:
        r = await c.post(f"/api/v1/training-plans/{plan.id}/retry", headers=auth_headers)

    assert r.status_code == 202
    assert r.json()["status"] == "failed"
    assert r.json()["errorMessage"] == ENQUEUE_FAILED
    assert (await ctx["plans"].get_by_id(plan.id)).status == "failed"


@pytest.mark.parametrize("status_value", ["completed", "processing"])
async def test_retry_plan_is_refused_unless_it_failed(
    client, ctx, auth_headers, user_id, status_value
):
    plan = await _failed_plan(ctx, user_id)
    plan.status = status_value

    async with client as c:
        r = await c.post(f"/api/v1/training-plans/{plan.id}/retry", headers=auth_headers)

    assert r.status_code == 409
    assert ctx["arq"].jobs == []


async def test_retry_plan_rejects_another_user(client, ctx, auth_headers, user_id):
    plan = await _failed_plan(ctx, uuid4())

    async with client as c:
        r = await c.post(f"/api/v1/training-plans/{plan.id}/retry", headers=auth_headers)

    assert r.status_code == 403


async def test_retry_unknown_plan_returns_404(client, ctx, auth_headers):
    async with client as c:
        r = await c.post(f"/api/v1/training-plans/{uuid4()}/retry", headers=auth_headers)
    assert r.status_code == 404


async def test_a_retried_plan_can_be_reprocessed_to_completion(client, ctx, auth_headers, user_id):
    """End to end: retry clears the failure, the worker step then fills in workouts."""
    plan = await _failed_plan(ctx, user_id)

    async with client as c:
        await c.post(f"/api/v1/training-plans/{plan.id}/retry", headers=auth_headers)

    service = TrainingService(
        review_repo=ctx["reviews"],  # type: ignore[arg-type]
        auth_repo=ctx["auth"],  # type: ignore[arg-type]
        training_plan_repo=ctx["plans"],  # type: ignore[arg-type]
        gemini=ctx["gemini"],  # type: ignore[arg-type]
    )
    completed = await service.process_training_plan(plan.id)

    assert completed.status == "completed"
    assert len(completed.workouts) == 3


async def test_a_second_user_cannot_retry_via_a_forged_token(client, ctx, user_id):
    """The token's subject, not the path, decides ownership."""
    plan = await _failed_plan(ctx, user_id)
    intruder = {"Authorization": f"Bearer {make_token(uuid4())}"}

    async with client as c:
        r = await c.post(f"/api/v1/training-plans/{plan.id}/retry", headers=intruder)

    assert r.status_code == 403
