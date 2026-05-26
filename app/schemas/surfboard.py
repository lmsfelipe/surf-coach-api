from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

BoardType = Literal["shortboard", "longboard", "funboard", "bodyboard", "other"]


class _CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class SurfboardCreate(_CamelModel):
    board_type: BoardType
    board_size: float = Field(..., gt=0, description="Board length in feet")
    volume: float | None = Field(default=None, gt=0, description="Volume in litres")
    label: str | None = Field(default=None, max_length=200)


class SurfboardUpdate(_CamelModel):
    board_type: BoardType | None = None
    board_size: float | None = Field(default=None, gt=0)
    volume: float | None = Field(default=None, gt=0)
    label: str | None = Field(default=None, max_length=200)


class SurfboardOut(_CamelModel):
    id: UUID
    profile_id: UUID
    board_type: str
    board_size: float
    volume: float | None = None
    label: str | None = None
    created_at: datetime
    updated_at: datetime
