"""The engine's pool must honour the DB_POOL_* settings.

Supabase's session-mode pooler (Supavisor) caps every client of the project at its
pool size, 15 by default. SQLAlchemy's own default (5 + 10 overflow) let the API
alone reach that cap, so the bound is set explicitly and sized per process.
"""

from app.core.config import get_settings
from app.core.db import engine


def test_engine_pool_uses_the_configured_bounds():
    settings = get_settings()
    assert engine.pool.size() == settings.DB_POOL_SIZE
    assert engine.pool._max_overflow == settings.DB_MAX_OVERFLOW
    assert engine.pool._timeout == settings.DB_POOL_TIMEOUT_SEC


def test_default_pools_for_api_and_worker_fit_the_supabase_session_cap():
    settings = get_settings()
    per_process = settings.DB_POOL_SIZE + settings.DB_MAX_OVERFLOW
    # API + worker, leaving headroom for the pre-deploy migration and a
    # redeploy's overlap under Supavisor's default pool size of 15.
    assert 2 * per_process <= 10
