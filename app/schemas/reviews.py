from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel


class _CamelModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel,
        populate_by_name=True,
        from_attributes=True,
    )


class ReviewCreate(_CamelModel):
    session_id: UUID


class ReviewOut(_CamelModel):
    id: UUID
    session_id: UUID
    profile_id: UUID
    status: str
    error_message: str | None = None
    narrative: str | None = None
    improvement_tips: list[str] | None = None
    score_flow: float | None = None
    score_drop: float | None = None
    score_balance: float | None = None
    score_wave_selection: float | None = None
    score_maneuvers: float | None = None
    score_arms: float | None = None
    overall_score: float | None = None
    ai_model_version: str | None = None
    created_at: datetime
