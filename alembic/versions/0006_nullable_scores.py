"""make score columns nullable

Revision ID: 0006_nullable_scores
Revises: 0005_create_reviews
Create Date: 2026-04-23
"""
from __future__ import annotations

from alembic import op

revision = "0006_nullable_scores"
down_revision = "0005_create_reviews"
branch_labels = None
depends_on = None

_SCORE_COLS = (
    "score_flow",
    "score_drop",
    "score_balance",
    "score_wave_selection",
    "score_maneuvers",
    "score_arms",
    "overall_score",
)


def upgrade() -> None:
    for col in _SCORE_COLS:
        op.execute(f"ALTER TABLE public.reviews ALTER COLUMN {col} DROP NOT NULL")


def downgrade() -> None:
    for col in _SCORE_COLS:
        op.execute(f"ALTER TABLE public.reviews ALTER COLUMN {col} SET NOT NULL")
