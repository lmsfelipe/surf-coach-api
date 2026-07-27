from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from functools import lru_cache

import httpx
import structlog
from supabase import Client, create_client

from app.core.config import get_settings
from app.core.errors import RangeNotSatisfiableError, StorageDownloadError, StorageUploadFailedError

logger = structlog.get_logger(__name__)

STREAM_TIMEOUT = httpx.Timeout(60.0)


@dataclass
class StorageDownloadResult:
    """Result of a (possibly partial) download from Supabase Storage."""

    status_code: int  # 200 or 206
    content: bytes
    content_type: str
    content_length: int
    content_range: str | None = None


class StorageStream:
    """An open (possibly partial) storage response whose body is not yet read.

    The upstream connection stays open until ``aclose()`` — the caller owns its
    lifetime, because the bytes are consumed by the response long after the
    method that opened it has returned.
    """

    def __init__(
        self,
        *,
        status_code: int,
        content_type: str,
        content_length: int | None,
        content_range: str | None,
        body: AsyncIterator[bytes],
        close: Callable[[], Awaitable[None]],
    ) -> None:
        self.status_code = status_code
        self.content_type = content_type
        self.content_length = content_length
        self.content_range = content_range
        self._body = body
        self._close = close

    def aiter_bytes(self) -> AsyncIterator[bytes]:
        return self._body

    async def aclose(self) -> None:
        await self._close()


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

    def upload_file(self, key: str, path: str, content_type: str) -> str:
        """Upload from a file on disk, streaming it rather than reading it into memory."""
        try:
            # A BufferedReader is handed straight to httpx as a multipart part,
            # so the file is read in chunks; passing the path instead would make
            # storage3 open a handle it never closes.
            with open(path, "rb") as fh:
                self._storage().upload(
                    path=key,
                    file=fh,
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
            raise StorageDownloadError("Media download failed.") from e

    def delete(self, key: str) -> None:
        try:
            self._storage().remove([key])
        except Exception:
            logger.warning("Supabase Storage delete failed for key=%s", key, exc_info=True)

    def _object_request(self, key: str, range_header: str | None) -> tuple[str, dict[str, str]]:
        settings = get_settings()
        url = f"{settings.SUPABASE_URL}/storage/v1/object/{self._bucket}/{key}"
        headers = {
            "Authorization": f"Bearer {settings.SUPABASE_SERVICE_ROLE_KEY}",
            "apikey": settings.SUPABASE_SERVICE_ROLE_KEY,
        }
        if range_header:
            headers["Range"] = range_header
        return url, headers

    @staticmethod
    def _raise_for_storage_status(status_code: int, key: str, body_preview: str) -> None:
        if status_code == 416:
            raise RangeNotSatisfiableError()
        if status_code >= 400:
            logger.error(
                "Supabase Storage returned %s for key=%s: %s",
                status_code,
                key,
                body_preview,
            )
            raise StorageDownloadError()

    async def stream_range(
        self,
        key: str,
        range_header: str | None = None,
    ) -> StorageStream:
        """Open an object for streaming, with optional HTTP Range support.

        Returns before the body is read; the caller must ``aclose()`` the stream
        once it has consumed ``aiter_bytes()``. Memory stays flat regardless of
        object size, which a range-less request for a full video otherwise would
        not.
        """
        url, headers = self._object_request(key, range_header)

        client = httpx.AsyncClient(timeout=STREAM_TIMEOUT)
        try:
            request = client.build_request("GET", url, headers=headers)
            resp = await client.send(request, stream=True)
        except httpx.HTTPError as e:
            await client.aclose()
            logger.exception("Supabase Storage stream failed for key=%s", key)
            raise StorageDownloadError() from e

        if resp.status_code >= 400:
            try:
                body = await resp.aread()
            finally:
                await resp.aclose()
                await client.aclose()
            self._raise_for_storage_status(
                resp.status_code, key, body.decode(errors="replace")[:200]
            )

        async def _close() -> None:
            await resp.aclose()
            await client.aclose()

        content_length = resp.headers.get("content-length")
        return StorageStream(
            status_code=resp.status_code,
            content_type=resp.headers.get("content-type", "application/octet-stream"),
            content_length=int(content_length) if content_length is not None else None,
            content_range=resp.headers.get("content-range"),
            body=resp.aiter_bytes(),
            close=_close,
        )

    async def download_range(
        self,
        key: str,
        range_header: str | None = None,
    ) -> StorageDownloadResult:
        """Buffered variant of :meth:`stream_range`, for callers that need bytes."""
        url, headers = self._object_request(key, range_header)

        try:
            async with httpx.AsyncClient(timeout=STREAM_TIMEOUT) as client:
                resp = await client.get(url, headers=headers)
        except httpx.HTTPError as e:
            logger.exception("Supabase Storage download failed for key=%s", key)
            raise StorageDownloadError() from e

        self._raise_for_storage_status(resp.status_code, key, resp.text[:200])

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
