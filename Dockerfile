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
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*
RUN python -m venv "$VIRTUAL_ENV"

FROM base AS deps
WORKDIR /app
COPY pyproject.toml ./
# Install dependencies from pyproject so the image never drifts from the declared set.
# A stub package satisfies the build backend; the real code is COPYed in later stages
# and the project itself is uninstalled so only its dependencies remain.
RUN mkdir -p app && touch app/__init__.py \
    && pip install --upgrade pip \
    && pip install ".[dev]" \
    && pip uninstall -y surf-coach-api \
    && rm -rf app *.egg-info

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
