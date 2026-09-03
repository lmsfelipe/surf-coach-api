from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
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

    async def create_pending(self, *, session_id: UUID, profile_id: UUID) -> Review:
        review = Review(
            session_id=session_id,
            profile_id=profile_id,
            status="processing",
            processing_started_at=func.now(),
        )
        self.db.add(review)
        await self.db.commit()
        await self.db.refresh(review)
        return review

    async def mark_completed(
        self,
        review_id: UUID,
        *,
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
        review = await self.get(review_id)
        assert review is not None
        review.status = "completed"
        review.error_message = None
        review.narrative = narrative
        review.improvement_tips = improvement_tips
        review.score_flow = score_flow
        review.score_drop = score_drop
        review.score_balance = score_balance
        review.score_wave_selection = score_wave_selection
        review.score_maneuvers = score_maneuvers
        review.score_arms = score_arms
        review.overall_score = overall_score
        review.ai_model_version = ai_model_version
        await self.db.commit()
        await self.db.refresh(review)
        return review

    async def mark_failed(self, review_id: UUID, error_message: str) -> Review:
        review = await self.get(review_id)
        assert review is not None
        review.status = "failed"
        review.error_message = error_message
        await self.db.commit()
        await self.db.refresh(review)
        return review

    async def reset_for_retry(self, review_id: UUID) -> Review:
        review = await self.get(review_id)
        assert review is not None
        review.status = "processing"
        review.error_message = None
        review.processing_started_at = func.now()
        await self.db.commit()
        await self.db.refresh(review)
        return review

    async def get(self, review_id: UUID) -> Review | None:
        result = await self.db.execute(select(Review).where(Review.id == review_id))
        return result.scalar_one_or_none()

    async def delete(self, review_id: UUID) -> None:
        review = await self.get(review_id)
        if review is not None:
            await self.db.delete(review)
            await self.db.commit()

    async def get_for_session(self, session_id: UUID) -> Review | None:
        result = await self.db.execute(select(Review).where(Review.session_id == session_id))
        return result.scalar_one_or_none()


class TrainingPlanRepository:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    def _eager_query(self):
        # populate_existing is required, not cosmetic: the mutating methods below
        # commit and then re-read to return a fully-populated plan. The session
        # is built with expire_on_commit=False, so without this the identity map
        # hands back the instance loaded *before* the write — a plan whose
        # `workouts` collection is still the empty one from create_pending, even
        # though the rows were inserted correctly.
        return (
            select(TrainingPlan)
            .options(selectinload(TrainingPlan.workouts).selectinload(Workout.exercises))
            .execution_options(populate_existing=True)
        )

    async def get_by_id(self, plan_id: UUID) -> TrainingPlan | None:
        result = await self.db.execute(self._eager_query().where(TrainingPlan.id == plan_id))
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
            select(Workout).options(selectinload(Workout.exercises)).where(Workout.id == workout_id)
        )
        return result.scalar_one_or_none()

    async def delete(self, plan_id: UUID) -> None:
        plan = await self.get_by_id(plan_id)
        if plan is not None:
            await self.db.delete(plan)
            await self.db.commit()

    async def create_pending(self, *, review_id: UUID, profile_id: UUID) -> TrainingPlan:
        plan = TrainingPlan(
            review_id=review_id,
            profile_id=profile_id,
            status="processing",
            generated_by="ai",
            processing_started_at=func.now(),
        )
        self.db.add(plan)
        await self.db.commit()
        plan_with_relations = await self.get_by_id(plan.id)
        assert plan_with_relations is not None
        return plan_with_relations

    async def mark_completed(
        self,
        plan_id: UUID,
        *,
        ai_model_version: str | None,
        workouts: list[dict],
    ) -> TrainingPlan:
        plan = await self.get_by_id(plan_id)
        assert plan is not None
        plan.status = "completed"
        plan.error_message = None
        plan.ai_model_version = ai_model_version

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
        result = await self.get_by_id(plan.id)
        assert result is not None
        return result

    async def mark_failed(self, plan_id: UUID, error_message: str) -> TrainingPlan:
        plan = await self.get_by_id(plan_id)
        assert plan is not None
        plan.status = "failed"
        plan.error_message = error_message
        await self.db.commit()
        plan_with_relations = await self.get_by_id(plan.id)
        assert plan_with_relations is not None
        return plan_with_relations

    async def reset_for_retry(self, plan_id: UUID) -> TrainingPlan:
        plan = await self.get_by_id(plan_id)
        assert plan is not None
        plan.status = "processing"
        plan.error_message = None
        plan.processing_started_at = func.now()
        await self.db.commit()
        plan_with_relations = await self.get_by_id(plan.id)
        assert plan_with_relations is not None
        return plan_with_relations

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
