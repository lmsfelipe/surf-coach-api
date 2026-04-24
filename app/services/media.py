import logging
from decimal import Decimal
from uuid import UUID, uuid4

import magic

from app.core.config import get_settings
from app.core.errors import (
    FileTooLargeError,
    ForbiddenError,
    InvalidMediaTypeError,
    NotFoundError,
    VideoTooLongError,
)
from app.core.frame_extractor import FrameExtractor
from app.core.security.jwt import AuthUser
from app.core.storage import StorageClient
from app.models.media import Media
from app.repositories.media import MediaRepository
from app.repositories.sessions import SessionsRepository

logger = logging.getLogger(__name__)


IMAGE_MIME_TYPES = {"image/jpeg", "image/png", "image/webp"}
VIDEO_MIME_TYPES = {"video/mp4", "video/quicktime", "video/x-m4v"}
ACCEPTED_MIME_TYPES = sorted(IMAGE_MIME_TYPES | VIDEO_MIME_TYPES)

MIME_EXT = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/x-m4v": "m4v",
}


class MediaService:
    def __init__(
        self,
        media_repo: MediaRepository,
        sessions_repo: SessionsRepository,
        storage: StorageClient,
        frame_extractor: FrameExtractor,
    ) -> None:
        self.media_repo = media_repo
        self.sessions_repo = sessions_repo
        self.storage = storage
        self.frame_extractor = frame_extractor
        self.settings = get_settings()

    async def upload(
        self,
        session_id: UUID,
        file_bytes: bytes,
        file_name: str,
        user: AuthUser,
    ) -> Media:
        session = await self.sessions_repo.get(session_id)
        if session is None:
            raise NotFoundError("Session not found.")
        if session.profile_id != user.id:
            raise ForbiddenError()

        size = len(file_bytes)
        max_bytes = self.settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        if size > max_bytes:
            raise FileTooLargeError(
                details={"max_size_mb": self.settings.MAX_UPLOAD_SIZE_MB},
            )

        detected_mime = magic.from_buffer(file_bytes, mime=True)
        if detected_mime not in IMAGE_MIME_TYPES and detected_mime not in VIDEO_MIME_TYPES:
            raise InvalidMediaTypeError(
                details={"detected": detected_mime, "accepted": ACCEPTED_MIME_TYPES},
            )

        media_type = "image" if detected_mime in IMAGE_MIME_TYPES else "video"
        duration_seconds: Decimal | None = None
        if media_type == "video":
            duration = self.frame_extractor.probe_duration(file_bytes)
            if duration > self.settings.MAX_VIDEO_DURATION_SEC:
                raise VideoTooLongError(
                    details={"max_seconds": self.settings.MAX_VIDEO_DURATION_SEC},
                )
            duration_seconds = Decimal(f"{duration:.2f}")

        media_id = uuid4()
        ext = MIME_EXT.get(detected_mime, "bin")
        storage_key = f"{user.id}/{session_id}/{media_id}.{ext}"

        storage_url = self.storage.upload(storage_key, file_bytes, detected_mime)

        media = await self.media_repo.create(
            session_id=session_id,
            media_type=media_type,
            storage_url=storage_url,
            file_name=file_name,
            file_size_bytes=size,
            duration_seconds=duration_seconds,
        )
        return media

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

    async def delete_media(self, media_id: UUID, user: AuthUser) -> None:
        media = await self.get_media(media_id, user)
        key = self._extract_storage_key(media.storage_url, user.id, media.session_id, media.id)
        if key:
            self.storage.delete(key)
        await self.media_repo.delete(media)

    @staticmethod
    def _extract_storage_key(url: str, user_id, session_id, media_id) -> str | None:
        marker = f"{user_id}/{session_id}/"
        idx = url.find(marker)
        if idx < 0:
            return None
        return url[idx:].split("?", 1)[0]
