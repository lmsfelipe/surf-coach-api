from datetime import date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

SurfLevel = Literal["beginner", "intermediate", "advanced", "pro"]
Gender = Literal["male", "female"]


class _CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class ProfileOut(_CamelModel):
    id: UUID
    email: str
    surf_level: SurfLevel
    height_cm: int | None = None
    weight_kg: int | None = None
    name: str | None = None
    gender: Gender | None = None
    birthday: date | None = None
    avatar_url: str | None = None
    created_at: datetime
    updated_at: datetime


class ProfileUpdate(_CamelModel):
    surf_level: SurfLevel | None = None
    height_cm: int | None = Field(default=None, ge=100, le=250)
    weight_kg: int | None = Field(default=None, ge=30, le=200)
    name: str | None = Field(default=None, max_length=200)
    gender: Gender | None = None
    birthday: date | None = None
    avatar_url: str | None = None
