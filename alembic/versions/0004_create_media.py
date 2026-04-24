"""create media table

Revision ID: 0004_create_media
Revises: 0003_create_sessions
Create Date: 2026-04-17
"""
from __future__ import annotations

from alembic import op

revision = "0004_create_media"
down_revision = "0003_create_sessions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE public.media (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            session_id UUID NOT NULL REFERENCES public.sessions(id) ON DELETE CASCADE,
            media_type TEXT NOT NULL CHECK (media_type IN ('image', 'video')),
            storage_url TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_size_bytes BIGINT,
            duration_seconds NUMERIC(6,2),
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute("CREATE INDEX idx_media_session_id ON public.media(session_id)")
    op.execute("ALTER TABLE public.media ENABLE ROW LEVEL SECURITY")
    op.execute(
        'CREATE POLICY "media_select_own" ON public.media FOR SELECT USING ('
        "EXISTS (SELECT 1 FROM public.sessions s "
        "WHERE s.id = media.session_id AND auth.uid() = s.profile_id))"
    )
    op.execute(
        'CREATE POLICY "media_insert_own" ON public.media FOR INSERT WITH CHECK ('
        "EXISTS (SELECT 1 FROM public.sessions s "
        "WHERE s.id = media.session_id AND auth.uid() = s.profile_id))"
    )


def downgrade() -> None:
    op.execute('DROP POLICY IF EXISTS "media_insert_own" ON public.media')
    op.execute('DROP POLICY IF EXISTS "media_select_own" ON public.media')
    op.execute("DROP INDEX IF EXISTS public.idx_media_session_id")
    op.execute("DROP TABLE IF EXISTS public.media")
