"""add optimize_attempts to media

Tracks how many times video optimization has failed for a row so the sweep can
stop re-enqueuing a permanently-failing (corrupt or too-large) video forever.

Revision ID: 0015_media_optimize_attempts
Revises: 0014_rls_alembic_version
Create Date: 2026-08-04
"""

import sqlalchemy as sa

from alembic import op

revision = "0015_media_optimize_attempts"
down_revision = "0014_rls_alembic_version"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "media",
        sa.Column("optimize_attempts", sa.Integer(), nullable=False, server_default="0"),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column("media", "optimize_attempts", schema="public")
