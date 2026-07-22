from dataclasses import dataclass
from functools import lru_cache

import httpx
import structlog
from supabase import Client, create_client

from app.core.config import get_settings
from app.core.errors import RangeNotSatisfiableError, StorageDownloadError, StorageUploadFailedError

logger = structlog.get_logger(__name__)


@dataclass
class StorageDownloadResult:
    """Result of a (possibly partial) download from Supabase Storage."""

    status_code: int  # 200 or 206
    content: bytes
    content_type: str
    content_length: int
    content_range: str | None = None


class StorageClient:
    def __init__(self, client: Client, bucket: str) -> None:
        self._client = client
        self._bucket = bucket

    def _storage(self):
        return self._client.storage.from_(self._bucket)

    def upload(self, key: str, data: bytes, content_type: str) -> str:
        try:
            self._storage().upload(
                path=key,
                file=data,
                file_options={"content-type": content_type, "upsert": "true"},
            )
            return self._storage().get_public_url(key)
        except Exception as e:
            logger.exception("Supabase Storage upload failed for key=%s", key)
            raise StorageUploadFailedError() from e

    def download(self, key: str) -> bytes:
        try:
            return self._storage().download(key)
        except Exception as e:
            logger.exception("Supabase Storage download failed for key=%s", key)
            raise StorageUploadFailedError("Media download failed.") from e

    def delete(self, key: str) -> None:
        try:
            self._storage().remove([key])
        except Exception:
            logger.warning("Supabase Storage delete failed for key=%s", key, exc_info=True)

    async def download_range(
        self, key: str, range_header: str | None = None,
    ) -> StorageDownloadResult:
        """Download an object from Supabase Storage with optional HTTP Range support."""
        settings = get_settings()
        url = f"{settings.SUPABASE_URL}/storage/v1/object/{self._bucket}/{key}"
        headers = {
            "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
        }
        if range_header:
            headers["Range"] = range_header

        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            logger.exception("Supabase Storage stream failed for key=%s", key)
            raise StorageDownloadError() from e

        if resp.status_code == 416:
            raise RangeNotSatisfiableError()
        if resp.status_code >= 400:
            logger.error(
                "Supabase Storage returned %s for key=%s: %s",
                resp.status_code, key, resp.text[:200],
            )
            raise StorageDownloadError()

        return StorageDownloadResult(
            status_code=resp.status_code,
            content=resp.content,
            content_type=resp.headers.get("content-type", "application/octet-stream"),
            content_length=int(resp.headers.get("content-length", str(len(resp.content)))),
            content_range=resp.headers.get("content-range"),
        )


@lru_cache(maxsize=1)
def get_storage_client() -> StorageClient:
    settings = get_settings()
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    return StorageClient(client=client, bucket=settings.SUPABASE_BUCKET)
