"""Bounded, disk-backed spooling of multipart upload parts.

Starlette hands each part to the route as an ``UploadFile`` with no size limit
of its own, and ``await file.read()`` materialises the whole part in memory.
``spool_upload`` instead copies a part into a temp file a chunk at a time with a
running byte counter, so an oversized part is rejected mid-stream rather than
after all of it has landed.

The spool is a plain file on disk rather than a ``SpooledTemporaryFile`` because
both downstream consumers need a path or a real handle: OpenCV opens videos by
filename, and the Supabase upload streams from an open reader instead of taking
a ``bytes`` payload.
"""

from __future__ import annotations

import os
import tempfile
from typing import BinaryIO

import structlog
from fastapi import UploadFile

from app.core.errors import FileTooLargeError

logger = structlog.get_logger(__name__)

CHUNK_SIZE = 1024 * 1024
SNIFF_BYTES = 2048


class SpooledUpload:
    """One uploaded part, backed by a temp file the caller must ``close()``."""

    def __init__(self, path: str, size: int, file_name: str) -> None:
        self.path = path
        self.size = size
        self.file_name = file_name

    def head(self, n: int = SNIFF_BYTES) -> bytes:
        """First ``n`` bytes — all libmagic needs to sniff the content type."""
        with open(self.path, "rb") as fh:
            return fh.read(n)

    def open(self) -> BinaryIO:
        return open(self.path, "rb")

    def read_all(self) -> bytes:
        """Whole part as bytes. Only for consumers that cannot take a path."""
        with open(self.path, "rb") as fh:
            return fh.read()

    def close(self) -> None:
        try:
            os.remove(self.path)
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Failed to remove upload spool %s", self.path, exc_info=True)


async def spool_upload(file: UploadFile, max_bytes: int) -> SpooledUpload:
    """Stream one part to disk, aborting the moment it exceeds ``max_bytes``."""
    fd, path = tempfile.mkstemp(prefix="surf-upload-")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(CHUNK_SIZE):
                size += len(chunk)
                if size > max_bytes:
                    raise FileTooLargeError(
                        details={
                            "max_size_mb": max_bytes // (1024 * 1024),
                            "file_name": file.filename,
                        },
                    )
                out.write(chunk)
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise

    return SpooledUpload(path=path, size=size, file_name=file.filename or "upload")
