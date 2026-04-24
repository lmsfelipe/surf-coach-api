import os

os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5432/postgres")
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
