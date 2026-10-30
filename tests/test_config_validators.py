"""Settings combinations that must be refused at startup.

A bad timeout combination does not fail loudly at runtime — it quietly makes
every job unrunnable, or makes "busy" and "took too long" swap meanings. Catching
it at boot turns that into a failed deploy instead.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings

REQUIRED = {
    "DATABASE_URL": "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres",
    "SUPABASE_URL": "https://example.supabase.co",
    "SUPABASE_ANON_KEY": "anon",
    "SUPABASE_SERVICE_ROLE_KEY": "service",
    "SUPABASE_JWT_SECRET": "secret",
    "GEMINI_API_KEY": "key",
}


def build(**overrides) -> Settings:
    return Settings(**{**REQUIRED, **overrides})


def test_the_defaults_are_consistent():
    settings = build()
    assert settings.JOB_MAX_WAIT_SEC < settings.JOB_TIMEOUT_SEC
    assert settings.STUCK_JOB_THRESHOLD_SEC > settings.JOB_TIMEOUT_SEC


def test_a_wait_cap_at_or_above_the_total_budget_is_refused():
    with pytest.raises(ValidationError, match="JOB_MAX_WAIT_SEC"):
        build(JOB_MAX_WAIT_SEC=240, JOB_TIMEOUT_SEC=240)


def test_a_run_budget_too_small_for_one_gemini_attempt_is_refused():
    with pytest.raises(ValidationError, match="GEMINI_MIN_ATTEMPT_SEC"):
        build(
            JOB_TIMEOUT_SEC=100,
            JOB_MAX_WAIT_SEC=95,
            GEMINI_MIN_ATTEMPT_SEC=10,
            GEMINI_DEADLINE_MARGIN_SEC=5,
        )


def test_a_minimum_attempt_longer_than_the_attempt_cap_is_refused():
    with pytest.raises(ValidationError, match="GEMINI_MIN_ATTEMPT_SEC"):
        build(GEMINI_MIN_ATTEMPT_SEC=120, GEMINI_REQUEST_TIMEOUT_SEC=90)


def test_a_tight_but_satisfiable_combination_is_accepted():
    settings = build(
        JOB_TIMEOUT_SEC=60,
        JOB_MAX_WAIT_SEC=45,
        GEMINI_MIN_ATTEMPT_SEC=10,
        GEMINI_DEADLINE_MARGIN_SEC=5,
        GEMINI_REQUEST_TIMEOUT_SEC=30,
    )
    assert settings.JOB_TIMEOUT_SEC - settings.JOB_MAX_WAIT_SEC == 15


def test_photo_bounds_that_cannot_be_satisfied_are_refused():
    with pytest.raises(ValidationError, match="MIN_UPLOAD_PHOTOS"):
        build(MIN_UPLOAD_PHOTOS=5, MAX_UPLOAD_PHOTOS=2)
