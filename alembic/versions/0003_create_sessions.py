"""create sessions table

Revision ID: 0003_create_sessions
Revises: 05c9449a6d9d
Create Date: 2026-04-17
"""
from __future__ import annotations

from alembic import op

revision = "0003_create_sessions"
down_revision = "05c9449a6d9d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.sessions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            profile_id UUID NOT NULL REFERENCES public.profiles(id) ON DELETE CASCADE,
            session_date DATE NOT NULL,
            location TEXT NOT NULL,
            wave_conditions TEXT NOT NULL,
            board_type TEXT,
            notes TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_sessions_profile_id ON public.sessions(profile_id)")
    op.execute("ALTER TABLE public.sessions ENABLE ROW LEVEL SECURITY")
    op.execute(
        'CREATE POLICY "sessions_select_own" ON public.sessions '
        "FOR SELECT USING (auth.uid() = profile_id)"
    )
    op.execute(
        'CREATE POLICY "sessions_insert_own" ON public.sessions '
        "FOR INSERT WITH CHECK (auth.uid() = profile_id)"
    )
    op.execute(
        'CREATE POLICY "sessions_update_own" ON public.sessions '
        "FOR UPDATE USING (auth.uid() = profile_id)"
    )
    op.execute(
        'CREATE POLICY "sessions_delete_own" ON public.sessions '
        "FOR DELETE USING (auth.uid() = profile_id)"
    )


def downgrade() -> None:
    op.execute('DROP POLICY IF EXISTS "sessions_delete_own" ON public.sessions')
    op.execute('DROP POLICY IF EXISTS "sessions_update_own" ON public.sessions')
    op.execute('DROP POLICY IF EXISTS "sessions_insert_own" ON public.sessions')
    op.execute('DROP POLICY IF EXISTS "sessions_select_own" ON public.sessions')
    op.execute("DROP INDEX IF EXISTS public.idx_sessions_profile_id")
    op.execute("DROP TABLE IF EXISTS public.sessions")
