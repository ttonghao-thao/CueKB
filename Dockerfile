FROM python:3.12.10-slim AS source
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
ENV PATH="/app/.venv/bin:$PATH" UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
WORKDIR /app
COPY --from=ghcr.io/astral-sh/uv:0.11.28 /uv /uvx /bin/
RUN useradd --create-home --uid 10001 cuekb \
    && mkdir -p /data/originals /home/cuekb/.cache \
    && chown -R cuekb:cuekb /data/originals /home/cuekb/.cache
COPY pyproject.toml uv.lock README.md alembic.ini ./
COPY src/ ./src/
COPY migrations/ ./migrations/
COPY db/ ./db/

FROM source AS api
RUN uv sync --frozen --no-dev --no-editable
USER cuekb
EXPOSE 8080
CMD ["uvicorn", "cuekb.main:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "2", "--proxy-headers"]

FROM source AS worker
RUN apt-get update && apt-get install -y --no-install-recommends libgl1 libglib2.0-0 && rm -rf /var/lib/apt/lists/* \
    && uv sync --frozen --no-dev --no-editable --extra ingestion
USER cuekb
CMD ["cuekb-worker"]
