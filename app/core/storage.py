import logging
from functools import lru_cache

from supabase import Client, create_client

from app.core.config import get_settings
from app.core.errors import StorageUploadFailedError

logger = logging.getLogger(__name__)


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


@lru_cache(maxsize=1)
def get_storage_client() -> StorageClient:
    settings = get_settings()
    client = create_client(settings.SUPABASE_URL, settings.SUPABASE_SERVICE_ROLE_KEY)
    return StorageClient(client=client, bucket=settings.SUPABASE_BUCKET)
