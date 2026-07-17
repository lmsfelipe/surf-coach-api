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


class GenerateTrainingPlanRequest(_CamelModel):
    review_id: UUID


class ExerciseResponse(_CamelModel):
    id: UUID
    sequence_number: int
    name: str
    description: str
    sets: int
    reps: str
    video_url: str | None = None
    created_at: datetime


class WorkoutResponse(_CamelModel):
    id: UUID
    plan_id: UUID
    sequence_number: int
    title: str
    focus_area: str
    created_at: datetime
    exercises: list[ExerciseResponse]


class TrainingPlanResponse(_CamelModel):
    id: UUID
    review_id: UUID
    profile_id: UUID
    status: str
    error_message: str | None = None
    generated_by: str
    ai_model_version: str | None = None
    created_at: datetime
    workouts: list[WorkoutResponse] = []


class TrainingPlanListResponse(_CamelModel):
    items: list[TrainingPlanResponse]
    total: int
