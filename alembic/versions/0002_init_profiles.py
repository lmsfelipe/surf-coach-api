"""init profiles table

Revision ID: 0002_init_profiles
Revises: 0001_dev_auth_users_shim
Create Date: 2026-04-16
"""
from __future__ import annotations

from alembic import op

revision = "0002_init_profiles"
down_revision = "0001_dev_auth_users_shim"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.profiles (
            id UUID PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
            surf_level TEXT NOT NULL CHECK (surf_level IN
                ('beginner','intermediate','advanced','pro')),
            height_cm INTEGER CHECK (height_cm BETWEEN 100 AND 250),
            weight_kg INTEGER CHECK (weight_kg BETWEEN 30 AND 200),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("ALTER TABLE public.profiles ENABLE ROW LEVEL SECURITY")
    op.execute(
        """
        CREATE POLICY "profiles_select_own" ON public.profiles
            FOR SELECT USING (auth.uid() = id)
        """
    )
    op.execute(
        """
        CREATE POLICY "profiles_update_own" ON public.profiles
            FOR UPDATE USING (auth.uid() = id)
        """
    )


def downgrade() -> None:
    op.execute('DROP POLICY IF EXISTS "profiles_update_own" ON public.profiles')
    op.execute('DROP POLICY IF EXISTS "profiles_select_own" ON public.profiles')
    op.execute("DROP TABLE IF EXISTS public.profiles")
