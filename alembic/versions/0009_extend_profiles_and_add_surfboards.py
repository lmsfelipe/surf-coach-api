"""extend profiles and add surfboards table

Revision ID: 0009_profiles_and_surfboards
Revises: 0008_create_exercises
Create Date: 2026-05-04
"""

from __future__ import annotations

from alembic import op

revision = "0009_profiles_and_surfboards"
down_revision = "0008_create_exercises"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        ALTER TABLE public.profiles
            ADD COLUMN name TEXT,
            ADD COLUMN gender TEXT CHECK (gender IN ('male', 'female')),
            ADD COLUMN birthday DATE,
            ADD COLUMN avatar_url TEXT
    """)

    op.execute("""
        CREATE TABLE public.surfboards (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            profile_id UUID NOT NULL
                REFERENCES public.profiles(id) ON DELETE CASCADE,
            board_type TEXT NOT NULL
                CHECK (board_type IN ('shortboard', 'longboard', 'funboard', 'bodyboard', 'other')),
            board_size NUMERIC(4,2) NOT NULL CHECK (board_size > 0),
            volume NUMERIC(5,1) CHECK (volume > 0),
            label TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    op.execute("CREATE INDEX idx_surfboards_profile_id ON public.surfboards(profile_id)")

    op.execute("ALTER TABLE public.surfboards ENABLE ROW LEVEL SECURITY")
    op.execute("""
        CREATE POLICY "surfboards_select_own" ON public.surfboards
            FOR SELECT USING (auth.uid() = profile_id)
    """)
    op.execute("""
        CREATE POLICY "surfboards_insert_own" ON public.surfboards
            FOR INSERT WITH CHECK (auth.uid() = profile_id)
    """)
    op.execute("""
        CREATE POLICY "surfboards_update_own" ON public.surfboards
            FOR UPDATE USING (auth.uid() = profile_id)
    """)
    op.execute("""
        CREATE POLICY "surfboards_delete_own" ON public.surfboards
            FOR DELETE USING (auth.uid() = profile_id)
    """)


def downgrade() -> None:
    op.execute('DROP POLICY IF EXISTS "surfboards_delete_own" ON public.surfboards')
    op.execute('DROP POLICY IF EXISTS "surfboards_update_own" ON public.surfboards')
    op.execute('DROP POLICY IF EXISTS "surfboards_insert_own" ON public.surfboards')
    op.execute('DROP POLICY IF EXISTS "surfboards_select_own" ON public.surfboards')
    op.execute("DROP INDEX IF EXISTS public.idx_surfboards_profile_id")
    op.execute("DROP TABLE IF EXISTS public.surfboards")

    op.execute("""
        ALTER TABLE public.profiles
            DROP COLUMN IF EXISTS avatar_url,
            DROP COLUMN IF EXISTS birthday,
            DROP COLUMN IF EXISTS gender,
            DROP COLUMN IF EXISTS name
    """)
