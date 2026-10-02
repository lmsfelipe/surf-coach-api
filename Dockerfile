# syntax=docker/dockerfile:1.6

FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
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
# Keep this uv version in step with .github/workflows/ci.yml.
COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv
ENV UV_PROJECT_ENVIRONMENT=$VIRTUAL_ENV \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
# Install exactly the versions pinned in uv.lock, hash-verified, so a build never picks
# up a newer release from PyPI. --locked fails the build if pyproject.toml was edited
# without re-running `uv lock`, rather than silently re-resolving. The project itself is
# not installed: its code is COPYed in by the stages below. Runtime dependencies only;
# the dev stage adds the `dev` extra on top.
RUN uv sync --locked --no-install-project

FROM deps AS dev
WORKDIR /app
RUN uv sync --locked --no-install-project --extra dev
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
