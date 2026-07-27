"""add processing_started_at to reviews and training_plans

Revision ID: 0012_processing_started_at
Revises: 0011_async_processing
Create Date: 2026-07-17
"""

import sqlalchemy as sa

from alembic import op

revision = "0012_processing_started_at"
down_revision = "0011_async_processing"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "reviews",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )
    op.add_column(
        "training_plans",
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        schema="public",
    )
    # Backfill in-flight rows so the sweeper can age them from migration time.
    op.execute(
        "UPDATE public.reviews SET processing_started_at = now() WHERE status = 'processing'"
    )
    op.execute(
        "UPDATE public.training_plans SET processing_started_at = now() WHERE status = 'processing'"
    )


def downgrade() -> None:
    op.drop_column("training_plans", "processing_started_at", schema="public")
    op.drop_column("reviews", "processing_started_at", schema="public")
