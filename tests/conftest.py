import os

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres"
)
os.environ.setdefault("SUPABASE_URL", "https://example.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "anon-test")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "service-role-test")
os.environ.setdefault("SUPABASE_JWT_SECRET", "test-jwt-secret-do-not-use-in-prod")
os.environ.setdefault("GEMINI_API_KEY", "test-gemini-key")
os.environ.setdefault("GEMINI_MODEL", "gemini-1.5-pro")
os.environ.setdefault("FRAME_EXTRACT_COUNT", "6")
os.environ.setdefault("MAX_UPLOAD_SIZE_MB", "100")
os.environ.setdefault("MAX_VIDEO_DURATION_SEC", "120")
os.environ.setdefault("SUPABASE_BUCKET", "surf-media")
os.environ.setdefault("TRAINING_WORKOUTS_PER_PLAN", "3")
os.environ.setdefault("CONTENT_MODERATION_ENABLED", "false")
os.environ.setdefault("RATE_LIMIT_ENABLED", "false")
os.environ.setdefault("SENTRY_DSN", "")
os.environ.setdefault("VIDEO_OPTIMIZE_ENABLED", "true")

# Imported after the env defaults above: importing app.core.config constructs
# Settings, which requires them to already be set.
from datetime import UTC, datetime, timedelta  # noqa: E402
from uuid import UUID, uuid4  # noqa: E402

import pytest  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from jose import jwt  # noqa: E402

from app.core.config import Settings, get_settings  # noqa: E402

# Keep the developer's real .env out of the test run. Settings reads env_file=".env",
# and any key not stubbed above would otherwise be picked up from it — so the same
# test could pass locally and fail in CI (which has no .env), or vice versa. Must
# happen before the first get_settings() call, i.e. before app.main is imported.
Settings.model_config["env_file"] = None
get_settings.cache_clear()

from app.main import app  # noqa: E402


def make_token(
    user_id: UUID,
    email: str = "surfer@example.com",
    *,
    expires_in: timedelta = timedelta(hours=1),
    audience: str = "authenticated",
) -> str:
    """Mint a Supabase-shaped JWT the app will accept."""
    payload = {
        "sub": str(user_id),
        "email": email,
        "aud": audience,
        "exp": datetime.now(tz=UTC) + expires_in,
    }
    return jwt.encode(payload, get_settings().SUPABASE_JWT_SECRET, algorithm="HS256")


@pytest.fixture
def user_id() -> UUID:
    return uuid4()


@pytest.fixture
def auth_headers(user_id: UUID) -> dict[str, str]:
    return {"Authorization": f"Bearer {make_token(user_id)}"}


@pytest.fixture
def client() -> AsyncClient:
    """ASGI-transport client. Test modules may override this fixture locally."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.fixture(autouse=True)
def _no_leaked_dependency_overrides():
    """Fail loudly if a test leaves a dependency override installed.

    Overrides live on the module-level ``app`` object, so one that is not torn
    down silently rewires every test collected after it.
    """
    before = dict(app.dependency_overrides)
    yield
    leaked = set(app.dependency_overrides) - set(before)
    if leaked:
        for dep in leaked:
            app.dependency_overrides.pop(dep, None)
        names = sorted(getattr(d, "__name__", repr(d)) for d in leaked)
        raise AssertionError(f"Test leaked FastAPI dependency overrides: {names}")
