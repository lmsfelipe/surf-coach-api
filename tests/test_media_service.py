"""MediaService rules that the HTTP tests exercise only partially: batch count
limits, per-file validation, the moderation switch, and ownership on read/delete.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from app.core.errors import (
    ExplicitContentError,
    FileTooLargeError,
    ForbiddenError,
    InvalidMediaTypeError,
    MediaNotSurfRelatedError,
    NotFoundError,
    TooFewPhotosError,
    TooManyFilesError,
    TooManyPhotosError,
    TooManyVideosError,
    VideoTooLongError,
)
from app.core.security.jwt import AuthUser
from app.core.upload import SpooledUpload
from app.services.media import MAX_PHOTOS, MAX_VIDEOS, MIN_PHOTOS, MediaService
from tests.fake_deps import (
    FakeFrameExtractor,
    FakeGeminiService,
    FakeMediaRepo,
    FakeSessionsRepo,
    FakeStorageClient,
    make_moderation_output,
)

magic = pytest.importorskip("magic")

JPEG_HEADER = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
PNG_HEADER = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + b"\x00" * 20
# libmagic does not recognise synthetic MP4 headers on every platform, so the
# video path uses the same sentinel prefix as tests/test_media_batch_upload.py.
MP4_HEADER = b"FAKEVIDEO" + b"\x00" * 128


@pytest.fixture(autouse=True)
def fake_video_magic(monkeypatch):
    """Sniff the sentinel prefix as video/mp4; everything else sniffs normally."""
    import app.services.media as media_mod

    real = media_mod.magic.from_buffer

    def _fake(head, mime=True):
        if head.startswith(b"FAKEVIDEO"):
            return "video/mp4"
        return real(head, mime=mime)

    monkeypatch.setattr(media_mod.magic, "from_buffer", _fake)


@pytest.fixture
def ctx():
    return {
        "media": FakeMediaRepo(),
        "sessions": FakeSessionsRepo(),
        "storage": FakeStorageClient(),
        "frames": FakeFrameExtractor(duration=10.0),
        "gemini": FakeGeminiService(None),
    }


def build_service(ctx) -> MediaService:
    return MediaService(
        media_repo=ctx["media"],  # type: ignore[arg-type]
        sessions_repo=ctx["sessions"],  # type: ignore[arg-type]
        storage=ctx["storage"],  # type: ignore[arg-type]
        frame_extractor=ctx["frames"],  # type: ignore[arg-type]
        gemini=ctx["gemini"],
    )


def spool(tmp_path, content: bytes, name: str = "file.jpg") -> SpooledUpload:
    path = tmp_path / name
    path.write_bytes(content)
    return SpooledUpload(path=str(path), size=len(content), file_name=name)


async def seed_session(ctx, user_id):
    return await ctx["sessions"].create(
        profile_id=user_id,
        session_date=date(2026, 4, 17),
        location="Maresias",
        wave_size=1.5,
    )


# ---------------------------------------------------------------------------
# Batch count limits
# ---------------------------------------------------------------------------


def test_file_count_within_the_cap_is_accepted(ctx):
    build_service(ctx).validate_file_count(5)  # must not raise


def test_file_count_over_the_cap_is_rejected(ctx):
    service = build_service(ctx)
    over = service.settings.MAX_UPLOAD_FILES + 1
    with pytest.raises(TooManyFilesError):
        service.validate_file_count(over)


def test_exactly_the_cap_is_still_accepted(ctx):
    service = build_service(ctx)
    service.validate_file_count(service.settings.MAX_UPLOAD_FILES)


def test_a_lone_photo_is_rejected_as_too_few(ctx):
    """Photo reviews need a minimum set; one frame is not a session."""
    with pytest.raises(TooFewPhotosError):
        build_service(ctx).validate_upload_counts([JPEG_HEADER])


def test_just_under_the_photo_minimum_is_rejected(ctx):
    with pytest.raises(TooFewPhotosError):
        build_service(ctx).validate_upload_counts([JPEG_HEADER] * (MIN_PHOTOS - 1))


def test_exactly_the_photo_minimum_is_accepted(ctx):
    build_service(ctx).validate_upload_counts([JPEG_HEADER] * MIN_PHOTOS)


def test_too_many_photos_is_rejected(ctx):
    with pytest.raises(TooManyPhotosError):
        build_service(ctx).validate_upload_counts([JPEG_HEADER] * (MAX_PHOTOS + 1))


def test_exactly_the_photo_maximum_is_accepted(ctx):
    build_service(ctx).validate_upload_counts([JPEG_HEADER] * MAX_PHOTOS)


def test_too_many_videos_is_rejected(ctx):
    with pytest.raises(TooManyVideosError):
        build_service(ctx).validate_upload_counts([MP4_HEADER] * (MAX_VIDEOS + 1))


def test_a_single_video_needs_no_photo_minimum(ctx):
    """The photo floor applies only when photos are present (0 < count < MIN)."""
    build_service(ctx).validate_upload_counts([MP4_HEADER])


def test_mixed_formats_all_count_as_photos(ctx):
    build_service(ctx).validate_upload_counts([JPEG_HEADER, PNG_HEADER, JPEG_HEADER])


def test_unrecognised_types_count_toward_neither_limit(ctx):
    """They are rejected later, per file — not by the photo/video counters."""
    build_service(ctx).validate_upload_counts([b"%PDF-1.4 not media at all"])


# ---------------------------------------------------------------------------
# Per-file validation
# ---------------------------------------------------------------------------


async def test_oversized_file_is_rejected(ctx, tmp_path):
    user_id = uuid4()
    session = await seed_session(ctx, user_id)
    service = build_service(ctx)
    upload = spool(tmp_path, JPEG_HEADER)
    upload.size = (service.settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024) + 1

    with pytest.raises(FileTooLargeError):
        await service._validate(session.id, upload, AuthUser(id=user_id, email="a@example.com"))


async def test_unsupported_type_is_rejected_with_the_detected_mime(ctx, tmp_path):
    user_id = uuid4()
    session = await seed_session(ctx, user_id)
    upload = spool(tmp_path, b"%PDF-1.4\n%not media", name="doc.pdf")

    with pytest.raises(InvalidMediaTypeError) as exc:
        await build_service(ctx)._validate(
            session.id, upload, AuthUser(id=user_id, email="a@example.com")
        )
    assert "detected" in (exc.value.details or {})


async def test_a_video_over_the_duration_cap_is_rejected(ctx, tmp_path):
    user_id = uuid4()
    session = await seed_session(ctx, user_id)
    service = build_service(ctx)
    ctx["frames"]._duration = service.settings.MAX_VIDEO_DURATION_SEC + 1

    with pytest.raises(VideoTooLongError):
        await service._validate(
            session.id,
            spool(tmp_path, MP4_HEADER, "clip.mp4"),
            AuthUser(id=user_id, email="a@example.com"),
        )


async def test_a_video_at_the_duration_cap_is_accepted(ctx, tmp_path):
    user_id = uuid4()
    session = await seed_session(ctx, user_id)
    service = build_service(ctx)
    ctx["frames"]._duration = float(service.settings.MAX_VIDEO_DURATION_SEC)

    validated = await service._validate(
        session.id,
        spool(tmp_path, MP4_HEADER, "clip.mp4"),
        AuthUser(id=user_id, email="a@example.com"),
    )
    assert validated.media_type == "video"
    assert validated.duration_seconds == Decimal(f"{service.settings.MAX_VIDEO_DURATION_SEC:.2f}")


async def test_the_storage_key_is_scoped_to_the_user_and_session(ctx, tmp_path):
    """Keys carry the ownership prefix the stream endpoint later checks against."""
    user_id = uuid4()
    session = await seed_session(ctx, user_id)

    validated = await build_service(ctx)._validate(
        session.id, spool(tmp_path, JPEG_HEADER), AuthUser(id=user_id, email="a@example.com")
    )

    assert validated.storage_key.startswith(f"{user_id}/{session.id}/")
    assert validated.storage_key.endswith(".jpg")


async def test_images_carry_no_duration(ctx, tmp_path):
    user_id = uuid4()
    session = await seed_session(ctx, user_id)

    validated = await build_service(ctx)._validate(
        session.id, spool(tmp_path, JPEG_HEADER), AuthUser(id=user_id, email="a@example.com")
    )

    assert validated.media_type == "image"
    assert validated.duration_seconds is None


# ---------------------------------------------------------------------------
# Moderation switch
# ---------------------------------------------------------------------------


@pytest.fixture
def moderation_on(monkeypatch):
    from app.core.config import get_settings

    monkeypatch.setattr(get_settings(), "CONTENT_MODERATION_ENABLED", True, raising=False)


async def test_moderation_is_skipped_when_disabled(ctx, tmp_path):
    await build_service(ctx)._moderate(spool(tmp_path, JPEG_HEADER), "image", "image/jpeg")
    assert ctx["gemini"].moderation_calls == []


async def test_moderation_is_skipped_when_no_gemini_client_is_wired(ctx, tmp_path, moderation_on):
    ctx["gemini"] = None
    await build_service(ctx)._moderate(spool(tmp_path, JPEG_HEADER), "image", "image/jpeg")


async def test_clean_content_passes_moderation(ctx, tmp_path, moderation_on):
    ctx["gemini"]._moderation_output = make_moderation_output()
    await build_service(ctx)._moderate(spool(tmp_path, JPEG_HEADER), "image", "image/jpeg")
    assert ctx["gemini"].moderation_calls == [1]


async def test_explicit_content_is_rejected(ctx, tmp_path, moderation_on):
    ctx["gemini"]._moderation_output = make_moderation_output(
        explicit_content=True, reason="Adult content."
    )
    with pytest.raises(ExplicitContentError) as exc:
        await build_service(ctx)._moderate(spool(tmp_path, JPEG_HEADER), "image", "image/jpeg")
    assert exc.value.details["reason"] == "Adult content."


async def test_non_surf_content_is_rejected(ctx, tmp_path, moderation_on):
    ctx["gemini"]._moderation_output = make_moderation_output(surf_related=False, reason="A cat.")
    with pytest.raises(MediaNotSurfRelatedError):
        await build_service(ctx)._moderate(spool(tmp_path, JPEG_HEADER), "image", "image/jpeg")


async def test_explicit_content_wins_over_the_surf_check(ctx, tmp_path, moderation_on):
    """Both flags set: the stronger rejection must be the one reported."""
    ctx["gemini"]._moderation_output = make_moderation_output(
        surf_related=False, explicit_content=True, reason="Explicit."
    )
    with pytest.raises(ExplicitContentError):
        await build_service(ctx)._moderate(spool(tmp_path, JPEG_HEADER), "image", "image/jpeg")


async def test_videos_are_moderated_from_sampled_frames(ctx, tmp_path, moderation_on):
    ctx["frames"] = FakeFrameExtractor(frames=[b"f1", b"f2", b"f3"])
    ctx["gemini"]._moderation_output = make_moderation_output()

    await build_service(ctx)._moderate(spool(tmp_path, MP4_HEADER, "c.mp4"), "video", "video/mp4")

    assert ctx["gemini"].moderation_calls == [3]


# ---------------------------------------------------------------------------
# Read / delete ownership
# ---------------------------------------------------------------------------


async def _seed_media(ctx, user_id):
    session = await seed_session(ctx, user_id)
    media_id = uuid4()
    key = f"{user_id}/{session.id}/{media_id}.jpg"
    ctx["storage"].uploaded[key] = b"bytes"
    media = await ctx["media"].create(
        session_id=session.id,
        media_type="image",
        storage_url=f"https://storage.test/surf-media/{key}",
        file_name="photo.jpg",
    )
    return session, media, key


async def test_list_media_rejects_another_users_session(ctx):
    owner, intruder = uuid4(), uuid4()
    session = await seed_session(ctx, owner)
    with pytest.raises(ForbiddenError):
        await build_service(ctx).list_media(
            session.id, AuthUser(id=intruder, email="x@example.com")
        )


async def test_list_media_404s_for_an_unknown_session(ctx):
    with pytest.raises(NotFoundError):
        await build_service(ctx).list_media(uuid4(), AuthUser(id=uuid4(), email="a@example.com"))


async def test_get_media_rejects_another_user(ctx):
    owner, intruder = uuid4(), uuid4()
    _, media, _ = await _seed_media(ctx, owner)
    with pytest.raises(ForbiddenError):
        await build_service(ctx).get_media(media.id, AuthUser(id=intruder, email="x@example.com"))


async def test_get_media_404s_when_missing(ctx):
    with pytest.raises(NotFoundError):
        await build_service(ctx).get_media(uuid4(), AuthUser(id=uuid4(), email="a@example.com"))


async def test_token_scoped_lookup_rejects_a_foreign_profile(ctx):
    """The media-token path must apply the same ownership rule as the JWT path."""
    owner, intruder = uuid4(), uuid4()
    _, media, _ = await _seed_media(ctx, owner)
    with pytest.raises(ForbiddenError):
        await build_service(ctx).get_media_for_profile(media.id, intruder)


async def test_token_scoped_lookup_returns_the_owners_media(ctx):
    owner = uuid4()
    _, media, _ = await _seed_media(ctx, owner)
    assert (await build_service(ctx).get_media_for_profile(media.id, owner)).id == media.id


async def test_delete_removes_both_the_row_and_the_object(ctx):
    owner = uuid4()
    _, media, key = await _seed_media(ctx, owner)

    await build_service(ctx).delete_media(media.id, AuthUser(id=owner, email="a@example.com"))

    assert ctx["storage"].deleted == [key]
    assert await ctx["media"].get(media.id) is None


async def test_delete_rejects_another_user_and_keeps_the_object(ctx):
    owner, intruder = uuid4(), uuid4()
    _, media, _ = await _seed_media(ctx, owner)

    with pytest.raises(ForbiddenError):
        await build_service(ctx).delete_media(
            media.id, AuthUser(id=intruder, email="x@example.com")
        )

    assert ctx["storage"].deleted == []
    assert await ctx["media"].get(media.id) is not None


async def test_delete_still_drops_the_row_when_the_url_yields_no_key(ctx):
    """A legacy/unparsable URL must not block the row from being removed."""
    owner = uuid4()
    session = await seed_session(ctx, owner)
    media = await ctx["media"].create(
        session_id=session.id,
        media_type="image",
        storage_url="https://cdn.example.com/legacy/path.jpg",
        file_name="photo.jpg",
    )

    await build_service(ctx).delete_media(media.id, AuthUser(id=owner, email="a@example.com"))

    assert ctx["storage"].deleted == []
    assert await ctx["media"].get(media.id) is None


def test_storage_key_extraction_drops_the_query_string():
    user_id, session_id, media_id = uuid4(), uuid4(), uuid4()
    url = f"https://x/{user_id}/{session_id}/{media_id}.jpg?token=abc"
    assert MediaService._extract_storage_key(url, user_id, session_id, media_id) == (
        f"{user_id}/{session_id}/{media_id}.jpg"
    )


def test_storage_key_extraction_returns_none_without_the_prefix():
    assert (
        MediaService._extract_storage_key("https://x/unrelated.jpg", uuid4(), uuid4(), uuid4())
        is None
    )
