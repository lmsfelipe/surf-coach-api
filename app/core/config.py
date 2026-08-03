from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Multipart boundaries, part headers and filenames ride along with the payload;
# 1 MiB comfortably covers the framing for a max-size batch.
MULTIPART_FRAMING_SLACK_BYTES = 1024 * 1024


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
    MAX_UPLOAD_SIZE_MB: int = Field(default=100, description="Per-file upload size cap (MB)")
    MAX_UPLOAD_FILES: int = Field(
        default=13,
        description=(
            "Max parts accepted in one multipart upload, counted regardless of type. "
            "Defaults to MAX_PHOTOS + MAX_VIDEOS so existing valid batches are unaffected."
        ),
    )
    MAX_VIDEO_DURATION_SEC: int = Field(default=120, description="Video duration cap (s)")
    UPLOAD_CONCURRENCY: int = Field(
        default=4,
        description=(
            "Max files stored in flight per batch upload. 1 = sequential stores "
            "(config-only rollback lever)."
        ),
    )
    SUPABASE_BUCKET: str = Field(default="surf-media", description="Supabase Storage bucket")
    TRAINING_WORKOUTS_PER_PLAN: int = Field(
        default=3, description="Number of workouts Gemini generates per plan"
    )
    CONTENT_MODERATION_ENABLED: bool = Field(
        default=False,
        description=(
            "Run Gemini content moderation at upload time. Disabled for the "
            "controlled release — re-enable once a faster moderation approach is defined."
        ),
    )
    REDIS_URL: str = Field(
        default="redis://localhost:6379", description="Redis URL for arq task queue"
    )
    WORKER_MAX_JOBS: int = Field(
        default=10, description="Max concurrent jobs per arq worker process"
    )
    WORKER_JOB_TIMEOUT_SEC: int = Field(
        default=240,
        description=(
            "Per-job timeout in the arq worker (s); lowered to 240 so timeout "
            "failures surface within the 5-min polling window"
        ),
    )
    STUCK_JOB_THRESHOLD_SEC: int = Field(
        default=600,
        description=(
            "Sweeper marks processing rows older than this as failed (s); must "
            "exceed job timeout + worst-case queue wait"
        ),
    )
    CORS_ORIGINS: list[str] = Field(default=[], description="Allowed CORS origins")
    ALLOWED_HOSTS: list[str] = Field(
        default=[],
        description=(
            "Allowed Host headers in production (empty = allow all). When set, include "
            "your platform's health-check host, e.g. 'healthcheck.railway.app'."
        ),
    )

    RATE_LIMIT_AI: str = Field(default="5/hour", description="Rate limit for AI endpoints per user")
    RATE_LIMIT_UPLOAD: str = Field(
        default="30/hour", description="Rate limit for upload endpoints per user"
    )
    RATE_LIMIT_DEFAULT: str = Field(default="120/minute", description="Default rate limit per user")
    RATE_LIMIT_ENABLED: bool = Field(default=True, description="Enable/disable rate limiting")

    SENTRY_DSN: str = Field(default="", description="Sentry DSN (empty = disabled)")

    VIDEO_OPTIMIZE_ENABLED: bool = Field(
        default=True, description="Master switch for video optimization (task + sweep)"
    )
    VIDEO_TARGET_HEIGHT: int = Field(
        default=720, description="Max output height; caps long-edge, never upscales"
    )
    VIDEO_CRF: int = Field(default=28, description="CRF quality/size knob (lower = better/larger)")
    VIDEO_KEEP_AUDIO: bool = Field(default=True, description="Keep low-bitrate audio vs strip it")
    VIDEO_AUDIO_BITRATE_KBPS: int = Field(default=96, description="Audio bitrate when kept (kbps)")
    VIDEO_OPTIMIZE_GRACE_SEC: int = Field(
        default=900, description="Delay after upload before compressing (s)"
    )
    VIDEO_OPTIMIZE_BATCH: int = Field(default=20, description="Max videos enqueued per sweep tick")

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"

    @property
    def max_request_body_bytes(self) -> int:
        """Hard ceiling on any request body, enforced before it is buffered.

        Derived from the per-file and per-batch caps rather than configured
        separately, plus a slice of slack for multipart boundaries and part
        headers, so raising either cap raises this one with it.
        """
        payload = self.MAX_UPLOAD_FILES * self.MAX_UPLOAD_SIZE_MB * 1024 * 1024
        return payload + MULTIPART_FRAMING_SLACK_BYTES


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
