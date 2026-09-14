# syntax=docker/dockerfile:1
ARG Q_RESEARCH_IMAGE_FINGERPRINT=unknown

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

ARG Q_RESEARCH_IMAGE_FINGERPRINT=unknown
LABEL dev.q.backend.research-fingerprint=$Q_RESEARCH_IMAGE_FINGERPRINT

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    PYTHONUNBUFFERED=1 \
    Q_DATA_LAKE_ROOT=/data/lake \
    Q_TICK_CACHE_DIR=/data/tick_cache

# Dependency metadata only — keeps the expensive sync layer stable across source edits.
COPY pyproject.toml uv.lock ./
COPY docker/metatrader5-stub ./docker/metatrader5-stub

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Application, contracts, migrations, and entrypoint (overridden by bind mounts in Research).
COPY src ./src
COPY contracts ./contracts
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker/entrypoint.sh /entrypoint.sh

RUN --mount=type=cache,target=/root/.cache/uv \
    chmod +x /entrypoint.sh \
    && uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

RUN mkdir -p /data/lake /data/tick_cache

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "q_backend.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
