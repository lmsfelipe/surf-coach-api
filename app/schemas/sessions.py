from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel


class _CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class SessionCreate(_CamelModel):
    session_date: date
    location: str = Field(..., min_length=1, max_length=200)
    wave_size: float = Field(..., gt=0, description="Wave face height in feet")
    surfboard_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=1000)


class SessionOut(_CamelModel):
    id: UUID
    profile_id: UUID
    session_date: date
    location: str
    wave_size: float
    surfboard_id: UUID | None = None
    notes: str | None = None
    created_at: datetime
    updated_at: datetime
