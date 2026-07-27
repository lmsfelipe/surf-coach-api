"""refactor sessions: wave_size and surfboard_id

Revision ID: 0010_sessions_wave_board
Revises: 0009_extend_profiles_and_add_surfboards
Create Date: 2026-05-04
"""

from __future__ import annotations

from alembic import op

revision = "0010_sessions_wave_board"
down_revision = "0009_profiles_and_surfboards"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.sessions
            ADD COLUMN wave_size NUMERIC(4,1),
            ADD COLUMN surfboard_id UUID
                REFERENCES public.surfboards(id) ON DELETE SET NULL
    """)

    # Back-fill existing rows
    op.execute("UPDATE public.sessions SET wave_size = 3.0 WHERE wave_size IS NULL")

    op.execute("""
        ALTER TABLE public.sessions
            ALTER COLUMN wave_size SET NOT NULL,
            DROP COLUMN wave_conditions,
            DROP COLUMN board_type
    """)

    op.execute("CREATE INDEX idx_sessions_surfboard_id ON public.sessions(surfboard_id)")


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS public.idx_sessions_surfboard_id")
    op.execute("""
        ALTER TABLE public.sessions
            ADD COLUMN wave_conditions TEXT NOT NULL DEFAULT 'unknown',
            ADD COLUMN board_type TEXT,
            DROP COLUMN IF EXISTS surfboard_id,
            DROP COLUMN IF EXISTS wave_size
    """)
