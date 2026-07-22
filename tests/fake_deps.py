"""In-memory fakes used across Phase 2 tests."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from app.models.exercise import Exercise
from app.models.media import Media
from app.models.profile import Profile
from app.models.review import Review
from app.models.session import Session
from app.models.training_plan import TrainingPlan
from app.models.workout import Workout


class FakeAuthRepo:
    def __init__(self) -> None:
        self._profiles: dict[UUID, Profile] = {}

    async def ensure_dev_auth_user(self, user_id: UUID, email: str) -> None:
        return None

    async def get_profile(self, user_id: UUID) -> Profile | None:
        return self._profiles.get(user_id)

    async def create_profile(self, user_id: UUID, *, surf_level: str = "beginner") -> Profile:
        now = datetime.now(tz=timezone.utc)
        p = Profile(
            id=user_id,
            surf_level=surf_level,
            height_cm=None,
            weight_kg=None,
            created_at=now,
            updated_at=now,
        )
        self._profiles[user_id] = p
        return p

    async def update_profile(self, profile: Profile, fields: dict) -> Profile:
        for k, v in fields.items():
            setattr(profile, k, v)
        profile.updated_at = datetime.now(tz=timezone.utc)
        return profile


class FakeSessionsRepo:
    def __init__(self) -> None:
        self._store: dict[UUID, Session] = {}

    async def create(self, **kwargs) -> Session:
        now = datetime.now(tz=timezone.utc)
        session = Session(
            id=uuid4(),
            profile_id=kwargs["profile_id"],
            session_date=kwargs["session_date"],
            location=kwargs["location"],
            wave_size=kwargs["wave_size"],
            surfboard_id=kwargs.get("surfboard_id"),
            notes=kwargs.get("notes"),
            created_at=now,
            updated_at=now,
        )
        self._store[session.id] = session
        return session

    async def get(self, session_id: UUID) -> Session | None:
        return self._store.get(session_id)

    async def list_for_profile(self, profile_id: UUID) -> list[Session]:
        items = [s for s in self._store.values() if s.profile_id == profile_id]
        items.sort(key=lambda s: s.session_date, reverse=True)
        return items

    async def delete(self, session: Session) -> None:
        self._store.pop(session.id, None)


class FakeMediaRepo:
    def __init__(self) -> None:
        self._store: dict[UUID, Media] = {}

    async def create(self, **kwargs) -> Media:
        now = datetime.now(tz=timezone.utc)
        media = Media(
            id=uuid4(),
            session_id=kwargs["session_id"],
            media_type=kwargs["media_type"],
            storage_url=kwargs["storage_url"],
            file_name=kwargs["file_name"],
            file_size_bytes=kwargs.get("file_size_bytes"),
            duration_seconds=kwargs.get("duration_seconds"),
            created_at=now,
        )
        self._store[media.id] = media
        return media

    async def get(self, media_id: UUID) -> Media | None:
        return self._store.get(media_id)

    async def list_for_session(self, session_id: UUID) -> list[Media]:
        return [m for m in self._store.values() if m.session_id == session_id]

    async def delete(self, media: Media) -> None:
        self._store.pop(media.id, None)


    async def mark_optimized(self, media_id: UUID, size_bytes: int) -> None:
        media = self._store.get(media_id)
        if media:
            media.optimized_at = datetime.now(tz=timezone.utc)
            media.file_size_bytes = size_bytes

    async def list_unoptimized_videos(
        self, older_than_sec: int, limit: int
    ) -> list[Media]:
        return [
            m
            for m in self._store.values()
            if m.media_type == "video" and m.optimized_at is None
        ][:limit]


class FakeVideoTranscoder:
    def __init__(
        self,
        output: bytes = b"compressed-mp4-bytes",
        *,
        raise_exc: Exception | None = None,
    ) -> None:
        self._output = output
        self._raise_exc = raise_exc
        self.calls: list[int] = []

    async def transcode(self, video_bytes: bytes) -> bytes:
        self.calls.append(len(video_bytes))
        if self._raise_exc:
            raise self._raise_exc
        return self._output


class FakeReviewRepo:
    def __init__(self) -> None:
        self._store: dict[UUID, Review] = {}

    async def create(self, **kwargs) -> Review:
        now = datetime.now(tz=timezone.utc)
        review = Review(
            id=uuid4(),
            session_id=kwargs["session_id"],
            profile_id=kwargs["profile_id"],
            narrative=kwargs["narrative"],
            improvement_tips=kwargs["improvement_tips"],
            score_flow=kwargs["score_flow"],
            score_drop=kwargs["score_drop"],
            score_balance=kwargs["score_balance"],
            score_wave_selection=kwargs["score_wave_selection"],
            score_maneuvers=kwargs["score_maneuvers"],
            score_arms=kwargs["score_arms"],
            overall_score=kwargs["overall_score"],
            ai_model_version=kwargs.get("ai_model_version"),
            created_at=now,
        )
        self._store[review.id] = review
        return review

    async def get(self, review_id: UUID) -> Review | None:
        return self._store.get(review_id)

    async def get_for_session(self, session_id: UUID) -> Review | None:
        for r in self._store.values():
            if r.session_id == session_id:
                return r
        return None


class FakeStorageClient:
    def __init__(self) -> None:
        self.uploaded: dict[str, bytes] = {}
        self.deleted: list[str] = []

    def upload(self, key: str, data: bytes, content_type: str) -> str:
        self.uploaded[key] = data
        return f"https://storage.test/surf-media/{key}"

    def download(self, key: str) -> bytes:
        return self.uploaded.get(key, b"")

    async def download_range(
        self, key: str, range_header: str | None = None,
    ):
        from app.core.storage import StorageDownloadResult

        content = self.uploaded.get(key, b"")

        if range_header and content:
            range_spec = range_header.replace("bytes=", "")
            parts = range_spec.split("-")
            start = int(parts[0]) if parts[0] else 0
            end = int(parts[1]) if parts[1] else len(content) - 1
            end = min(end, len(content) - 1)
            sliced = content[start : end + 1]
            return StorageDownloadResult(
                status_code=206,
                content=sliced,
                content_type="image/jpeg",
                content_length=len(sliced),
                content_range=f"bytes {start}-{end}/{len(content)}",
            )

        return StorageDownloadResult(
            status_code=200,
            content=content,
            content_type="image/jpeg",
            content_length=len(content),
        )

    def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.uploaded.pop(key, None)


class FakeFrameExtractor:
    def __init__(self, frames: list[bytes] | None = None, duration: float = 10.0) -> None:
        self._frames = frames or [b"frame-bytes"]
        self._duration = duration

    def extract(self, video_bytes: bytes, frame_count: int = 6) -> list[bytes]:
        return list(self._frames)

    def probe_duration(self, video_bytes: bytes) -> float:
        return self._duration


class FakeTrainingPlanRepo:
    def __init__(self) -> None:
        self._plans: dict[UUID, TrainingPlan] = {}
        self._workouts: dict[UUID, Workout] = {}

    async def get_by_id(self, plan_id: UUID) -> TrainingPlan | None:
        return self._plans.get(plan_id)

    async def get_by_review_id(self, review_id: UUID) -> TrainingPlan | None:
        for p in self._plans.values():
            if p.review_id == review_id:
                return p
        return None

    async def get_workout_by_id(self, workout_id: UUID) -> Workout | None:
        return self._workouts.get(workout_id)

    async def create(self, *, review_id, profile_id, ai_model_version, workouts) -> TrainingPlan:
        now = datetime.now(tz=timezone.utc)
        plan = TrainingPlan(
            id=uuid4(),
            review_id=review_id,
            profile_id=profile_id,
            generated_by="ai",
            ai_model_version=ai_model_version,
            created_at=now,
        )
        plan_workouts = []
        for w_data in workouts:
            workout = Workout(
                id=uuid4(),
                plan_id=plan.id,
                sequence_number=w_data["sequence_number"],
                title=w_data["title"],
                focus_area=w_data["focus_area"],
                created_at=now,
            )
            workout_exercises = []
            for idx, ex_data in enumerate(w_data["exercises"], start=1):
                exercise = Exercise(
                    id=uuid4(),
                    workout_id=workout.id,
                    sequence_number=idx,
                    name=ex_data["name"],
                    description=ex_data["description"],
                    sets=ex_data["sets"],
                    reps=ex_data["reps"],
                    video_url=ex_data.get("video_url"),
                    created_at=now,
                )
                workout_exercises.append(exercise)
            workout.exercises = workout_exercises
            self._workouts[workout.id] = workout
            plan_workouts.append(workout)
        plan.workouts = plan_workouts
        self._plans[plan.id] = plan
        return plan


class FakeGeminiService:
    def __init__(self, output) -> None:
        self._output = output
        self.calls: list[tuple[int, object]] = []
        self._training_output = None
        self._moderation_output = None
        self.moderation_calls: list[int] = []

    def analyze_surf_media(self, images, context):
        self.calls.append((len(images), context))
        return self._output

    def moderate_media_content(self, images, mime_type="image/jpeg"):
        self.moderation_calls.append(len(images))
        if self._moderation_output is not None:
            return self._moderation_output
        from app.services.ai import ModerationOutput
        return ModerationOutput(surf_related=True, explicit_content=False, reason="Surf content.")

    def generate_training_plan(self, context):
        return self._training_output

    @staticmethod
    def parse_response(raw: str):
        from app.services.ai import GeminiService

        return GeminiService.parse_response(raw)


def make_review_output(
    *,
    narrative: str = "Great paddle technique and consistent pop-ups.",
    tips: list[str] | None = None,
    flow: float = 7.2,
    drop: float = 6.8,
    balance: float = 7.5,
    wave_selection: float = 6.0,
    maneuvers: float = 5.5,
    arms: float = 6.5,
):
    from app.services.ai import ReviewOutput, ScoreRubric

    return ReviewOutput(
        narrative=narrative,
        improvement_tips=tips
        or [
            "Shift rear foot further back during bottom turns.",
            "Keep arms lower and parallel to the board surface.",
            "Practice reading the wave face earlier.",
        ],
        scores=ScoreRubric(
            flow=flow,
            drop=drop,
            balance=balance,
            wave_selection=wave_selection,
            maneuvers=maneuvers,
            arms=arms,
        ),
    )


def make_moderation_output(
    *,
    surf_related: bool = True,
    explicit_content: bool = False,
    reason: str = "Surf content detected.",
):
    from app.services.ai import ModerationOutput

    return ModerationOutput(
        surf_related=surf_related,
        explicit_content=explicit_content,
        reason=reason,
    )


def decimal(n: float) -> Decimal:
    return Decimal(str(n))


def make_training_plan_output(workout_count: int = 3, exercises_per_workout: int = 5):
    from app.services.ai import ExerciseOutput, TrainingPlanOutput, WorkoutOutput

    workouts = []
    for i in range(1, workout_count + 1):
        exercises = [
            ExerciseOutput(
                name=f"Exercise {j}",
                description=f"Description for exercise {j} in workout {i}.",
                sets=3,
                reps="10",
                video_url=None,
            )
            for j in range(1, exercises_per_workout + 1)
        ]
        workouts.append(
            WorkoutOutput(
                sequence_number=i,
                title=f"Workout {i} Title",
                focus_area=f"focus area {i}",
                exercises=exercises,
            )
        )
    return TrainingPlanOutput(workouts=workouts)
