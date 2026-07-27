"""Tests for video optimization (transcode + sweep)."""

from __future__ import annotations

import datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.core.config import get_settings
from app.core.errors import InvalidMediaError
from app.worker import _key_from_storage_url, optimize_media_task, sweep_unoptimized_media
from tests.fake_deps import FakeMediaRepo, FakeStorageClient, FakeVideoTranscoder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_video_media(repo: FakeMediaRepo, *, session_id=None, optimized_at=None, raw=b"raw-video"):
    """Insert a fake video media row and return it + the storage key."""
    import asyncio

    session_id = session_id or uuid4()
    media = asyncio.get_event_loop().run_until_complete(
        repo.create(
            session_id=session_id,
            media_type="video",
            storage_url=f"https://example.supabase.co/storage/v1/object/public/surf-media/user1/{session_id}/{uuid4()}.mov",
            file_name="clip.mov",
            file_size_bytes=len(raw),
            duration_seconds=None,
        )
    )
    if optimized_at:
        media.optimized_at = optimized_at
    return media


async def _make_video_media_async(repo, *, session_id=None, optimized_at=None, raw=b"raw-video"):
    session_id = session_id or uuid4()
    media = await repo.create(
        session_id=session_id,
        media_type="video",
        storage_url=f"https://example.supabase.co/storage/v1/object/public/surf-media/user1/{session_id}/{uuid4()}.mov",
        file_name="clip.mov",
        file_size_bytes=len(raw),
        duration_seconds=None,
    )
    if optimized_at:
        media.optimized_at = optimized_at
    return media


# ---------------------------------------------------------------------------
# _key_from_storage_url
# ---------------------------------------------------------------------------


class TestKeyFromStorageUrl:
    def test_extracts_key(self):
        url = "https://x.supabase.co/storage/v1/object/public/surf-media/uid/sid/mid.mov"
        assert _key_from_storage_url(url, "surf-media") == "uid/sid/mid.mov"

    def test_strips_query_string(self):
        url = "https://x.supabase.co/storage/v1/object/public/surf-media/a/b/c.mp4?token=xyz"
        assert _key_from_storage_url(url, "surf-media") == "a/b/c.mp4"

    def test_returns_none_for_unknown_bucket(self):
        url = "https://x.supabase.co/storage/v1/object/public/other-bucket/a/b.mov"
        assert _key_from_storage_url(url, "surf-media") is None


# ---------------------------------------------------------------------------
# optimize_media_task
# ---------------------------------------------------------------------------


class TestOptimizeMediaTask:
    @pytest.fixture()
    def media_repo(self):
        return FakeMediaRepo()

    @pytest.fixture()
    def storage(self):
        return FakeStorageClient()

    @pytest.fixture()
    def transcoder(self):
        return FakeVideoTranscoder(output=b"small")

    async def test_transcodes_and_replaces(self, media_repo, storage, transcoder, monkeypatch):
        raw = b"x" * 1000
        media = await _make_video_media_async(media_repo, raw=raw)
        key = _key_from_storage_url(media.storage_url, "surf-media")
        storage.uploaded[key] = raw

        monkeypatch.setattr("app.worker.SessionLocal", lambda: AsyncMock())
        monkeypatch.setattr("app.worker.MediaRepository", lambda db: media_repo)
        monkeypatch.setattr("app.worker.get_storage_client", lambda: storage)
        monkeypatch.setattr("app.worker.VideoTranscoder", lambda: transcoder)

        await optimize_media_task({}, str(media.id))

        assert media.optimized_at is not None
        assert media.file_size_bytes == len(b"small")
        assert storage.uploaded[key] == b"small"
        assert len(transcoder.calls) == 1

    async def test_skips_already_optimized(self, media_repo, storage, transcoder, monkeypatch):
        media = await _make_video_media_async(
            media_repo, optimized_at=datetime.datetime.now(tz=datetime.UTC)
        )

        monkeypatch.setattr("app.worker.SessionLocal", lambda: AsyncMock())
        monkeypatch.setattr("app.worker.MediaRepository", lambda db: media_repo)
        monkeypatch.setattr("app.worker.get_storage_client", lambda: storage)
        monkeypatch.setattr("app.worker.VideoTranscoder", lambda: transcoder)

        await optimize_media_task({}, str(media.id))

        assert len(transcoder.calls) == 0

    async def test_skips_image(self, media_repo, storage, transcoder, monkeypatch):
        media = await media_repo.create(
            session_id=uuid4(),
            media_type="image",
            storage_url="https://example.supabase.co/storage/v1/object/public/surf-media/u/s/m.jpg",
            file_name="photo.jpg",
            file_size_bytes=5000,
            duration_seconds=None,
        )

        monkeypatch.setattr("app.worker.SessionLocal", lambda: AsyncMock())
        monkeypatch.setattr("app.worker.MediaRepository", lambda db: media_repo)
        monkeypatch.setattr("app.worker.get_storage_client", lambda: storage)
        monkeypatch.setattr("app.worker.VideoTranscoder", lambda: transcoder)

        await optimize_media_task({}, str(media.id))

        assert len(transcoder.calls) == 0

    async def test_skips_missing_media(self, media_repo, storage, transcoder, monkeypatch):
        monkeypatch.setattr("app.worker.SessionLocal", lambda: AsyncMock())
        monkeypatch.setattr("app.worker.MediaRepository", lambda db: media_repo)
        monkeypatch.setattr("app.worker.get_storage_client", lambda: storage)
        monkeypatch.setattr("app.worker.VideoTranscoder", lambda: transcoder)

        await optimize_media_task({}, str(uuid4()))  # no-op, no exception

    async def test_no_gain_keeps_raw(self, media_repo, storage, monkeypatch):
        """When compressed >= raw, keep the raw and still mark optimized."""
        raw = b"tiny"
        media = await _make_video_media_async(media_repo, raw=raw)
        key = _key_from_storage_url(media.storage_url, "surf-media")
        storage.uploaded[key] = raw

        big_transcoder = FakeVideoTranscoder(output=b"x" * (len(raw) + 10))
        monkeypatch.setattr("app.worker.SessionLocal", lambda: AsyncMock())
        monkeypatch.setattr("app.worker.MediaRepository", lambda db: media_repo)
        monkeypatch.setattr("app.worker.get_storage_client", lambda: storage)
        monkeypatch.setattr("app.worker.VideoTranscoder", lambda: big_transcoder)

        await optimize_media_task({}, str(media.id))

        assert media.optimized_at is not None
        assert media.file_size_bytes == len(raw)
        assert storage.uploaded[key] == raw  # raw preserved

    async def test_transcode_failure_leaves_raw_intact(self, media_repo, storage, monkeypatch):
        raw = b"original-raw-bytes"
        media = await _make_video_media_async(media_repo, raw=raw)
        key = _key_from_storage_url(media.storage_url, "surf-media")
        storage.uploaded[key] = raw

        failing = FakeVideoTranscoder(raise_exc=InvalidMediaError("ffmpeg boom"))
        monkeypatch.setattr("app.worker.SessionLocal", lambda: AsyncMock())
        monkeypatch.setattr("app.worker.MediaRepository", lambda db: media_repo)
        monkeypatch.setattr("app.worker.get_storage_client", lambda: storage)
        monkeypatch.setattr("app.worker.VideoTranscoder", lambda: failing)

        await optimize_media_task({}, str(media.id))  # does not raise

        assert media.optimized_at is None
        assert storage.uploaded[key] == raw  # untouched

    async def test_disabled_flag_skips(self, media_repo, storage, transcoder, monkeypatch):
        media = await _make_video_media_async(media_repo)

        monkeypatch.setattr("app.worker.SessionLocal", lambda: AsyncMock())
        monkeypatch.setattr("app.worker.MediaRepository", lambda db: media_repo)
        monkeypatch.setattr("app.worker.get_storage_client", lambda: storage)
        monkeypatch.setattr("app.worker.VideoTranscoder", lambda: transcoder)

        settings = get_settings()
        original = settings.VIDEO_OPTIMIZE_ENABLED
        try:
            settings.VIDEO_OPTIMIZE_ENABLED = False
            await optimize_media_task({}, str(media.id))
        finally:
            settings.VIDEO_OPTIMIZE_ENABLED = original

        assert len(transcoder.calls) == 0


# ---------------------------------------------------------------------------
# sweep_unoptimized_media
# ---------------------------------------------------------------------------


class TestSweepUnoptimizedMedia:
    async def test_enqueues_unoptimized_videos(self, monkeypatch):
        media_repo = FakeMediaRepo()
        await _make_video_media_async(media_repo)
        await _make_video_media_async(media_repo)
        # one image — should not appear
        await media_repo.create(
            session_id=uuid4(),
            media_type="image",
            storage_url="https://x/surf-media/u/s/m.jpg",
            file_name="photo.jpg",
            file_size_bytes=100,
            duration_seconds=None,
        )

        monkeypatch.setattr("app.worker.SessionLocal", lambda: AsyncMock())
        monkeypatch.setattr("app.worker.MediaRepository", lambda db: media_repo)

        redis_mock = AsyncMock()
        ctx = {"redis": redis_mock}

        await sweep_unoptimized_media(ctx)

        assert redis_mock.enqueue_job.call_count == 2
        for call in redis_mock.enqueue_job.call_args_list:
            assert call[0][0] == "optimize_media_task"

    async def test_disabled_flag_skips(self, monkeypatch):
        monkeypatch.setattr("app.worker.SessionLocal", lambda: AsyncMock())

        redis_mock = AsyncMock()
        ctx = {"redis": redis_mock}

        settings = get_settings()
        original = settings.VIDEO_OPTIMIZE_ENABLED
        try:
            settings.VIDEO_OPTIMIZE_ENABLED = False
            await sweep_unoptimized_media(ctx)
        finally:
            settings.VIDEO_OPTIMIZE_ENABLED = original

        redis_mock.enqueue_job.assert_not_called()


# ---------------------------------------------------------------------------
# FakeMediaRepo — optimization helpers
# ---------------------------------------------------------------------------


class TestFakeMediaRepoOptimization:
    async def test_mark_optimized_sets_fields(self):
        repo = FakeMediaRepo()
        media = await _make_video_media_async(repo)
        assert media.optimized_at is None

        await repo.mark_optimized(media.id, 42)

        assert media.optimized_at is not None
        assert media.file_size_bytes == 42

    async def test_list_unoptimized_videos_filters(self):
        repo = FakeMediaRepo()
        v1 = await _make_video_media_async(repo)
        v2 = await _make_video_media_async(
            repo, optimized_at=datetime.datetime.now(tz=datetime.UTC)
        )
        await repo.create(
            session_id=uuid4(),
            media_type="image",
            storage_url="x",
            file_name="p.jpg",
            file_size_bytes=1,
            duration_seconds=None,
        )

        rows = await repo.list_unoptimized_videos(older_than_sec=0, limit=100)
        ids = [m.id for m in rows]
        assert v1.id in ids
        assert v2.id not in ids
