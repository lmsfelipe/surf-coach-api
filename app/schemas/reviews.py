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
    narrative: str
    improvement_tips: list[str]
    score_flow: float
    score_drop: float
    score_balance: float
    score_wave_selection: float
    score_maneuvers: float
    score_arms: float
    overall_score: float
    ai_model_version: str | None = None
    created_at: datetime
