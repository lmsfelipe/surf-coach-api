# syntax=docker/dockerfile:1.6

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
        libmagic1 \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv "$VIRTUAL_ENV"

FROM base AS deps
WORKDIR /app
COPY pyproject.toml ./
RUN pip install --upgrade pip \
    && pip install \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.30" \
        "pydantic>=2.7" \
        "pydantic-settings>=2.3" \
        "sqlalchemy>=2.0" \
        "asyncpg>=0.29" \
        "alembic>=1.13" \
        "python-jose[cryptography]>=3.3" \
        "httpx>=0.27" \
        "python-dotenv>=1.0" \
        "google-generativeai>=0.7" \
        "opencv-python-headless>=4.10" \
        "supabase>=2.0" \
        "python-magic>=0.4" \
        "aiofiles>=23.0" \
        "python-multipart>=0.0.9" \
        "pytest>=8.2" \
        "pytest-asyncio>=0.23" \
        "ruff>=0.5"

FROM deps AS dev
WORKDIR /app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY app ./app
COPY tests ./tests
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

FROM deps AS prod
WORKDIR /app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY app ./app
EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
