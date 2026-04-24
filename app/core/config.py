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

    @property
    def is_development(self) -> bool:
        return self.APP_ENV == "development"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
