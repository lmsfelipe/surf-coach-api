"""Tests for request-path bounds on POST /sessions/{id}/media/.

Covers the two defects in PLAN_BACKEND_Request_Path_Blocking.md: blocking I/O on
the event loop (§1.1) and unbounded request buffering (§1.2).
"""

import asyncio
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
from app.core.errors import FileTooLargeError
from app.core.upload import CHUNK_SIZE, spool_upload
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

MB = 1024 * 1024


@pytest.fixture
def limits(monkeypatch):
    """Shrink the caps so the tests can cross them without moving real megabytes."""

    def _apply(*, max_size_mb: int = 1, max_files: int = 2, upload_delay_sec: float = 0.0):
        monkeypatch.setenv("MAX_UPLOAD_SIZE_MB", str(max_size_mb))
        monkeypatch.setenv("MAX_UPLOAD_FILES", str(max_files))
        get_settings.cache_clear()

        storage = FakeStorageClient(upload_delay_sec=upload_delay_sec)
        sessions_repo = FakeSessionsRepo()
        media_repo = FakeMediaRepo()
        frames = FakeFrameExtractor(duration=5.0)

        app.dependency_overrides[sessions_api.get_sessions_service] = lambda: SessionsService(
            sessions_repo  # type: ignore[arg-type]
        )
        app.dependency_overrides[media_api.get_media_service] = lambda: MediaService(
            media_repo=media_repo,  # type: ignore[arg-type]
            sessions_repo=sessions_repo,  # type: ignore[arg-type]
            storage=storage,  # type: ignore[arg-type]
            frame_extractor=frames,  # type: ignore[arg-type]
        )
        return storage

    yield _apply

    app.dependency_overrides.pop(sessions_api.get_sessions_service, None)
    app.dependency_overrides.pop(media_api.get_media_service, None)
    get_settings.cache_clear()


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


# ---------------------------------------------------------------------------
# Per-file size cap
# ---------------------------------------------------------------------------


class _CountingUploadFile:
    """Stands in for Starlette's UploadFile, recording how much was handed over."""

    def __init__(self, data: bytes, filename: str = "big.bin") -> None:
        self._data = data
        self._pos = 0
        self.filename = filename
        self.bytes_read = 0

    async def read(self, size: int = -1) -> bytes:
        chunk = self._data[self._pos : self._pos + size]
        self._pos += len(chunk)
        self.bytes_read += len(chunk)
        return chunk


async def test_spool_upload_aborts_without_reading_whole_part():
    """The cap is enforced mid-stream, so the oversized tail is never consumed."""
    part = _CountingUploadFile(b"x" * (5 * MB))

    with pytest.raises(FileTooLargeError):
        await spool_upload(part, max_bytes=1 * MB)  # type: ignore[arg-type]

    # Reading stops one chunk past the cap, nowhere near the full 5 MB.
    assert part.bytes_read <= 1 * MB + CHUNK_SIZE
    assert part.bytes_read < len(part._data)


async def test_spool_upload_removes_temp_file_on_rejection(tmp_path, monkeypatch):
    spools: list[str] = []
    import app.core.upload as upload_mod

    real_mkstemp = upload_mod.tempfile.mkstemp

    def _tracking_mkstemp(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        spools.append(path)
        return fd, path

    monkeypatch.setattr(upload_mod.tempfile, "mkstemp", _tracking_mkstemp)

    part = _CountingUploadFile(b"x" * (3 * MB))
    with pytest.raises(FileTooLargeError):
        await spool_upload(part, max_bytes=1 * MB)  # type: ignore[arg-type]

    assert spools and not Path(spools[0]).exists()


async def test_upload_over_size_cap_returns_413(client, limits):
    pytest.importorskip("magic")
    limits(max_size_mb=1, max_files=5)
    user_id = uuid4()
    oversized = b"x" * (2 * MB)

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=[("file", ("big.jpg", oversized, "image/jpeg"))],
        )

    assert r.status_code == 413
    assert r.json()["error"]["code"] == "FILE_TOO_LARGE"
    assert r.json()["error"]["details"]["max_size_mb"] == 1


# ---------------------------------------------------------------------------
# Part count cap
# ---------------------------------------------------------------------------


async def test_upload_over_file_cap_returns_422(client, limits):
    pytest.importorskip("magic")
    limits(max_files=2)
    user_id = uuid4()
    image_bytes = JPEG_PATH.read_bytes()

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=[("file", (f"surf{i}.jpg", image_bytes, "image/jpeg")) for i in range(3)],
        )

    body = r.json()["error"]
    assert r.status_code == 422
    assert body["code"] == "TOO_MANY_FILES"
    assert body["details"] == {"max_files": 2, "uploaded": 3}


async def test_unrecognised_types_count_toward_file_cap(client, limits):
    """Parts that are neither image nor video used to escape every count check."""
    pytest.importorskip("magic")
    limits(max_files=2)
    user_id = uuid4()

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=[("file", (f"notes{i}.txt", b"hello world", "text/plain")) for i in range(3)],
        )

    assert r.status_code == 422
    assert r.json()["error"]["code"] == "TOO_MANY_FILES"


# ---------------------------------------------------------------------------
# Whole-body cap (BodySizeLimitMiddleware)
# ---------------------------------------------------------------------------


async def test_oversized_body_rejected_on_content_length(client, limits):
    limits(max_size_mb=1, max_files=2)
    user_id = uuid4()
    # Cap is MAX_UPLOAD_FILES * MAX_UPLOAD_SIZE_MB + 1 MiB of framing slack = 3 MiB.
    body = b"x" * (4 * MB)

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=[("file", ("big.jpg", body, "image/jpeg"))],
        )

    assert r.status_code == 413
    assert r.json()["error"]["code"] == "REQUEST_TOO_LARGE"


async def test_oversized_chunked_body_rejected_without_content_length(client, limits):
    """No Content-Length to trust, so the cap is enforced on the stream itself."""
    limits(max_size_mb=1, max_files=2)
    user_id = uuid4()

    async def _chunks():
        for _ in range(8):
            yield b"x" * MB

    async with client as c:
        r = await c.post(
            "/api/v1/sessions/",
            headers={
                "Authorization": f"Bearer {_token(user_id)}",
                "Content-Type": "application/json",
            },
            content=_chunks(),
        )

    assert r.status_code == 413
    assert r.json()["error"]["code"] == "REQUEST_TOO_LARGE"


async def test_within_limit_body_still_accepted(client, limits):
    pytest.importorskip("magic")
    limits(max_size_mb=1, max_files=5)
    user_id = uuid4()
    image_bytes = JPEG_PATH.read_bytes()

    async with client as c:
        session_id = await _create_session(c, user_id)
        r = await c.post(
            f"/api/v1/sessions/{session_id}/media/",
            headers={"Authorization": f"Bearer {_token(user_id)}"},
            files=[("file", (f"surf{i}.jpg", image_bytes, "image/jpeg")) for i in range(3)],
        )

    assert r.status_code == 201
    assert len(r.json()) == 3


# ---------------------------------------------------------------------------
# §1.1 regression: the event loop stays free during an upload
# ---------------------------------------------------------------------------


async def test_health_stays_responsive_during_slow_upload(client, limits):
    """A slow upload must not stall the single event loop that serves everything.

    The fake storage client sleeps synchronously, exactly as the real blocking
    Supabase call does. Before the service moved that call onto a thread, the
    concurrent health check could not be served until the upload finished.
    """
    pytest.importorskip("magic")
    upload_delay = 0.3
    parts = 3
    limits(max_size_mb=10, max_files=5, upload_delay_sec=upload_delay)
    user_id = uuid4()
    image_bytes = JPEG_PATH.read_bytes()

    async with client as c:
        session_id = await _create_session(c, user_id)

        async def _upload():
            return await c.post(
                f"/api/v1/sessions/{session_id}/media/",
                headers={"Authorization": f"Bearer {_token(user_id)}"},
                files=[("file", (f"surf{i}.jpg", image_bytes, "image/jpeg")) for i in range(parts)],
            )

        # Measured from before the upload starts, not from when the health
        # coroutine happens to be scheduled — a blocked loop delays the
        # scheduling itself, which a clock started inside _health cannot see.
        started_at = time.perf_counter()

        async def _health():
            await asyncio.sleep(0.05)  # let the upload reach its first storage call
            resp = await c.get("/health/live")
            return resp, time.perf_counter() - started_at

        upload_resp, (health_resp, health_elapsed) = await asyncio.gather(_upload(), _health())

    assert upload_resp.status_code == 201
    assert health_resp.status_code == 200
    # Serving the upload takes parts * upload_delay (0.9 s). If any of that ran
    # on the event loop, the health check could not land before it finished.
    assert health_elapsed < upload_delay, (
        f"health check answered after {health_elapsed:.3f}s; "
        f"the upload occupied the loop for up to {parts * upload_delay:.1f}s"
    )
