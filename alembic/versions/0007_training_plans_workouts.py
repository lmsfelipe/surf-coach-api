"""create training_plans and workouts tables

Revision ID: 0007_create_training_plans_and_workouts
Revises: 0006_nullable_scores
Create Date: 2026-04-24
"""

from __future__ import annotations

from alembic import op

revision = "0007_training_plans_workouts"
down_revision = "0006_nullable_scores"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE public.training_plans (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            review_id UUID NOT NULL UNIQUE REFERENCES public.reviews(id) ON DELETE CASCADE,
            profile_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
            generated_by TEXT NOT NULL DEFAULT 'ai'
                CHECK (generated_by IN ('ai', 'coach', 'personal_trainer')),
            ai_model_version TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_training_plans_profile_id ON public.training_plans(profile_id)")

    op.execute("""
        CREATE TABLE public.workouts (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            plan_id UUID NOT NULL REFERENCES public.training_plans(id) ON DELETE CASCADE,
            sequence_number INTEGER NOT NULL CHECK (sequence_number BETWEEN 1 AND 3),
            title TEXT NOT NULL,
            focus_area TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            UNIQUE (plan_id, sequence_number)
        )
    """)
    op.execute("CREATE INDEX idx_workouts_plan_id ON public.workouts(plan_id)")

    op.execute("ALTER TABLE public.training_plans ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY "training_plans_select_own" ON public.training_plans
            FOR SELECT USING (auth.uid() = profile_id)
    """)

    op.execute("ALTER TABLE public.workouts ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY "workouts_select_own" ON public.workouts
            FOR SELECT USING (
                EXISTS (
                    SELECT 1 FROM public.training_plans tp
                    WHERE tp.id = workouts.plan_id AND auth.uid() = tp.profile_id
                )
            )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS public.workouts")
    op.execute("DROP TABLE IF EXISTS public.training_plans")
