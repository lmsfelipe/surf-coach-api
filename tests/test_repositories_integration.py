"""Integration tests for the repository layer, against a real Postgres.

Every other suite substitutes an in-memory fake for these classes, so nothing
else executes the actual SQL: ordering clauses, cascade deletes, server defaults,
JSON round-tripping and the RETURNING-order guarantee in ``create_many`` are all
invisible to the fakes. tests/test_repo_contracts.py keeps the fakes' shape
honest; this file keeps the real queries honest.

Skipped automatically when no database is reachable. To run locally:

    docker compose up -d db
    alembic upgrade head
    pytest tests/test_repositories_integration.py

CI already provisions Postgres and runs the migrations, so these run there.
"""

from __future__ import annotations

import os
from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.repositories.ai import ReviewRepository, TrainingPlanRepository
from app.repositories.auth import AuthRepository
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository
from app.repositories.surfboards import SurfboardRepository

DB_URL = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL", "")


@pytest.fixture
async def db() -> AsyncSession:
    """A session whose writes are rolled back after each test.

    The repositories call ``commit()``, so the session joins an outer transaction
    via savepoints: each commit releases a savepoint, and the outer rollback then
    discards everything the test wrote — no cross-test bleed, no truncation step.

    The engine is built per test rather than per module: an asyncpg connection is
    bound to the event loop that opened it, and pytest-asyncio gives each test a
    fresh loop, so a shared engine would fail with "another operation is in
    progress".
    """
    if not DB_URL:
        pytest.skip("No DATABASE_URL configured")

    engine = create_async_engine(DB_URL, poolclass=NullPool)
    try:
        connection = await engine.connect()
    except Exception as exc:  # noqa: BLE001 — unreachable database means skip, not fail
        await engine.dispose()
        pytest.skip(f"Database not reachable: {exc}")

    try:
        transaction = await connection.begin()
        try:
            # An un-migrated database must skip rather than report spurious failures.
            try:
                await connection.execute(text("SELECT 1 FROM public.profiles LIMIT 1"))
            except Exception as exc:  # noqa: BLE001
                pytest.skip(f"Database not migrated (run `alembic upgrade head`): {exc}")

            maker = async_sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            session = maker()
            try:
                yield session
            finally:
                await session.close()
        finally:
            await transaction.rollback()
    finally:
        await connection.close()
        await engine.dispose()


@pytest.fixture
async def profile_id(db) -> str:
    """A committed profile — everything else FKs to it."""
    user_id = uuid4()
    repo = AuthRepository(db)
    await repo.ensure_dev_auth_user(user_id, f"{user_id}@example.com")
    await repo.create_profile(user_id, surf_level="intermediate")
    return user_id


async def _make_session(db, profile_id, *, session_date=date(2026, 4, 17), surfboard_id=None):
    return await SessionsRepository(db).create(
        profile_id=profile_id,
        session_date=session_date,
        location="Maresias",
        wave_size=1.5,
        surfboard_id=surfboard_id,
        notes=None,
    )


async def _backdate(db, table: str, row_id, seconds: int) -> None:
    """Push a row's created_at into the past.

    Everything in one test shares a transaction, and Postgres ``now()`` is
    transaction-scoped — so rows written here all carry the *same* created_at.
    Ordering and age-window queries need real spread, which only an explicit
    timestamp provides.
    """
    await db.execute(
        text(
            f"UPDATE public.{table} "
            "SET created_at = now() - make_interval(secs => :secs) WHERE id = :id"
        ),
        {"secs": seconds, "id": str(row_id)},
    )
    await db.commit()


# ---------------------------------------------------------------------------
# AuthRepository
# ---------------------------------------------------------------------------


async def test_a_created_profile_is_readable_back(db):
    user_id = uuid4()
    repo = AuthRepository(db)
    await repo.ensure_dev_auth_user(user_id, "a@example.com")

    created = await repo.create_profile(user_id, surf_level="advanced")
    fetched = await repo.get_profile(user_id)

    assert fetched is not None
    assert fetched.id == created.id == user_id
    assert fetched.surf_level == "advanced"


async def test_profile_timestamps_are_filled_by_the_server(db):
    user_id = uuid4()
    repo = AuthRepository(db)
    await repo.ensure_dev_auth_user(user_id, "a@example.com")

    profile = await repo.create_profile(user_id)

    assert profile.created_at is not None
    assert profile.updated_at is not None


async def test_an_unknown_profile_reads_as_none(db):
    assert await AuthRepository(db).get_profile(uuid4()) is None


async def test_profile_updates_persist(db, profile_id):
    repo = AuthRepository(db)
    profile = await repo.get_profile(profile_id)

    await repo.update_profile(profile, {"height_cm": 180, "weight_kg": 75, "name": "Ana"})

    reread = await repo.get_profile(profile_id)
    assert (reread.height_cm, reread.weight_kg, reread.name) == (180, 75, "Ana")


async def test_ensure_dev_auth_user_is_idempotent(db):
    user_id = uuid4()
    repo = AuthRepository(db)
    await repo.ensure_dev_auth_user(user_id, "a@example.com")
    await repo.ensure_dev_auth_user(user_id, "a@example.com")  # ON CONFLICT DO NOTHING


# ---------------------------------------------------------------------------
# SessionsRepository
# ---------------------------------------------------------------------------


async def test_a_created_session_is_readable_back(db, profile_id):
    created = await _make_session(db, profile_id)
    fetched = await SessionsRepository(db).get(created.id)

    assert fetched is not None
    assert fetched.profile_id == profile_id
    assert fetched.location == "Maresias"
    assert float(fetched.wave_size) == 1.5


async def test_sessions_list_newest_first(db, profile_id):
    repo = SessionsRepository(db)
    await _make_session(db, profile_id, session_date=date(2026, 4, 10))
    await _make_session(db, profile_id, session_date=date(2026, 4, 20))
    await _make_session(db, profile_id, session_date=date(2026, 4, 15))

    dates = [s.session_date for s in await repo.list_for_profile(profile_id)]

    assert dates == sorted(dates, reverse=True)


async def test_sessions_are_scoped_to_their_profile(db, profile_id):
    other = uuid4()
    auth = AuthRepository(db)
    await auth.ensure_dev_auth_user(other, "b@example.com")
    await auth.create_profile(other)
    await _make_session(db, profile_id)

    assert await SessionsRepository(db).list_for_profile(other) == []


async def test_deleting_a_session_removes_it(db, profile_id):
    repo = SessionsRepository(db)
    session = await _make_session(db, profile_id)

    await repo.delete(session)

    assert await repo.get(session.id) is None


async def test_a_session_cannot_reference_a_missing_profile(db):
    """The FK is the last line of defence behind the service-level ownership check."""
    with pytest.raises(IntegrityError):
        await _make_session(db, uuid4())


# ---------------------------------------------------------------------------
# SurfboardRepository
# ---------------------------------------------------------------------------


async def test_a_created_board_is_readable_back(db, profile_id):
    repo = SurfboardRepository(db)
    board = await repo.create(
        profile_id=profile_id, board_type="shortboard", board_size=5.9, volume=27.5, label="Daily"
    )

    fetched = await repo.get_by_id(board.id)

    assert fetched is not None
    assert fetched.board_type == "shortboard"
    assert float(fetched.board_size) == 5.9
    assert float(fetched.volume) == 27.5


async def test_boards_list_newest_first(db, profile_id):
    repo = SurfboardRepository(db)
    for age, label in ((300, "oldest"), (200, "middle"), (100, "newest")):
        board = await repo.create(
            profile_id=profile_id, board_type="funboard", board_size=7.0, volume=45.0, label=label
        )
        await _backdate(db, "surfboards", board.id, age)

    boards = await repo.get_all_by_profile(profile_id)

    assert [b.label for b in boards] == ["newest", "middle", "oldest"]


async def test_optional_board_fields_round_trip_as_null(db, profile_id):
    repo = SurfboardRepository(db)
    board = await repo.create(
        profile_id=profile_id, board_type="bodyboard", board_size=3.5, volume=None, label=None
    )

    fetched = await repo.get_by_id(board.id)

    assert fetched.volume is None
    assert fetched.label is None


async def test_board_updates_persist(db, profile_id):
    repo = SurfboardRepository(db)
    board = await repo.create(
        profile_id=profile_id, board_type="shortboard", board_size=5.9, volume=27.0, label="Old"
    )

    await repo.update(board, {"label": "New", "board_size": 6.2})

    fetched = await repo.get_by_id(board.id)
    assert fetched.label == "New"
    assert float(fetched.board_size) == 6.2


async def test_deleting_a_board_removes_it(db, profile_id):
    repo = SurfboardRepository(db)
    board = await repo.create(
        profile_id=profile_id, board_type="longboard", board_size=9.0, volume=70.0, label=None
    )

    await repo.delete(board)

    assert await repo.get_by_id(board.id) is None


async def test_a_session_can_reference_a_board(db, profile_id):
    board = await SurfboardRepository(db).create(
        profile_id=profile_id, board_type="longboard", board_size=9.0, volume=70.0, label=None
    )

    session = await _make_session(db, profile_id, surfboard_id=board.id)

    assert (await SessionsRepository(db).get(session.id)).surfboard_id == board.id


# ---------------------------------------------------------------------------
# MediaRepository
# ---------------------------------------------------------------------------


async def _media_kwargs(session_id, **overrides):
    base = {
        "session_id": session_id,
        "media_type": "image",
        "storage_url": "https://storage.test/surf-media/a/b/c.jpg",
        "file_name": "photo.jpg",
        "file_size_bytes": 1024,
        "duration_seconds": None,
    }
    base.update(overrides)
    return base


async def test_a_created_media_row_is_readable_back(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = MediaRepository(db)

    media = await repo.create(**await _media_kwargs(session.id))

    fetched = await repo.get(media.id)
    assert fetched is not None
    assert fetched.file_name == "photo.jpg"
    assert fetched.file_size_bytes == 1024


async def test_a_video_row_keeps_its_duration_precision(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = MediaRepository(db)

    media = await repo.create(
        **await _media_kwargs(
            session.id,
            media_type="video",
            duration_seconds=Decimal("12.34"),
            file_name="clip.mp4",
        )
    )

    assert (await repo.get(media.id)).duration_seconds == Decimal("12.34")


async def test_optimize_attempts_defaults_to_zero(db, profile_id):
    """The poison-pill guard counts up from this server default."""
    session = await _make_session(db, profile_id)
    media = await MediaRepository(db).create(**await _media_kwargs(session.id))

    assert media.optimize_attempts == 0
    assert media.optimized_at is None


async def test_create_many_preserves_input_order(db, profile_id):
    """sort_by_parameter_order: the 207 response pairs results with upload order."""
    from app.services.media import _PreparedMedia

    session = await _make_session(db, profile_id)
    items = [
        _PreparedMedia(
            media_type="image",
            storage_url=f"https://storage.test/{i}.jpg",
            file_name=f"{i:02d}.jpg",
            file_size_bytes=100 + i,
            duration_seconds=None,
        )
        for i in range(10)
    ]

    created = await MediaRepository(db).create_many(session_id=session.id, items=items)

    assert [m.file_name for m in created] == [f"{i:02d}.jpg" for i in range(10)]


async def test_create_many_with_no_items_writes_nothing(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = MediaRepository(db)

    assert await repo.create_many(session_id=session.id, items=[]) == []
    assert await repo.list_for_session(session.id) == []


async def test_media_lists_oldest_first(db, profile_id):
    """Ascending, so the gallery shows the session in the order it was shot."""
    session = await _make_session(db, profile_id)
    repo = MediaRepository(db)
    for age, name in ((300, "first.jpg"), (200, "second.jpg"), (100, "third.jpg")):
        media = await repo.create(**await _media_kwargs(session.id, file_name=name))
        await _backdate(db, "media", media.id, age)

    rows = await repo.list_for_session(session.id)

    assert [r.file_name for r in rows] == ["first.jpg", "second.jpg", "third.jpg"]


async def test_deleting_media_removes_the_row(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = MediaRepository(db)
    media = await repo.create(**await _media_kwargs(session.id))

    await repo.delete(media)

    assert await repo.get(media.id) is None


async def test_deleting_a_session_cascades_to_its_media(db, profile_id):
    session = await _make_session(db, profile_id)
    media_repo = MediaRepository(db)
    media = await media_repo.create(**await _media_kwargs(session.id))

    await SessionsRepository(db).delete(session)

    assert await media_repo.get(media.id) is None


async def test_mark_optimized_stamps_the_time_and_new_size(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = MediaRepository(db)
    media = await repo.create(**await _media_kwargs(session.id, media_type="video"))

    await repo.mark_optimized(media.id, 512)

    # mark_optimized issues raw SQL that bypasses the ORM, so the row is read
    # back the same way rather than through the (now stale) identity map.
    row = (
        await db.execute(
            text("SELECT optimized_at, file_size_bytes FROM public.media WHERE id = :id"),
            {"id": str(media.id)},
        )
    ).one()
    assert row.optimized_at is not None
    assert row.file_size_bytes == 512


async def test_increment_optimize_attempts_accumulates(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = MediaRepository(db)
    media = await repo.create(**await _media_kwargs(session.id, media_type="video"))

    await repo.increment_optimize_attempts(media.id)
    await repo.increment_optimize_attempts(media.id)

    attempts = await db.scalar(
        text("SELECT optimize_attempts FROM public.media WHERE id = :id"), {"id": str(media.id)}
    )
    assert attempts == 2


async def test_the_optimize_sweep_respects_the_grace_period(db, profile_id):
    """A just-uploaded video is still within its grace window, so it is not swept."""
    session = await _make_session(db, profile_id)
    repo = MediaRepository(db)
    media = await repo.create(**await _media_kwargs(session.id, media_type="video"))
    await _backdate(db, "media", media.id, 600)  # ten minutes old

    within_grace = await repo.list_unoptimized_videos(older_than_sec=900, limit=10, max_attempts=3)
    assert media.id not in {m.id for m in within_grace}

    past_grace = await repo.list_unoptimized_videos(older_than_sec=300, limit=10, max_attempts=3)
    assert media.id in {m.id for m in past_grace}


async def test_the_optimize_sweep_skips_images(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = MediaRepository(db)
    image = await repo.create(**await _media_kwargs(session.id, media_type="image"))
    await _backdate(db, "media", image.id, 3600)

    rows = await repo.list_unoptimized_videos(older_than_sec=0, limit=10, max_attempts=3)

    assert image.id not in {m.id for m in rows}


async def test_the_optimize_sweep_skips_already_optimized_videos(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = MediaRepository(db)
    media = await repo.create(**await _media_kwargs(session.id, media_type="video"))
    await _backdate(db, "media", media.id, 3600)
    await repo.mark_optimized(media.id, 100)

    rows = await repo.list_unoptimized_videos(older_than_sec=0, limit=10, max_attempts=3)

    assert media.id not in {m.id for m in rows}


async def test_the_optimize_sweep_gives_up_after_max_attempts(db, profile_id):
    """The poison-pill guard: a video that keeps failing must stop being re-enqueued."""
    session = await _make_session(db, profile_id)
    repo = MediaRepository(db)
    media = await repo.create(**await _media_kwargs(session.id, media_type="video"))
    await _backdate(db, "media", media.id, 3600)

    for _ in range(2):
        await repo.increment_optimize_attempts(media.id)
    still_eligible = await repo.list_unoptimized_videos(older_than_sec=0, limit=10, max_attempts=3)
    assert media.id in {m.id for m in still_eligible}

    await repo.increment_optimize_attempts(media.id)  # third strike
    exhausted = await repo.list_unoptimized_videos(older_than_sec=0, limit=10, max_attempts=3)
    assert media.id not in {m.id for m in exhausted}


async def test_the_optimize_sweep_honours_its_batch_limit(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = MediaRepository(db)
    for i in range(5):
        media = await repo.create(
            **await _media_kwargs(session.id, media_type="video", file_name=f"{i}.mp4")
        )
        await _backdate(db, "media", media.id, 3600)

    assert len(await repo.list_unoptimized_videos(older_than_sec=0, limit=2, max_attempts=3)) == 2


# ---------------------------------------------------------------------------
# ReviewRepository
# ---------------------------------------------------------------------------


async def test_a_pending_review_starts_processing_with_a_start_time(db, profile_id):
    session = await _make_session(db, profile_id)

    review = await ReviewRepository(db).create_pending(session_id=session.id, profile_id=profile_id)

    assert review.status == "processing"
    assert review.processing_started_at is not None
    assert review.error_message is None


async def test_marking_a_review_completed_persists_scores_and_tips(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = ReviewRepository(db)
    review = await repo.create_pending(session_id=session.id, profile_id=profile_id)

    await repo.mark_completed(
        review.id,
        narrative="Boa sessão.",
        improvement_tips=["a", "b", "c"],
        score_flow=Decimal("7.2"),
        score_drop=Decimal("6.8"),
        score_balance=None,
        score_wave_selection=Decimal("6.0"),
        score_maneuvers=Decimal("5.5"),
        score_arms=Decimal("6.5"),
        overall_score=Decimal("6.4"),
        ai_model_version="gemini-test",
    )

    fetched = await repo.get(review.id)
    assert fetched.status == "completed"
    assert fetched.narrative == "Boa sessão."
    assert fetched.improvement_tips == ["a", "b", "c"]  # JSON round-trip
    assert fetched.score_flow == Decimal("7.2")
    assert fetched.score_balance is None  # a null score stays null
    assert fetched.ai_model_version == "gemini-test"


async def test_marking_a_review_failed_records_the_message(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = ReviewRepository(db)
    review = await repo.create_pending(session_id=session.id, profile_id=profile_id)

    await repo.mark_failed(review.id, "AI service is temporarily unavailable.")

    fetched = await repo.get(review.id)
    assert fetched.status == "failed"
    assert fetched.error_message == "AI service is temporarily unavailable."


async def test_retrying_a_review_clears_the_error(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = ReviewRepository(db)
    review = await repo.create_pending(session_id=session.id, profile_id=profile_id)
    await repo.mark_failed(review.id, "boom")

    await repo.reset_for_retry(review.id)

    fetched = await repo.get(review.id)
    assert fetched.status == "processing"
    assert fetched.error_message is None


async def test_a_review_is_findable_by_its_session(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = ReviewRepository(db)
    review = await repo.create_pending(session_id=session.id, profile_id=profile_id)

    assert (await repo.get_for_session(session.id)).id == review.id


async def test_no_review_for_a_session_reads_as_none(db, profile_id):
    session = await _make_session(db, profile_id)
    assert await ReviewRepository(db).get_for_session(session.id) is None


async def test_deleting_a_review_removes_it(db, profile_id):
    session = await _make_session(db, profile_id)
    repo = ReviewRepository(db)
    review = await repo.create_pending(session_id=session.id, profile_id=profile_id)

    await repo.delete(review.id)

    assert await repo.get(review.id) is None


async def test_deleting_an_absent_review_is_a_no_op(db):
    await ReviewRepository(db).delete(uuid4())  # must not raise


# ---------------------------------------------------------------------------
# TrainingPlanRepository
# ---------------------------------------------------------------------------


def _workouts(count: int = 3, exercises: int = 4) -> list[dict]:
    return [
        {
            "sequence_number": i,
            "title": f"Workout {i}",
            "focus_area": f"focus {i}",
            "exercises": [
                {
                    "name": f"Exercise {j}",
                    "description": "Execute com o tronco vertical.",
                    "sets": 3,
                    "reps": "10",
                    "video_url": None,
                }
                for j in range(1, exercises + 1)
            ],
        }
        for i in range(1, count + 1)
    ]


async def _review_for_plan(db, profile_id):
    session = await _make_session(db, profile_id)
    return await ReviewRepository(db).create_pending(session_id=session.id, profile_id=profile_id)


async def test_a_pending_plan_starts_processing_with_no_workouts(db, profile_id):
    review = await _review_for_plan(db, profile_id)

    plan = await TrainingPlanRepository(db).create_pending(
        review_id=review.id, profile_id=profile_id
    )

    assert plan.status == "processing"
    assert plan.workouts == []  # eager-loaded, so this is a real empty relation


async def test_completing_a_plan_writes_workouts_and_exercises(db, profile_id):
    review = await _review_for_plan(db, profile_id)
    repo = TrainingPlanRepository(db)
    plan = await repo.create_pending(review_id=review.id, profile_id=profile_id)

    completed = await repo.mark_completed(
        plan.id, ai_model_version="gemini-test", workouts=_workouts(3, 4)
    )

    assert completed.status == "completed"
    assert len(completed.workouts) == 3
    assert all(len(w.exercises) == 4 for w in completed.workouts)


async def test_exercises_are_numbered_in_order_within_a_workout(db, profile_id):
    review = await _review_for_plan(db, profile_id)
    repo = TrainingPlanRepository(db)
    plan = await repo.create_pending(review_id=review.id, profile_id=profile_id)

    completed = await repo.mark_completed(plan.id, ai_model_version=None, workouts=_workouts(1, 5))

    numbers = sorted(ex.sequence_number for ex in completed.workouts[0].exercises)
    assert numbers == [1, 2, 3, 4, 5]


async def test_a_plan_is_findable_by_its_review(db, profile_id):
    review = await _review_for_plan(db, profile_id)
    repo = TrainingPlanRepository(db)
    plan = await repo.create_pending(review_id=review.id, profile_id=profile_id)

    assert (await repo.get_by_review_id(review.id)).id == plan.id


async def test_plans_list_newest_first(db, profile_id):
    repo = TrainingPlanRepository(db)
    ids = []
    for age in (300, 200, 100):
        review = await _review_for_plan(db, profile_id)
        plan = await repo.create_pending(review_id=review.id, profile_id=profile_id)
        await _backdate(db, "training_plans", plan.id, age)
        ids.append(plan.id)

    listed = [p.id for p in await repo.list_for_profile(profile_id)]

    assert listed == list(reversed(ids))


async def test_a_workout_is_fetchable_on_its_own_with_its_exercises(db, profile_id):
    review = await _review_for_plan(db, profile_id)
    repo = TrainingPlanRepository(db)
    plan = await repo.create_pending(review_id=review.id, profile_id=profile_id)
    completed = await repo.mark_completed(plan.id, ai_model_version=None, workouts=_workouts(2, 4))
    target = completed.workouts[0]

    fetched = await repo.get_workout_by_id(target.id)

    assert fetched is not None
    assert len(fetched.exercises) == 4


async def test_deleting_a_plan_cascades_to_workouts_and_exercises(db, profile_id):
    review = await _review_for_plan(db, profile_id)
    repo = TrainingPlanRepository(db)
    plan = await repo.create_pending(review_id=review.id, profile_id=profile_id)
    completed = await repo.mark_completed(plan.id, ai_model_version=None, workouts=_workouts(2, 3))
    workout_id = completed.workouts[0].id

    await repo.delete(plan.id)

    assert await repo.get_by_id(plan.id) is None
    assert await repo.get_workout_by_id(workout_id) is None


async def test_marking_a_plan_failed_records_the_message(db, profile_id):
    review = await _review_for_plan(db, profile_id)
    repo = TrainingPlanRepository(db)
    plan = await repo.create_pending(review_id=review.id, profile_id=profile_id)

    failed = await repo.mark_failed(plan.id, "AI returned an unexpected response format.")

    assert failed.status == "failed"
    assert failed.error_message == "AI returned an unexpected response format."


async def test_retrying_a_plan_clears_the_error(db, profile_id):
    review = await _review_for_plan(db, profile_id)
    repo = TrainingPlanRepository(db)
    plan = await repo.create_pending(review_id=review.id, profile_id=profile_id)
    await repo.mark_failed(plan.id, "boom")

    reset = await repo.reset_for_retry(plan.id)

    assert reset.status == "processing"
    assert reset.error_message is None


async def test_create_writes_a_completed_plan_in_one_go(db, profile_id):
    review = await _review_for_plan(db, profile_id)

    plan = await TrainingPlanRepository(db).create(
        review_id=review.id,
        profile_id=profile_id,
        ai_model_version="gemini-test",
        workouts=_workouts(3, 4),
    )

    assert len(plan.workouts) == 3
    assert plan.ai_model_version == "gemini-test"


async def test_an_unknown_plan_reads_as_none(db):
    assert await TrainingPlanRepository(db).get_by_id(uuid4()) is None
