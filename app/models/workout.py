from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


class Workout(Base):
    __tablename__ = "workouts"
    __table_args__ = (
        UniqueConstraint("plan_id", "sequence_number", name="uq_workouts_plan_seq"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()")
    )
    plan_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("public.training_plans.id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence_number: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    focus_area: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    plan: Mapped["TrainingPlan"] = relationship(  # noqa: F821
        "TrainingPlan", back_populates="workouts"
    )
    exercises: Mapped[list["Exercise"]] = relationship(  # noqa: F821
        "Exercise", back_populates="workout", order_by="Exercise.sequence_number"
    )
