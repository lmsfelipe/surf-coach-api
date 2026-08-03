"""Tests for concurrent batch store + bulk persist on POST /sessions/{id}/media/.

Covers SPEC_BACKEND_Media_Upload_Optimization.md §6: one session fetch + one
commit per batch, bounded-concurrency stores, order preservation, and
storage-stage partial success (207) while validation/moderation stay
whole-request.
"""

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from jose import jwt

from app.api import media as media_api
from app.api import sessions as sessions_api
from app.core.config import get_settings
from app.core.errors import StorageUploadFailedError
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
JPEG = JPEG_PATH.read_bytes()


def _photo(marker: bytes = b"") -> bytes:
    """A valid JPEG with optional trailing marker bytes.

    libmagic sniffs the SOI header, so trailing bytes keep the detected type
    image/jpeg while giving each part distinguishable content the fake storage
    can key failure/latency off of.
    """
    return JPEG + marker


# ---------------------------------------------------------------------------
# Instrumented fakes
# ---------------------------------------------------------------------------


class CountingSessionsRepo(FakeSessionsRepo):
    def __init__(self) -> None:
        super().__init__()
        self.get_calls = 0

    async def get(self, session_id: UUID):
        self.get_calls += 1
        return await super().get(session_id)


class RecordingMediaRepo(FakeMediaRepo):
    def __init__(self) -> None:
        super().__init__()
        self.create_calls = 0
        self.create_many_calls = 0
        self.last_items: list | None = None

    async def create(self, **kwargs):
        self.create_calls += 1
        return await super().create(**kwargs)

    async def create_many(self, *, session_id, items):
        self.create_many_calls += 1
        self.last_items = list(items)
        return await super().create_many(session_id=session_id, items=items)


class InstrumentedStorage(FakeStorageClient):
    """Records in-flight concurrency and can fail / delay specific parts by content."""

    def __init__(
        self,
        *,
        default_delay: float = 0.0,
        fail_on: bytes | None = None,
        fail_exc: Exception | None = None,
        content_delays: list[tuple[bytes, float]] | None = None,
    ) -> None:
        super().__init__()
        self._default_delay = default_delay
        self._fail_on = fail_on
        self._fail_exc = fail_exc or StorageUploadFailedError()
        self._content_delays = content_delays or []
        self._lock = threading.Lock()
        self.in_flight = 0
        self.max_in_flight = 0
        self.upload_file_calls = 0

    def upload_file(self, key: str, path: str, content_type: str) -> str:
        with self._lock:
            self.in_flight += 1
            self.max_in_flight = max(self.max_in_flight, self.in_flight)
            self.upload_file_calls += 1
        try:
            with open(path, "rb") as fh:
                data = fh.read()
            delay = self._default_delay
            for marker, secs in self._content_delays:
                if marker in data:
                    delay = secs
                    break
            if delay:
                time.sleep(delay)
            if self._fail_on is not None and self._fail_on in data:
                raise self._fail_exc
            self.uploaded[key] = data
            return f"https://storage.test/surf-media/{key}"
        finally:
            with self._lock:
                self.in_flight -= 1


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def batch(monkeypatch):
    """Wire fakes into the media/sessions services and return the instrumented set.

    A single sessions_repo is shared between both services, exactly as
    get_media_service does in production (one AsyncSession per request).
    """
    handles: dict[str, object] = {}

    def _apply(
        *,
        upload_concurrency: int = 4,
        storage: InstrumentedStorage | None = None,
        gemini=None,
    ) -> dict[str, object]:
        monkeypatch.setenv("UPLOAD_CONCURRENCY", str(upload_concurrency))
        if gemini is not None:
            monkeypatch.setenv("CONTENT_MODERATION_ENABLED", "true")
        get_settings.cache_clear()

        sessions_repo = CountingSessionsRepo()
        media_repo = RecordingMediaRepo()
        store = storage or InstrumentedStorage()
        frames = FakeFrameExtractor(duration=5.0)

        app.dependency_overrides[sessions_api.get_sessions_service] = lambda: SessionsService(
            sessions_repo  # type: ignore[arg-type]
        )
        app.dependency_overrides[media_api.get_media_service] = lambda: MediaService(
            media_repo=media_repo,  # type: ignore[arg-type]
            sessions_repo=sessions_repo,  # type: ignore[arg-type]
            storage=store,  # type: ignore[arg-type]
            frame_extractor=frames,  # type: ignore[arg-type]
            gemini=gemini,
        )
        handles.update(sessions_repo=sessions_repo, media_repo=media_repo, storage=store)
        return handles

    yield _apply

    app.dependency_overrides.pop(sessions_api.get_sessions_service, None)
    app.dependency_overrides.pop(media_api.get_media_service, None)
    get_settings.cache_clear()


@pytest.fixture
def fake_video_magic(monkeypatch):
    """Detect a sentinel byte prefix as video/mp4.

    libmagic will not recognise synthetic MP4 headers on every platform, so a
    scoped monkeypatch lets the video path be exercised over HTTP without a real
    video fixture. Real JPEGs still sniff normally.
    """
    import app.services.media as media_mod

    real = media_mod.magic.from_buffer

    def _fake(head, mime=True):
        if head.startswith(b"FAKEVIDEO"):
            return "video/mp4"
        return real(head, mime=mime)

    monkeypatch.setattr(media_mod.magic, "from_buffer", _fake)


def _token(user_id: UUID, email: str = "surfer@example.com") -> str:
    payload = {
        "sub": str(user_id),
        "email": email,
        "aud": "authenticated",
        "exp": datetime.now(tz=UTC) + timedelta(hours=1),
    }
    return jwt.encode(payload, get_settings().SUPABASE_JWT_SECRET, algorithm="HS256")


@pytest.fixture
def client():
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _create_session(c: AsyncClient, user_id: UUID) -> str:
    r = await c.post(
        "/api/v1/sessions/",
        headers={"Authorization": f"Bearer {_token(user_id)}"},
        json={"sessionDate": "2026-04-17", "location": "Praia de Santos", "waveSize": 1.5},
    )
    assert r.status_code == 201
    return r.json()["id"]


def _files(names_and_content: list[tuple[str, bytes]]) -> list[tuple[str, tuple]]:
    return [("file", (name, content, "image/jpeg")) for name, content in names_and_content]


# ---------------------------------------------------------------------------
# One fetch, one commit
# ---------------------------------------------------------------------------


async def test_batch_uses_one_fetch_and_one_bulk_insert(client, batch):
    pytest.importorskip("magic")
    env = batch(upload_concurrency=4)
    media_repo: RecordingMediaRepo = env["media_repo"]  # type: ignore[assignment]
    sessions_repo: CountingSessionsRepo = env["sessions_repo"]  # type: ignore[assignment]
    user_id = uuid4()

    async with client as c:
        session_id = await _create_session(c, user_id)
        before = sessions_repo.get_calls
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=_files([(f"wave{i}.jpg", _photo(b"__%d__" % i)) for i in range(5)]),
        )

    assert r.status_code == 201
    body = r.json()
    assert len(body) == 5
    assert [m["fileName"] for m in body] == [f"wave{i}.jpg" for i in range(5)]
    # One session fetch for the whole batch, one bulk insert, no per-file create.
    assert sessions_repo.get_calls - before == 1
    assert media_repo.create_many_calls == 1
    assert media_repo.create_calls == 0
    assert media_repo.last_items is not None and len(media_repo.last_items) == 5


# ---------------------------------------------------------------------------
# Bounded concurrency
# ---------------------------------------------------------------------------


async def test_store_concurrency_is_bounded(client, batch):
    pytest.importorskip("magic")
    storage = InstrumentedStorage(default_delay=0.05)
    batch(upload_concurrency=2, storage=storage)
    user_id = uuid4()

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=_files([(f"wave{i}.jpg", _photo(b"__%d__" % i)) for i in range(4)]),
        )

    assert r.status_code == 201
    # UPLOAD_CONCURRENCY=2 → never more than 2 stores in flight, and real overlap.
    assert storage.max_in_flight == 2


async def test_concurrency_one_is_sequential(client, batch):
    pytest.importorskip("magic")
    storage = InstrumentedStorage(default_delay=0.02)
    env = batch(upload_concurrency=1, storage=storage)
    media_repo: RecordingMediaRepo = env["media_repo"]  # type: ignore[assignment]
    sessions_repo: CountingSessionsRepo = env["sessions_repo"]  # type: ignore[assignment]
    user_id = uuid4()

    async with client as c:
        session_id = await _create_session(c, user_id)
        before = sessions_repo.get_calls
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=_files([(f"wave{i}.jpg", _photo(b"__%d__" % i)) for i in range(4)]),
        )

    assert r.status_code == 201
    # Rollback lever: one store at a time, but still one fetch + one bulk insert.
    assert storage.max_in_flight == 1
    assert media_repo.create_many_calls == 1
    assert sessions_repo.get_calls - before == 1


# ---------------------------------------------------------------------------
# Order preservation
# ---------------------------------------------------------------------------


async def test_succeeded_order_matches_input_despite_staggered_latency(client, batch):
    pytest.importorskip("magic")
    # Earlier files store slowest, so completion order is reversed from input.
    storage = InstrumentedStorage(
        content_delays=[
            (b"__D0__", 0.20),
            (b"__D1__", 0.14),
            (b"__D2__", 0.08),
            (b"__D3__", 0.0),
        ]
    )
    batch(upload_concurrency=4, storage=storage)
    user_id = uuid4()

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=_files([(f"wave{i}.jpg", _photo(b"__D%d__" % i)) for i in range(4)]),
        )

    assert r.status_code == 201
    assert [m["fileName"] for m in r.json()] == [f"wave{i}.jpg" for i in range(4)]


# ---------------------------------------------------------------------------
# Storage-stage partial success (207)
# ---------------------------------------------------------------------------


async def test_partial_storage_failure_returns_207(client, batch):
    pytest.importorskip("magic")
    storage = InstrumentedStorage(fail_on=b"__FAIL__")
    env = batch(upload_concurrency=4, storage=storage)
    media_repo: RecordingMediaRepo = env["media_repo"]  # type: ignore[assignment]
    user_id = uuid4()

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=_files(
                [
                    ("wave0.jpg", _photo(b"__M0__")),
                    ("wave1.jpg", _photo(b"__M1__")),
                    ("wave2.jpg", _photo(b"__FAIL__")),
                    ("wave3.jpg", _photo(b"__M3__")),
                ]
            ),
        )

    assert r.status_code == 207
    body = r.json()
    assert [m["fileName"] for m in body["succeeded"]] == ["wave0.jpg", "wave1.jpg", "wave3.jpg"]
    assert len(body["failed"]) == 1
    assert body["failed"][0]["fileName"] == "wave2.jpg"
    assert body["failed"][0]["code"] == "STORAGE_UPLOAD_FAILED"
    # Only the 3 stored files are persisted; the half-write is cleaned up.
    assert media_repo.create_many_calls == 1
    assert media_repo.last_items is not None and len(media_repo.last_items) == 3
    assert len(storage.deleted) == 1


async def test_all_files_fail_storage_returns_502(client, batch):
    pytest.importorskip("magic")
    storage = InstrumentedStorage(fail_on=b"__FAIL__")
    env = batch(upload_concurrency=4, storage=storage)
    media_repo: RecordingMediaRepo = env["media_repo"]  # type: ignore[assignment]
    user_id = uuid4()

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=_files([(f"wave{i}.jpg", _photo(b"__FAIL__")) for i in range(3)]),
        )

    assert r.status_code == 502
    assert r.json()["error"]["code"] == "STORAGE_UPLOAD_FAILED"
    assert media_repo.create_many_calls == 0


async def test_unexpected_store_error_is_not_swallowed(batch):
    pytest.importorskip("magic")
    storage = InstrumentedStorage(fail_on=b"__BOOM__", fail_exc=RuntimeError("boom"))
    env = batch(upload_concurrency=4, storage=storage)
    media_repo: RecordingMediaRepo = env["media_repo"]  # type: ignore[assignment]
    user_id = uuid4()

    # The 500 response is generated by the exception handler, but Starlette also
    # re-raises server errors; tell the ASGI transport to return, not re-raise.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=_files(
                [
                    ("wave0.jpg", _photo(b"__M0__")),
                    ("wave1.jpg", _photo(b"__BOOM__")),
                    ("wave2.jpg", _photo(b"__M2__")),
                ]
            ),
        )

    # A non-storage failure surfaces as a whole-request error, never a 207.
    assert r.status_code == 500
    assert media_repo.create_many_calls == 0


# ---------------------------------------------------------------------------
# Validation stays whole-request (no partial save)
# ---------------------------------------------------------------------------


async def test_invalid_type_rejects_whole_request(client, batch):
    pytest.importorskip("magic")
    storage = InstrumentedStorage()
    env = batch(upload_concurrency=4, storage=storage)
    media_repo: RecordingMediaRepo = env["media_repo"]  # type: ignore[assignment]
    user_id = uuid4()

    files = _files([(f"wave{i}.jpg", _photo(b"__%d__" % i)) for i in range(3)])
    files.append(("file", ("notes.txt", b"hello world", "text/plain")))

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=files,
        )

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "INVALID_MEDIA_TYPE"
    # Fail-fast in Phase 0: no object stored, no row inserted.
    assert storage.uploaded == {}
    assert media_repo.create_many_calls == 0


async def test_too_many_photos_rejects_before_any_store(client, batch):
    pytest.importorskip("magic")
    storage = InstrumentedStorage()
    env = batch(upload_concurrency=4, storage=storage)
    media_repo: RecordingMediaRepo = env["media_repo"]  # type: ignore[assignment]
    user_id = uuid4()

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=_files([(f"wave{i}.jpg", _photo(b"__%d__" % i)) for i in range(11)]),
        )

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "TOO_MANY_PHOTOS"
    assert storage.uploaded == {}
    assert media_repo.create_many_calls == 0


# ---------------------------------------------------------------------------
# Single video unchanged + video-opt invariants
# ---------------------------------------------------------------------------


async def test_single_video_returns_201(client, batch, fake_video_magic):
    batch(upload_concurrency=4)
    user_id = uuid4()
    video = b"FAKEVIDEO" + b"\x00" * 128

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=[("file", ("clip.mp4", video, "video/mp4"))],
        )

    assert r.status_code == 201
    body = r.json()
    assert len(body) == 1
    assert body[0]["mediaType"] == "video"
    assert body[0]["durationSeconds"] == 5.0


async def test_created_video_row_preserves_invariants(client, batch, fake_video_magic):
    env = batch(upload_concurrency=4)
    storage: InstrumentedStorage = env["storage"]  # type: ignore[assignment]
    media_repo: RecordingMediaRepo = env["media_repo"]  # type: ignore[assignment]
    user_id = uuid4()
    video = b"FAKEVIDEO" + b"\x00" * 128

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=[("file", ("clip.mp4", video, "video/mp4"))],
        )

    assert r.status_code == 201
    created = list(media_repo._store.values())
    assert len(created) == 1
    media = created[0]
    # optimized_at stays NULL so the sweeper still picks the video up (§4.6).
    assert media.optimized_at is None
    assert media.media_type == "video"
    # Storage key format {user_id}/{session_id}/{media_id}.{ext} is preserved.
    keys = list(storage.uploaded.keys())
    assert len(keys) == 1
    assert keys[0].startswith(f"{user_id}/{session_id}/")
    assert keys[0].endswith(".mp4")
