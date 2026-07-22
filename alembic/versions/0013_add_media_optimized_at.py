"""add optimized_at to media

Revision ID: 0013_media_optimized_at
Revises: 0012_processing_started_at
Create Date: 2026-07-21
"""

from alembic import op
import sqlalchemy as sa

revision = "0013_media_optimized_at"
down_revision = "0012_processing_started_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "media",
        sa.Column("optimized_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column("media", "optimized_at", schema="public")
