"""ReviewService pipeline behaviour that the HTTP-level tests do not reach:
the single-pass flag, tip normalisation, refinement fallback, storage-key
derivation, and the retry/stale guards.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from app.core.errors import (
    AIGenerationFailedError,
    AIParseFailedError,
    ForbiddenError,
    NoMediaForSessionError,
    NotFoundError,
    ReviewAlreadyExistsError,
    ReviewNotRetryableError,
)
from app.core.security.jwt import AuthUser
from app.services.ai import ReviewOutput, ReviewService, ScoreRubric, normalise_scores
from tests.fake_deps import (
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
def ctx():
    return {
        "auth": FakeAuthRepo(),
        "sessions": FakeSessionsRepo(),
        "media": FakeMediaRepo(),
        "reviews": FakeReviewRepo(),
        "surfboards": FakeSurfboardRepo(),
        "storage": FakeStorageClient(),
        "frames": FakeFrameExtractor(frames=[b"frame-1", b"frame-2"]),
        "gemini": FakeGeminiService(make_review_output()),
    }


def build_service(ctx) -> ReviewService:
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


async def seed_session(ctx, user_id, *, notes=None, surfboard_id=None, media_type="video"):
    """Create a session with one media row whose bytes are in the fake storage."""
    session = await ctx["sessions"].create(
        profile_id=user_id,
        session_date=date(2026, 4, 17),
        location="Praia de Santos",
        wave_size=2.0,
        surfboard_id=surfboard_id,
        notes=notes,
    )
    media_id = uuid4()
    ext = "mp4" if media_type == "video" else "jpg"
    key = f"{user_id}/{session.id}/{media_id}.{ext}"
    ctx["storage"].uploaded[key] = b"raw-image" if media_type == "image" else b"raw-video"
    await ctx["media"].create(
        session_id=session.id,
        media_type=media_type,
        storage_url=f"https://storage.test/surf-media/{key}",
        file_name=f"clip.{ext}",
    )
    return session


async def process(ctx, session, user_id):
    pending = await ctx["reviews"].create_pending(session_id=session.id, profile_id=user_id)
    return await build_service(ctx).process_review(pending.id)


# ---------------------------------------------------------------------------
# Single-pass vs two-pass
# ---------------------------------------------------------------------------


@pytest.fixture
def single_pass(monkeypatch):
    """Flip SINGLE_PASS_REVIEW on the cached Settings instance."""
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "SINGLE_PASS_REVIEW", True, raising=False)
    monkeypatch.setattr(get_settings(), "GEMINI_TEMPERATURE", 0.15, raising=False)


class _SinglePassGemini(FakeGeminiService):
    """Captures the full positional signature of the scoring call."""

    def __init__(self, output) -> None:
        super().__init__(output)
        self.analyze_args: list[tuple] = []

    def analyze_surf_media(self, images, context, description=None, temperature=None):
        self.analyze_args.append((images, context, description, temperature))
        return super().analyze_surf_media(images, context)


async def test_single_pass_sends_the_description_in_the_scoring_call(ctx, single_pass):
    user_id = uuid4()
    ctx["gemini"] = _SinglePassGemini(make_review_output())
    session = await seed_session(ctx, user_id, notes="Mandei bem no drop")

    await process(ctx, session, user_id)

    (_, _, description, temperature) = ctx["gemini"].analyze_args[0]
    assert description == "Mandei bem no drop"
    assert temperature == 0.15
    # Single pass means exactly one Gemini call — no refinement.
    assert ctx["gemini"].refine_calls == []


async def test_single_pass_with_no_notes_passes_none_not_empty_string(ctx, single_pass):
    user_id = uuid4()
    ctx["gemini"] = _SinglePassGemini(make_review_output())
    session = await seed_session(ctx, user_id, notes="   ")

    await process(ctx, session, user_id)

    assert ctx["gemini"].analyze_args[0][2] is None


async def test_two_pass_is_the_default(ctx):
    user_id = uuid4()
    session = await seed_session(ctx, user_id, notes="Relato do surfista")
    await process(ctx, session, user_id)
    assert len(ctx["gemini"].calls) == 1
    assert len(ctx["gemini"].refine_calls) == 1


async def test_whitespace_only_notes_skip_the_refinement_pass(ctx):
    user_id = uuid4()
    session = await seed_session(ctx, user_id, notes="\n  \t ")
    await process(ctx, session, user_id)
    assert ctx["gemini"].refine_calls == []


@pytest.mark.parametrize("failure", [AIGenerationFailedError, AIParseFailedError])
async def test_refinement_failure_keeps_the_media_only_review(ctx, failure):
    """Pass 2 is best-effort: its failure must not lose a completed pass-1 review."""
    user_id = uuid4()

    class _FailingRefine(FakeGeminiService):
        def refine_review_with_description(self, review, context, description):
            raise failure()

    ctx["gemini"] = _FailingRefine(make_review_output(narrative="Análise só da mídia."))
    session = await seed_session(ctx, user_id, notes="Relato do surfista")

    review = await process(ctx, session, user_id)

    assert review.status == "completed"
    assert review.narrative == "Análise só da mídia."


async def test_unexpected_refinement_error_is_not_swallowed(ctx):
    """Only the two AI errors are tolerated — anything else must surface."""
    user_id = uuid4()

    class _BrokenRefine(FakeGeminiService):
        def refine_review_with_description(self, review, context, description):
            raise RuntimeError("bug in refinement")

    ctx["gemini"] = _BrokenRefine(make_review_output())
    session = await seed_session(ctx, user_id, notes="Relato")

    with pytest.raises(RuntimeError):
        await process(ctx, session, user_id)


# ---------------------------------------------------------------------------
# Improvement-tip normalisation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("supplied", [0, 1, 2, 4, 7])
async def test_tips_are_always_normalised_to_exactly_three(ctx, supplied):
    user_id = uuid4()
    tips = [f"tip {i}" for i in range(supplied)]
    ctx["gemini"] = FakeGeminiService(make_review_output(tips=tips))
    session = await seed_session(ctx, user_id)

    review = await process(ctx, session, user_id)

    assert len(review.improvement_tips) == 3


async def test_extra_tips_are_truncated_keeping_the_first_three(ctx):
    user_id = uuid4()
    ctx["gemini"] = FakeGeminiService(
        make_review_output(tips=["one", "two", "three", "four", "five"])
    )
    session = await seed_session(ctx, user_id)

    review = await process(ctx, session, user_id)

    assert review.improvement_tips == ["one", "two", "three"]


async def test_missing_tips_are_padded_with_a_fallback(ctx):
    user_id = uuid4()
    ctx["gemini"] = FakeGeminiService(make_review_output(tips=["only one"]))
    session = await seed_session(ctx, user_id)

    review = await process(ctx, session, user_id)

    assert review.improvement_tips[0] == "only one"
    assert all(t for t in review.improvement_tips)


async def test_exactly_three_tips_pass_through_untouched(ctx):
    user_id = uuid4()
    ctx["gemini"] = FakeGeminiService(make_review_output(tips=["a", "b", "c"]))
    session = await seed_session(ctx, user_id)

    review = await process(ctx, session, user_id)

    assert review.improvement_tips == ["a", "b", "c"]


# ---------------------------------------------------------------------------
# Media handling
# ---------------------------------------------------------------------------


async def test_images_are_sent_whole_while_videos_are_sampled(ctx):
    user_id = uuid4()
    session = await seed_session(ctx, user_id, media_type="image")

    await process(ctx, session, user_id)

    images, _ = ctx["gemini"].calls[0]
    # An image contributes its own bytes once; the fake extractor, had it run,
    # would have produced two frames instead.
    assert images == 1


async def test_video_contributes_every_extracted_frame(ctx):
    user_id = uuid4()
    ctx["frames"] = FakeFrameExtractor(frames=[b"f1", b"f2", b"f3", b"f4"])
    session = await seed_session(ctx, user_id, media_type="video")

    await process(ctx, session, user_id)

    images, _ = ctx["gemini"].calls[0]
    assert images == 4


async def test_media_with_an_unparsable_storage_url_is_skipped(ctx):
    """A URL that does not carry the profile/session prefix yields no key."""
    user_id = uuid4()
    session = await ctx["sessions"].create(
        profile_id=user_id,
        session_date=date(2026, 4, 17),
        location="Praia de Santos",
        wave_size=2.0,
    )
    await ctx["media"].create(
        session_id=session.id,
        media_type="video",
        storage_url="https://cdn.example.com/some/other/path.mp4",
        file_name="clip.mp4",
    )

    with pytest.raises(NoMediaForSessionError):
        await process(ctx, session, user_id)


async def test_board_type_from_the_session_reaches_the_prompt_context(ctx):
    user_id = uuid4()
    board = await ctx["surfboards"].create(
        profile_id=user_id, board_type="longboard", board_size=9.0, volume=70.0, label="Log"
    )
    session = await seed_session(ctx, user_id, surfboard_id=board.id)

    await process(ctx, session, user_id)

    _, context = ctx["gemini"].calls[0]
    assert context.board_type == "longboard"
    assert context.wave_conditions == "2.0 m"


async def test_missing_profile_defaults_the_skill_level_to_beginner(ctx):
    user_id = uuid4()
    session = await seed_session(ctx, user_id)

    await process(ctx, session, user_id)

    _, context = ctx["gemini"].calls[0]
    assert context.skill_level == "beginner"


async def test_profile_skill_level_is_used_when_present(ctx):
    user_id = uuid4()
    await ctx["auth"].create_profile(user_id, surf_level="advanced")
    session = await seed_session(ctx, user_id)

    await process(ctx, session, user_id)

    _, context = ctx["gemini"].calls[0]
    assert context.skill_level == "advanced"


async def test_process_review_rejects_an_unknown_review(ctx):
    with pytest.raises(NotFoundError):
        await build_service(ctx).process_review(uuid4())


# ---------------------------------------------------------------------------
# Score normalisation through the pipeline
# ---------------------------------------------------------------------------


async def test_partially_null_scores_average_over_the_visible_ones(ctx):
    user_id = uuid4()
    ctx["gemini"] = FakeGeminiService(
        ReviewOutput(
            narrative="Só o drop aparece.",
            improvement_tips=["a", "b", "c"],
            scores=ScoreRubric(drop=8.0, balance=6.0),
        )
    )
    session = await seed_session(ctx, user_id)

    review = await process(ctx, session, user_id)

    assert review.score_drop == normalise_scores(ScoreRubric(drop=8.0, balance=6.0))["drop"]
    assert review.score_flow is None
    assert float(review.overall_score) == 7.0  # mean of the two present scores


def test_all_null_scores_produce_no_overall():
    assert normalise_scores(ScoreRubric())["overall"] is None


def test_scores_are_clamped_into_range():
    values = normalise_scores(ScoreRubric.model_construct(flow=12.0, drop=-3.0))
    assert float(values["flow"]) == 10.0
    assert float(values["drop"]) == 0.0


# ---------------------------------------------------------------------------
# enqueue / retry / stale guards
# ---------------------------------------------------------------------------


async def test_enqueue_rejects_a_session_owned_by_someone_else(ctx):
    owner, intruder = uuid4(), uuid4()
    session = await seed_session(ctx, owner)
    with pytest.raises(ForbiddenError):
        await build_service(ctx).enqueue_review(
            session.id, AuthUser(id=intruder, email="x@example.com")
        )


async def test_enqueue_replaces_a_previously_failed_review(ctx):
    user_id = uuid4()
    session = await seed_session(ctx, user_id)
    failed = await ctx["reviews"].create_pending(session_id=session.id, profile_id=user_id)
    await ctx["reviews"].mark_failed(failed.id, "boom")

    fresh = await build_service(ctx).enqueue_review(
        session.id, AuthUser(id=user_id, email="a@example.com")
    )

    assert fresh.id != failed.id
    assert fresh.status == "processing"
    assert await ctx["reviews"].get(failed.id) is None


@pytest.mark.parametrize("status", ["completed", "processing"])
async def test_enqueue_refuses_to_duplicate_a_live_review(ctx, status):
    user_id = uuid4()
    session = await seed_session(ctx, user_id)
    existing = await ctx["reviews"].create_pending(session_id=session.id, profile_id=user_id)
    existing.status = status

    with pytest.raises(ReviewAlreadyExistsError):
        await build_service(ctx).enqueue_review(
            session.id, AuthUser(id=user_id, email="a@example.com")
        )


@pytest.mark.parametrize("status", ["completed", "processing"])
async def test_retry_is_refused_unless_the_review_failed(ctx, status):
    user_id = uuid4()
    session = await seed_session(ctx, user_id)
    review = await ctx["reviews"].create_pending(session_id=session.id, profile_id=user_id)
    review.status = status

    with pytest.raises(ReviewNotRetryableError):
        await build_service(ctx).retry_review(
            review.id, AuthUser(id=user_id, email="a@example.com")
        )


async def test_retry_rejects_another_users_review(ctx):
    owner, intruder = uuid4(), uuid4()
    session = await seed_session(ctx, owner)
    review = await ctx["reviews"].create_pending(session_id=session.id, profile_id=owner)
    await ctx["reviews"].mark_failed(review.id, "boom")

    with pytest.raises(ForbiddenError):
        await build_service(ctx).retry_review(
            review.id, AuthUser(id=intruder, email="x@example.com")
        )


async def test_retry_clears_the_error_and_restarts_the_clock(ctx):
    user_id = uuid4()
    session = await seed_session(ctx, user_id)
    review = await ctx["reviews"].create_pending(session_id=session.id, profile_id=user_id)
    await ctx["reviews"].mark_failed(review.id, "boom")

    reset = await build_service(ctx).retry_review(
        review.id, AuthUser(id=user_id, email="a@example.com")
    )

    assert reset.status == "processing"
    assert reset.error_message is None


async def test_a_naive_processing_timestamp_is_treated_as_utc(ctx):
    """Rows written without a tz must not raise when the staleness check runs."""
    user_id = uuid4()
    session = await seed_session(ctx, user_id)
    review = await ctx["reviews"].create_pending(session_id=session.id, profile_id=user_id)
    review.processing_started_at = datetime.now(tz=UTC).replace(tzinfo=None) - timedelta(hours=2)

    out = await build_service(ctx).get_review(
        review.id, AuthUser(id=user_id, email="a@example.com")
    )

    assert out.status == "failed"
    assert out.error_message == ReviewService.STUCK_PROCESSING_MESSAGE


async def test_review_for_session_rejects_another_users_session(ctx):
    owner, intruder = uuid4(), uuid4()
    session = await seed_session(ctx, owner)
    await ctx["reviews"].create_pending(session_id=session.id, profile_id=owner)

    with pytest.raises(ForbiddenError):
        await build_service(ctx).get_review_for_session(
            session.id, AuthUser(id=intruder, email="x@example.com")
        )


async def test_review_for_session_404s_when_none_exists(ctx):
    user_id = uuid4()
    session = await seed_session(ctx, user_id)

    with pytest.raises(NotFoundError):
        await build_service(ctx).get_review_for_session(
            session.id, AuthUser(id=user_id, email="a@example.com")
        )


# ---------------------------------------------------------------------------
# Storage-key derivation
# ---------------------------------------------------------------------------


def test_extract_key_pulls_the_object_path_out_of_a_public_url():
    user_id, session_id, media_id = uuid4(), uuid4(), uuid4()
    url = f"https://x.supabase.co/storage/v1/object/public/surf-media/{user_id}/{session_id}/{media_id}.mp4"
    assert ReviewService._extract_key(url, user_id, session_id, media_id) == (
        f"{user_id}/{session_id}/{media_id}.mp4"
    )


def test_extract_key_drops_a_query_string():
    user_id, session_id, media_id = uuid4(), uuid4(), uuid4()
    url = f"https://x/{user_id}/{session_id}/{media_id}.mp4?token=abc&expires=1"
    assert ReviewService._extract_key(url, user_id, session_id, media_id) == (
        f"{user_id}/{session_id}/{media_id}.mp4"
    )


def test_extract_key_returns_none_when_the_prefix_is_absent():
    assert ReviewService._extract_key("https://x/other.mp4", uuid4(), uuid4(), uuid4()) is None
