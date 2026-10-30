from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
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
    # Supabase's session-mode pooler (Supavisor, :5432) caps *all* clients of the
    # project at its pool size — 15 by default — and every pooled connection held
    # here, idle or not, counts against it. The API and the worker each get their
    # own pool, so pool size + overflow, times processes, must stay under that cap
    # with room left for the pre-deploy migration and a redeploy's overlap.
    DB_POOL_SIZE: int = Field(default=5, ge=1, description="Pooled DB connections per process")
    DB_MAX_OVERFLOW: int = Field(
        default=0, ge=0, description="Extra DB connections opened past DB_POOL_SIZE under load"
    )
    DB_POOL_TIMEOUT_SEC: float = Field(
        default=10.0,
        gt=0,
        description="Wait for a free pooled connection before failing the request (s)",
    )

    SUPABASE_URL: str = Field(..., description="Supabase project URL")
    SUPABASE_ANON_KEY: str = Field(..., description="Supabase anon key (public)")
    SUPABASE_SERVICE_ROLE_KEY: str = Field(..., description="Supabase service role key (server)")
    SUPABASE_JWT_SECRET: str = Field(..., description="HMAC secret for verifying Supabase JWTs")

    GEMINI_API_KEY: str = Field(..., description="Gemini Vision API key")
    GEMINI_MODEL: str = Field(default="gemini-2.0-flash", description="Gemini model version")
    SINGLE_PASS_REVIEW: bool = Field(
        default=False,
        description=(
            "Generate the review in one Gemini call (media + description together) "
            "instead of the two-pass media-then-refine flow. Keep OFF until score-bias "
            "validation confirms the description does not move the scores."
        ),
    )
    GEMINI_TEMPERATURE: float = Field(
        default=0.2,
        description="Sampling temperature for the single-pass review call (score stability).",
    )
    GEMINI_MAX_ATTEMPTS: int = Field(
        default=3,
        description=(
            "Total attempts per Gemini call (1 = no retry). Retries transient 503 "
            "overloads and dropped keep-alive connections with exponential backoff."
        ),
    )
    GEMINI_RETRY_BASE_DELAY_SEC: float = Field(
        default=0.5, description="Base backoff delay between Gemini retry attempts (seconds)."
    )
    GEMINI_THINKING_LEVEL: str = Field(
        default="",
        description=(
            "Thinking level for Gemini 3.x calls: MINIMAL, LOW, MEDIUM, or HIGH. 3.x "
            "models think at MEDIUM by default; LOW cuts latency for structured tasks "
            "like scoring. Leave empty to keep the model default (required for 2.x "
            "models, which do not support thinking_level)."
        ),
    )
    FRAME_EXTRACT_COUNT: int = Field(default=6, description="Frames sampled per video")
    MAX_UPLOAD_SIZE_MB: int = Field(default=100, description="Per-file upload size cap (MB)")
    MAX_UPLOAD_FILES: int = Field(
        default=11,
        description=(
            "Max parts accepted in one multipart upload, counted regardless of type. "
            "Bounds parts of any type, including ones the photo/video counters below "
            "do not recognise, so keep it at or above MAX_UPLOAD_PHOTOS + MAX_UPLOAD_VIDEOS."
        ),
    )
    MIN_UPLOAD_PHOTOS: int = Field(
        default=1,
        ge=1,
        description=(
            "Photos required in an upload that contains any photo at all. Must not "
            "exceed MAX_UPLOAD_PHOTOS; a video-only upload is unaffected."
        ),
    )
    MAX_UPLOAD_PHOTOS: int = Field(
        default=10,
        ge=1,
        description="Max photos accepted in one upload.",
    )
    MAX_UPLOAD_VIDEOS: int = Field(
        default=1,
        ge=1,
        description="Max videos accepted in one upload.",
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
    GEMINI_REQUEST_TIMEOUT_SEC: float = Field(
        default=90.0,
        gt=0,
        description=(
            "Per-attempt HTTP timeout on a Gemini call (s). Without it a hung "
            "request keeps its thread — and the frames it was sent — alive in the "
            "API process long after the job that started it was cancelled."
        ),
    )
    GEMINI_MIN_ATTEMPT_SEC: float = Field(
        default=10.0,
        gt=0,
        description=(
            "Do not start (or retry into) a Gemini attempt with less budget than "
            "this left before the job deadline (s)."
        ),
    )
    GEMINI_DEADLINE_MARGIN_SEC: float = Field(
        default=5.0,
        ge=0,
        description=(
            "Stop Gemini work this long before the job deadline (s), so the thread "
            "finishes around the time the job's coroutine is cancelled."
        ),
    )

    # Background jobs run inside this process (no worker, no queue broker) — see
    # docs/SPEC_BACKEND_Merge_Worker_Into_API.md.
    JOBS_MAX_CONCURRENT: int = Field(
        default=2,
        ge=1,
        description=(
            "Jobs running at once. Each review holds the raw video bytes and the "
            "extracted frames in memory, and the API container now pays that cost, "
            "so keep it low. Also keep JOBS_MAX_CONCURRENT + concurrent HTTP DB use "
            "within DB_POOL_SIZE."
        ),
    )
    JOBS_MAX_QUEUED: int = Field(
        default=4,
        ge=1,
        description=(
            "Jobs allowed to wait for a slot. Beyond this, submit is rejected "
            "immediately and the row is failed as busy, instead of the user waiting "
            "out the whole timeout to be told it took too long. A job counts as "
            "waiting from submit until it takes a slot, so 0 would reject everything."
        ),
    )
    JOB_MAX_WAIT_SEC: float = Field(
        default=60.0,
        gt=0,
        description=(
            "Longest a job may wait for a slot (s). A job that does start therefore "
            "always has JOB_TIMEOUT_SEC - JOB_MAX_WAIT_SEC left to run."
        ),
    )
    JOB_TIMEOUT_SEC: float = Field(
        default=240.0,
        gt=0,
        description=(
            "Total budget per job (s), counted from submit and covering the wait "
            "for a slot plus the run. 240 keeps timeout failures inside the 5-min "
            "polling window."
        ),
    )
    JOB_SHUTDOWN_GRACE_SEC: float = Field(
        default=25.0,
        ge=0,
        description=(
            "How long a redeploy waits for running jobs before cancelling them (s). "
            "Railway's deployment draining period must exceed this, or close() is "
            "killed mid-drain and the startup sweep has to clean up instead."
        ),
    )
    STUCK_JOB_THRESHOLD_SEC: int = Field(
        default=300,
        description=(
            "The startup sweep and the read-path check mark processing rows older "
            "than this as failed (s); must exceed JOB_TIMEOUT_SEC, which already "
            "covers the queue wait"
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

    @model_validator(mode="after")
    def _photo_bounds_are_satisfiable(self) -> "Settings":
        """Reject MIN_UPLOAD_PHOTOS > MAX_UPLOAD_PHOTOS at startup.

        Left to runtime it would not error — it would silently reject every
        photo upload, since no count can satisfy both bounds.
        """
        if self.MIN_UPLOAD_PHOTOS > self.MAX_UPLOAD_PHOTOS:
            raise ValueError(
                f"MIN_UPLOAD_PHOTOS ({self.MIN_UPLOAD_PHOTOS}) cannot exceed "
                f"MAX_UPLOAD_PHOTOS ({self.MAX_UPLOAD_PHOTOS})"
            )
        return self

    @model_validator(mode="after")
    def _job_budgets_are_consistent(self) -> "Settings":
        """Reject timeout combinations that would make a job unrunnable.

        The wait for a slot is carved out of the same budget as the run, so these
        bounds are what keep a job that starts at the last moment able to make one
        Gemini attempt — and keep "busy" and "took too long" meaning what they say.
        """
        if self.JOB_MAX_WAIT_SEC >= self.JOB_TIMEOUT_SEC:
            raise ValueError(
                f"JOB_MAX_WAIT_SEC ({self.JOB_MAX_WAIT_SEC}) must be below "
                f"JOB_TIMEOUT_SEC ({self.JOB_TIMEOUT_SEC}); a job that waits its "
                "whole budget would never run"
            )
        run_budget = self.JOB_TIMEOUT_SEC - self.JOB_MAX_WAIT_SEC
        min_useful_run = self.GEMINI_MIN_ATTEMPT_SEC + self.GEMINI_DEADLINE_MARGIN_SEC
        if run_budget < min_useful_run:
            raise ValueError(
                f"JOB_TIMEOUT_SEC - JOB_MAX_WAIT_SEC ({run_budget}) leaves less than "
                f"GEMINI_MIN_ATTEMPT_SEC + GEMINI_DEADLINE_MARGIN_SEC ({min_useful_run}), "
                "so a job that starts at the last moment could not make one Gemini attempt"
            )
        if self.GEMINI_MIN_ATTEMPT_SEC > self.GEMINI_REQUEST_TIMEOUT_SEC:
            raise ValueError(
                f"GEMINI_MIN_ATTEMPT_SEC ({self.GEMINI_MIN_ATTEMPT_SEC}) cannot exceed "
                f"GEMINI_REQUEST_TIMEOUT_SEC ({self.GEMINI_REQUEST_TIMEOUT_SEC})"
            )
        return self

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
