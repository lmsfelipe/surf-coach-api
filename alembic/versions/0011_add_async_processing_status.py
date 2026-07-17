"""add status and error_message to reviews and training_plans

Revision ID: 0011_async_processing
Revises: 0010_sessions_wave_board
Create Date: 2026-07-14
"""

from alembic import op
import sqlalchemy as sa

revision = "0011_async_processing"
down_revision = "0010_sessions_wave_board"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- reviews --
    op.add_column(
        "reviews",
        sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
        schema="public",
    )
    op.add_column(
        "reviews",
        sa.Column("error_message", sa.Text, nullable=True),
        schema="public",
    )
    # Allow narrative and improvement_tips to be NULL (for processing state)
    op.alter_column(
        "reviews", "narrative", existing_type=sa.Text(), nullable=True, schema="public"
    )
    op.alter_column(
        "reviews",
        "improvement_tips",
        existing_type=sa.ARRAY(sa.Text()),
        nullable=True,
        schema="public",
    )

    # -- training_plans --
    op.add_column(
        "training_plans",
        sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
        schema="public",
    )
    op.add_column(
        "training_plans",
        sa.Column("error_message", sa.Text, nullable=True),
        schema="public",
    )


def downgrade() -> None:
    op.drop_column("training_plans", "error_message", schema="public")
    op.drop_column("training_plans", "status", schema="public")

    op.alter_column(
        "reviews",
        "improvement_tips",
        existing_type=sa.ARRAY(sa.Text()),
        nullable=False,
        schema="public",
    )
    op.alter_column(
        "reviews", "narrative", existing_type=sa.Text(), nullable=False, schema="public"
    )
    op.drop_column("reviews", "error_message", schema="public")
    op.drop_column("reviews", "status", schema="public")
