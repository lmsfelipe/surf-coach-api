from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Numeric, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.db import Base


class Review(Base):
    __tablename__ = "reviews"
    __table_args__ = (
        UniqueConstraint("session_id", name="uq_reviews_session"),
        {"schema": "public"},
    )

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        primary_key=True,
        server_default=text("gen_random_uuid()"),
    )
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("public.sessions.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
    )
    profile_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("public.profiles.id", ondelete="CASCADE"),
        nullable=False,
    )
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    improvement_tips: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    score_flow: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    score_drop: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    score_balance: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    score_wave_selection: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    score_maneuvers: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    score_arms: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    overall_score: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True)
    ai_model_version: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
