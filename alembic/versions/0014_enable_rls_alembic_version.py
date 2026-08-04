"""enable RLS on alembic_version

Supabase exposes the `public` schema through PostgREST, so its linter flags any
table there without row level security. `alembic_version` is Alembic's internal
bookkeeping table and should never be reachable through the REST API. Enabling
RLS with no policies denies all access to the anon/authenticated roles while the
migration runner (which owns the table) keeps full access.

Revision ID: 0014_rls_alembic_version
Revises: 0013_media_optimized_at
Create Date: 2026-08-04
"""

from alembic import op

revision = "0014_rls_alembic_version"
down_revision = "0013_media_optimized_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE public.alembic_version ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.execute("ALTER TABLE public.alembic_version DISABLE ROW LEVEL SECURITY")
