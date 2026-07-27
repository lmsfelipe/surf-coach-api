"""create exercises table

Revision ID: 0008_create_exercises
Revises: 0007_create_training_plans_and_workouts
Create Date: 2026-04-24
"""

from __future__ import annotations

from alembic import op

revision = "0008_create_exercises"
down_revision = "0007_training_plans_workouts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.exercises (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            workout_id UUID NOT NULL REFERENCES public.workouts(id) ON DELETE CASCADE,
            sequence_number INTEGER NOT NULL CHECK (sequence_number >= 1),
            name TEXT NOT NULL,
            description TEXT NOT NULL,
            sets INTEGER NOT NULL CHECK (sets >= 1),
            reps TEXT NOT NULL,
            video_url TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_exercises_workout_id ON public.exercises(workout_id)")

    op.execute("ALTER TABLE public.exercises ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY "exercises_select_own" ON public.exercises
            FOR SELECT USING (
                EXISTS (
                    SELECT 1 FROM public.workouts w
                    JOIN public.training_plans tp ON tp.id = w.plan_id
                    WHERE w.id = exercises.workout_id AND auth.uid() = tp.profile_id
                )
            )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.exercises")
