from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


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
    storage_url: str
    file_name: str
    file_size_bytes: int | None = None
    duration_seconds: float | None = None
    created_at: datetime
