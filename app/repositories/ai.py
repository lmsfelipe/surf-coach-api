from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import Review


class ReviewRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create(
        self,
        *,
        session_id: UUID,
        profile_id: UUID,
        narrative: str,
        improvement_tips: list[str],
        score_flow: Decimal | None,
        score_drop: Decimal | None,
        score_balance: Decimal | None,
        score_wave_selection: Decimal | None,
        score_maneuvers: Decimal | None,
        score_arms: Decimal | None,
        overall_score: Decimal | None,
        ai_model_version: str | None,
    ) -> Review:
        review = Review(
            session_id=session_id,
            profile_id=profile_id,
            narrative=narrative,
            improvement_tips=improvement_tips,
            score_flow=score_flow,
            score_drop=score_drop,
            score_balance=score_balance,
            score_wave_selection=score_wave_selection,
            score_maneuvers=score_maneuvers,
            score_arms=score_arms,
            overall_score=overall_score,
            ai_model_version=ai_model_version,
        )
        self.db.add(review)
        await self.db.commit()
        await self.db.refresh(review)
        return review

    async def get(self, review_id: UUID) -> Review | None:
        result = await self.db.execute(select(Review).where(Review.id == review_id))
        return result.scalar_one_or_none()

    async def get_for_session(self, session_id: UUID) -> Review | None:
        result = await self.db.execute(select(Review).where(Review.session_id == session_id))
        return result.scalar_one_or_none()
