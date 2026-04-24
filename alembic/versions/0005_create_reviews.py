"""create reviews table

Revision ID: 0005_create_reviews
Revises: 0004_create_media
Create Date: 2026-04-17
"""
from __future__ import annotations

from alembic import op

revision = "0005_create_reviews"
down_revision = "0004_create_media"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.reviews (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id UUID NOT NULL UNIQUE REFERENCES public.sessions(id) ON DELETE CASCADE,
            profile_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
            narrative TEXT NOT NULL,
            improvement_tips TEXT[] NOT NULL,
            score_flow NUMERIC(4,1) NOT NULL CHECK (score_flow BETWEEN 0 AND 10),
            score_drop NUMERIC(4,1) NOT NULL CHECK (score_drop BETWEEN 0 AND 10),
            score_balance NUMERIC(4,1) NOT NULL CHECK (score_balance BETWEEN 0 AND 10),
            score_wave_selection NUMERIC(4,1) NOT NULL CHECK (score_wave_selection BETWEEN 0 AND 10),
            score_maneuvers NUMERIC(4,1) NOT NULL CHECK (score_maneuvers BETWEEN 0 AND 10),
            score_arms NUMERIC(4,1) NOT NULL CHECK (score_arms BETWEEN 0 AND 10),
            overall_score NUMERIC(4,1) NOT NULL CHECK (overall_score BETWEEN 0 AND 10),
            ai_model_version TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_reviews_profile_id ON public.reviews(profile_id)")
    op.execute("ALTER TABLE public.reviews ENABLE ROW LEVEL SECURITY")
    op.execute(
        'CREATE POLICY "reviews_select_own" ON public.reviews '
        "FOR SELECT USING (auth.uid() = profile_id)"
    )


def downgrade() -> None:
    op.execute('DROP POLICY IF EXISTS "reviews_select_own" ON public.reviews')
    op.execute("DROP INDEX IF EXISTS public.idx_reviews_profile_id")
    op.execute("DROP TABLE IF EXISTS public.reviews")
