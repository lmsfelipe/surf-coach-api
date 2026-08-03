from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.core.errors import AppError


class _CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class MediaOut(_CamelModel):
    id: UUID
    session_id: UUID
    media_type: Literal["image", "video"]
    content_url: str
    file_name: str
    file_size_bytes: int | None = None
    duration_seconds: float | None = None
    created_at: datetime


class FailedUpload(_CamelModel):
    """One file of a batch that stored-stage failed (207 Multi-Status only)."""

    file_name: str
    code: str
    message: str
    details: dict[str, Any] | None = None

    @classmethod
    def from_error(cls, file_name: str, error: AppError) -> "FailedUpload":
        return cls(
            file_name=file_name,
            code=error.code,
            message=error.message,
            details=error.details,
        )


class BatchUploadResult(_CamelModel):
    """207 Multi-Status body: some files stored, some hit STORAGE_UPLOAD_FAILED."""

    succeeded: list[MediaOut]
    failed: list[FailedUpload]
