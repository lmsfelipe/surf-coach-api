"""dev-only shim for the auth.users table (skipped in production)

Revision ID: 0001_dev_auth_users_shim
Revises:
Create Date: 2026-04-16

Stands in for Supabase's managed `auth.users` when running against a plain
Postgres container locally. Gated on APP_ENV=development so production never
touches the real Supabase-owned schema.
"""
from __future__ import annotations

from alembic import op

from app.core.config import get_settings

revision = "0001_dev_auth_users_shim"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    if not get_settings().is_development:
        return
    op.execute("CREATE SCHEMA IF NOT EXISTS auth")
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS auth.users (
            id UUID PRIMARY KEY,
            email TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    # Supabase provides auth.uid() in its hosted DB; stub it locally so RLS policies
    # that reference it still parse against our plain Postgres container.
    op.execute(
        """
        CREATE OR REPLACE FUNCTION auth.uid() RETURNS uuid
        LANGUAGE sql STABLE AS $$ SELECT NULL::uuid $$
        """
    )


def downgrade() -> None:
    if not get_settings().is_development:
        return
    op.execute("DROP TABLE IF EXISTS auth.users")
