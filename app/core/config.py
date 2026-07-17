from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    APP_ENV: Literal["development", "staging", "production"] = "development"
    PORT: int = 8000
    LOG_LEVEL: str = "INFO"

    DATABASE_URL: str = Field(..., description="Async SQLAlchemy DSN (asyncpg driver)")

    SUPABASE_URL: str = Field(..., description="Supabase project URL")
    SUPABASE_ANON_KEY: str = Field(..., description="Supabase anon key (public)")
    SUPABASE_SERVICE_ROLE_KEY: str = Field(..., description="Supabase service role key (server)")
    SUPABASE_JWT_SECRET: str = Field(..., description="HMAC secret for verifying Supabase JWTs")

    GEMINI_API_KEY: str = Field(..., description="Gemini Vision API key")
    GEMINI_MODEL: str = Field(default="gemini-2.0-flash", description="Gemini model version")
    FRAME_EXTRACT_COUNT: int = Field(default=6, description="Frames sampled per video")
    MAX_UPLOAD_SIZE_MB: int = Field(default=100, description="Upload size cap (MB)")
    MAX_VIDEO_DURATION_SEC: int = Field(default=120, description="Video duration cap (s)")
    SUPABASE_BUCKET: str = Field(default="surf-media", description="Supabase Storage bucket")
    TRAINING_WORKOUTS_PER_PLAN: int = Field(default=3, description="Number of workouts Gemini generates per plan")
    CONTENT_MODERATION_ENABLED: bool = Field(default=True, description="Run Gemini content moderation at upload time")
    REDIS_URL: str = Field(default="redis://localhost:6379", description="Redis URL for arq task queue")
    WORKER_MAX_JOBS: int = Field(default=10, description="Max concurrent jobs per arq worker process")
    WORKER_JOB_TIMEOUT_SEC: int = Field(default=240, description="Per-job timeout in the arq worker (s); lowered to 240 so timeout failures surface within the 5-min polling window")
    STUCK_JOB_THRESHOLD_SEC: int = Field(default=600, description="Sweeper marks processing rows older than this as failed (s); must exceed job timeout + worst-case queue wait")
    CORS_ORIGINS: list[str] = Field(default=[], description="Allowed CORS origins")

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
