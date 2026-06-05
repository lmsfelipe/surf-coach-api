from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.exercise import Exercise
from app.models.review import Review
from app.models.training_plan import TrainingPlan
from app.models.workout import Workout


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


class TrainingPlanRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _eager_query(self):
        return select(TrainingPlan).options(
            selectinload(TrainingPlan.workouts).selectinload(Workout.exercises)
        )

    async def get_by_id(self, plan_id: UUID) -> TrainingPlan | None:
        result = await self.db.execute(
            self._eager_query().where(TrainingPlan.id == plan_id)
        )
        return result.scalar_one_or_none()

    async def get_by_review_id(self, review_id: UUID) -> TrainingPlan | None:
        result = await self.db.execute(
            self._eager_query().where(TrainingPlan.review_id == review_id)
        )
        return result.scalar_one_or_none()

    async def list_for_profile(self, profile_id: UUID) -> list[TrainingPlan]:
        result = await self.db.execute(
            self._eager_query()
            .where(TrainingPlan.profile_id == profile_id)
            .order_by(TrainingPlan.created_at.desc())
        )
        return list(result.scalars().all())

    async def get_workout_by_id(self, workout_id: UUID) -> Workout | None:
        result = await self.db.execute(
            select(Workout)
            .options(selectinload(Workout.exercises))
            .where(Workout.id == workout_id)
        )
        return result.scalar_one_or_none()

    async def create(
        self,
        *,
        review_id: UUID,
        profile_id: UUID,
        ai_model_version: str | None,
        workouts: list[dict],
    ) -> TrainingPlan:
        plan = TrainingPlan(
            review_id=review_id,
            profile_id=profile_id,
            generated_by="ai",
            ai_model_version=ai_model_version,
        )
        self.db.add(plan)
        await self.db.flush()

        for w_data in workouts:
            workout = Workout(
                plan_id=plan.id,
                sequence_number=w_data["sequence_number"],
                title=w_data["title"],
                focus_area=w_data["focus_area"],
            )
            self.db.add(workout)
            await self.db.flush()

            for idx, ex_data in enumerate(w_data["exercises"], start=1):
                exercise = Exercise(
                    workout_id=workout.id,
                    sequence_number=idx,
                    name=ex_data["name"],
                    description=ex_data["description"],
                    sets=ex_data["sets"],
                    reps=ex_data["reps"],
                    video_url=ex_data.get("video_url"),
                )
                self.db.add(exercise)

        await self.db.commit()

        plan_with_relations = await self.get_by_id(plan.id)
        assert plan_with_relations is not None
        return plan_with_relations
