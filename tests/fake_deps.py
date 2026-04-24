"""In-memory fakes used across Phase 2 tests."""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from uuid import UUID, uuid4

from app.models.media import Media
from app.models.profile import Profile
from app.models.review import Review
from app.models.session import Session


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
            wave_conditions=kwargs["wave_conditions"],
            board_type=kwargs.get("board_type"),
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


class FakeGeminiService:
    def __init__(self, output) -> None:
        self._output = output
        self.calls: list[tuple[int, object]] = []

    def analyze_surf_media(self, images, context):
        self.calls.append((len(images), context))
        return self._output

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


def decimal(n: float) -> Decimal:
    return Decimal(str(n))
