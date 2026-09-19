# syntax=docker/dockerfile:1

FROM python:3.12.10-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/
COPY pyproject.toml uv.lock README.md ./
COPY src/ ./src/

FROM builder AS api-builder
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra api

FROM builder AS worker-builder
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --extra ingestion

FROM python:3.12.10-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"
WORKDIR /app
RUN useradd --create-home --uid 10001 cuekb \
    && mkdir -p /data/originals /home/cuekb/.cache \
    && chown -R cuekb:cuekb /data/originals /home/cuekb/.cache

FROM runtime AS api
COPY --from=api-builder /app/.venv /app/.venv
COPY alembic.ini ./
COPY migrations/ ./migrations/
USER cuekb
EXPOSE 8080
CMD ["uvicorn", "cuekb.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2", "--proxy-headers"]

FROM runtime AS worker
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*
COPY --from=worker-builder /app/.venv /app/.venv
USER cuekb
CMD ["cuekb-worker"]
