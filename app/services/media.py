import asyncio
from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID, uuid4

import magic
import structlog

from app.core.config import get_settings
from app.core.errors import (
    ExplicitContentError,
    FileTooLargeError,
    ForbiddenError,
    InvalidMediaTypeError,
    MediaNotSurfRelatedError,
    NotFoundError,
    StorageUploadFailedError,
    TooFewPhotosError,
    TooManyFilesError,
    TooManyPhotosError,
    TooManyVideosError,
    VideoTooLongError,
)
from app.core.frame_extractor import FrameExtractor
from app.core.security.jwt import AuthUser
from app.core.storage import StorageClient
from app.core.upload import SpooledUpload
from app.models.media import Media
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository
from app.schemas.media import FailedUpload

logger = structlog.get_logger(__name__)


IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
VIDEO_MIME_TYPES = {"video/mp4", "video/quicktime", "video/x-m4v"}
ACCEPTED_MIME_TYPES = sorted(IMAGE_MIME_TYPES | VIDEO_MIME_TYPES)

MIN_PHOTOS = 3
MAX_PHOTOS = 10
MAX_VIDEOS = 3

MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/x-m4v": "m4v",
}


@dataclass
class _ValidatedMedia:
    """One file that passed Phase 0 (validate + moderate). Carries the spooled
    handle plus the resolved facts needed to store it — but no DB access."""

    upload: SpooledUpload
    media_type: str  # "image" | "video"
    detected_mime: str
    storage_key: str
    duration_seconds: Decimal | None
    file_name: str
    file_size_bytes: int


@dataclass
class _PreparedMedia:
    """Inert metadata for one stored object — the output of Phase A. Holds no ORM
    state, so it is safe to produce inside a ``gather``-ed coroutine (§4.1)."""

    media_type: str  # "image" | "video"
    storage_url: str  # returned by storage.upload_file
    file_name: str
    file_size_bytes: int
    duration_seconds: Decimal | None


@dataclass
class BatchUploadResult:
    """Domain result of :meth:`MediaService.upload_batch` — ORM rows that stored
    cleanly plus per-file storage failures. The route converts ``succeeded`` into
    ``MediaOut`` and builds the Pydantic 207 body (see app/schemas/media.py)."""

    succeeded: list[Media]
    failed: list[FailedUpload]


class MediaService:
    def __init__(
        self,
        media_repo: MediaRepository,
        sessions_repo: SessionsRepository,
        storage: StorageClient,
        frame_extractor: FrameExtractor,
        gemini=None,
    ) -> None:
        self.media_repo = media_repo
        self.sessions_repo = sessions_repo
        self.storage = storage
        self.frame_extractor = frame_extractor
        self.gemini = gemini
        self.settings = get_settings()

    def validate_file_count(self, count: int) -> None:
        """Cap the number of parts in one request, whatever their type.

        The photo/video limits below only constrain parts we recognise, so
        without this a batch of arbitrary-type parts is bounded by nothing.
        """
        max_files = self.settings.MAX_UPLOAD_FILES
        if count > max_files:
            raise TooManyFilesError(details={"max_files": max_files, "uploaded": count})

    def validate_upload_counts(self, file_heads: list[bytes]) -> None:
        """Validate photo/video count limits for a batch upload.

        Takes the leading bytes of each part — libmagic only ever looks at the
        header, so there is no reason to hand it whole files.
        """
        detected = [magic.from_buffer(head, mime=True) for head in file_heads]
        photo_count = sum(1 for mime in detected if mime in IMAGE_MIME_TYPES)
        video_count = sum(1 for mime in detected if mime in VIDEO_MIME_TYPES)

        if 0 < photo_count < MIN_PHOTOS:
            raise TooFewPhotosError(
                details={"min_photos": MIN_PHOTOS, "uploaded": photo_count},
            )
        if photo_count > MAX_PHOTOS:
            raise TooManyPhotosError(
                details={"max_photos": MAX_PHOTOS, "uploaded": photo_count},
            )
        if video_count > MAX_VIDEOS:
            raise TooManyVideosError(
                details={"max_videos": MAX_VIDEOS, "uploaded": video_count},
            )

    async def upload(
        self,
        session_id: UUID,
        upload: SpooledUpload,
        user: AuthUser,
    ) -> Media:
        """Validate, moderate and store one already-spooled upload.

        Thin wrapper over :meth:`upload_batch` so existing single-file callers
        keep working. A single-file storage failure surfaces as a whole-request
        502, exactly as before (``upload_batch`` raises when every file fails).
        """
        result = await self.upload_batch(session_id, [upload], user)
        return result.succeeded[0]

    async def upload_batch(
        self,
        session_id: UUID,
        uploads: list[SpooledUpload],
        user: AuthUser,
    ) -> BatchUploadResult:
        """Validate, moderate and store a batch of already-spooled uploads.

        Three phases (§4 of the media-upload-optimization spec):

        - Phase 0 — validate + moderate every file first (fail-fast,
          whole-request). Any failure raises here and returns the existing 4xx,
          before a single object is stored.
        - Phase A — store bytes concurrently, thread-offloaded and bounded by a
          semaphore. Only STORAGE_UPLOAD_FAILED is expected per file. No coroutine
          touches the DB session — an AsyncSession is not concurrency-safe (§4.1).
        - Phase B — persist the files that stored cleanly in one bulk insert.
        """
        # (1) Authorize once for the whole batch — same session across all files.
        session = await self.sessions_repo.get(session_id)
        if session is None:
            raise NotFoundError("Session not found.")
        if session.profile_id != user.id:
            raise ForbiddenError()

        # (2) Phase 0: validate + moderate EVERY file first. Any failure raises
        #     here and the whole request returns the existing 4xx — nothing stored.
        validated = [await self._validate(session_id, u, user) for u in uploads]

        # (3) Phase A: store bytes concurrently. return_exceptions keeps one
        #     file's failure from cancelling the siblings still uploading.
        sem = asyncio.Semaphore(self.settings.UPLOAD_CONCURRENCY)

        async def _store(v: _ValidatedMedia) -> _PreparedMedia:
            async with sem:
                return await self._store_object(v)

        results = await asyncio.gather(*(_store(v) for v in validated), return_exceptions=True)

        # (4) Partition. gather preserves order, so results zip back to their parts.
        prepared: list[_PreparedMedia] = []
        failed: list[FailedUpload] = []
        for v, result in zip(validated, results, strict=True):
            if isinstance(result, _PreparedMedia):
                prepared.append(result)
            elif isinstance(result, StorageUploadFailedError):
                failed.append(FailedUpload.from_error(v.file_name, result))
                await self._cleanup_partial(v)  # best-effort delete of a half-write
            else:
                raise result  # not an expected storage failure: surface it

        # (5) All files failed to store → whole-request 502, unchanged contract.
        if uploads and not prepared:
            raise StorageUploadFailedError()

        # (6) Phase B: persist the files that stored cleanly — one bulk insert,
        #     one commit. `prepared` keeps upload order, so response order is stable.
        stored = await self.media_repo.create_many(session_id=session_id, items=prepared)
        return BatchUploadResult(succeeded=stored, failed=failed)

    async def _validate(
        self,
        session_id: UUID,
        upload: SpooledUpload,
        user: AuthUser,
    ) -> _ValidatedMedia:
        """Phase 0 for one file: size, libmagic sniff, duration cap, moderation.

        Raises the same 4xx errors as the old ``upload()`` did — a failure here
        rejects the whole batch. Touches no DB session and stores nothing.
        """
        max_bytes = self.settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        if upload.size > max_bytes:
            raise FileTooLargeError(
                details={"max_size_mb": self.settings.MAX_UPLOAD_SIZE_MB},
            )

        detected_mime = magic.from_buffer(upload.head(), mime=True)
        if detected_mime not in IMAGE_MIME_TYPES and detected_mime not in VIDEO_MIME_TYPES:
            raise InvalidMediaTypeError(
                details={"detected": detected_mime, "accepted": ACCEPTED_MIME_TYPES},
            )

        media_type = "image" if detected_mime in IMAGE_MIME_TYPES else "video"
        duration_seconds: Decimal | None = None
        if media_type == "video":
            duration = await asyncio.to_thread(
                self.frame_extractor.probe_duration_path, upload.path
            )
            if duration > self.settings.MAX_VIDEO_DURATION_SEC:
                raise VideoTooLongError(
                    details={"max_seconds": self.settings.MAX_VIDEO_DURATION_SEC},
                )
            duration_seconds = Decimal(f"{duration:.2f}")

        await self._moderate(upload, media_type, detected_mime)

        media_id = uuid4()
        ext = MIME_EXT.get(detected_mime, "bin")
        storage_key = f"{user.id}/{session_id}/{media_id}.{ext}"

        return _ValidatedMedia(
            upload=upload,
            media_type=media_type,
            detected_mime=detected_mime,
            storage_key=storage_key,
            duration_seconds=duration_seconds,
            file_name=upload.file_name,
            file_size_bytes=upload.size,
        )

    async def _store_object(self, v: _ValidatedMedia) -> _PreparedMedia:
        """Phase A for one file: the Supabase PUT, thread-offloaded. No DB access.

        Raises ``StorageUploadFailedError`` on a transient storage/network blip —
        the one per-file failure that yields a 207 rather than failing the batch.
        """
        storage_url = await asyncio.to_thread(
            self.storage.upload_file, v.storage_key, v.upload.path, v.detected_mime
        )
        return _PreparedMedia(
            media_type=v.media_type,
            storage_url=storage_url,
            file_name=v.file_name,
            file_size_bytes=v.file_size_bytes,
            duration_seconds=v.duration_seconds,
        )

    async def _cleanup_partial(self, v: _ValidatedMedia) -> None:
        """Best-effort delete of a half-written object after a storage failure.

        ``StorageClient.delete`` already swallows its own errors, so this never
        masks the storage failure that triggered it.
        """
        await asyncio.to_thread(self.storage.delete, v.storage_key)

    async def list_media(self, session_id: UUID, user: AuthUser) -> list[Media]:
        session = await self.sessions_repo.get(session_id)
        if session is None:
            raise NotFoundError("Session not found.")
        if session.profile_id != user.id:
            raise ForbiddenError()
        return await self.media_repo.list_for_session(session_id)

    async def get_media(self, media_id: UUID, user: AuthUser) -> Media:
        media = await self.media_repo.get(media_id)
        if media is None:
            raise NotFoundError("Media not found.")
        session = await self.sessions_repo.get(media.session_id)
        if session is None or session.profile_id != user.id:
            raise ForbiddenError()
        return media

    async def get_media_for_profile(self, media_id: UUID, profile_id: UUID) -> Media:
        """Get media verifying it belongs to the given profile (token-based auth)."""
        media = await self.media_repo.get(media_id)
        if media is None:
            raise NotFoundError("Media not found.")
        session = await self.sessions_repo.get(media.session_id)
        if session is None or session.profile_id != profile_id:
            raise ForbiddenError()
        return media

    async def delete_media(self, media_id: UUID, user: AuthUser) -> None:
        media = await self.get_media(media_id, user)
        key = self._extract_storage_key(media.storage_url, user.id, media.session_id, media.id)
        if key:
            await asyncio.to_thread(self.storage.delete, key)
        await self.media_repo.delete(media)

    async def _moderate(self, upload: SpooledUpload, media_type: str, mime_type: str) -> None:
        if not self.settings.CONTENT_MODERATION_ENABLED:
            return
        if self.gemini is None:
            logger.warning("Content moderation enabled but GeminiService not provided; skipping.")
            return

        if media_type == "image":
            # Gemini takes bytes, so an image is read in full here — one file at
            # a time, rather than the whole batch as before.
            images = [await asyncio.to_thread(upload.read_all)]
            moderation_mime = mime_type
        else:
            images = await asyncio.to_thread(self.frame_extractor.extract_path, upload.path, 3)
            moderation_mime = "image/jpeg"

        result = await asyncio.to_thread(
            self.gemini.moderate_media_content, images, moderation_mime
        )

        if result.explicit_content:
            raise ExplicitContentError(details={"reason": result.reason})
        if not result.surf_related:
            raise MediaNotSurfRelatedError(details={"reason": result.reason})

    @staticmethod
    def _extract_storage_key(url: str, user_id, session_id, media_id) -> str | None:
        marker = f"{user_id}/{session_id}/"
        idx = url.find(marker)
        if idx < 0:
            return None
        return url[idx:].split("?", 1)[0]
