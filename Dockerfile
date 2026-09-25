# syntax=docker/dockerfile:1

FROM python:3.12.10-slim AS builder
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app
RUN python -m venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
COPY pyproject.toml requirements.lock README.md ./
COPY src/ ./src/

FROM builder AS api-builder
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --constraint requirements.lock '.[api]'

FROM builder AS worker-builder
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --constraint requirements.lock \
        --index-url https://download.pytorch.org/whl/cpu \
        --no-deps torch \
    && python -m pip install --constraint requirements.lock '.[ingestion]'

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
COPY db/ ./db/
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
