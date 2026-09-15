# syntax=docker/dockerfile:1
ARG Q_RESEARCH_IMAGE_FINGERPRINT=unknown

# --- Builder Stage ---
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gcc \
        libc6-dev \
    && rm -rf /var/lib/apt/lists/*

ENV RUSTUP_HOME=/usr/local/rustup \
    CARGO_HOME=/usr/local/cargo \
    PATH=/usr/local/cargo/bin:${PATH}

RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y \
    --default-toolchain 1.98.0 \
    --profile minimal \
    --no-modify-path

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never

# Dependency metadata only
COPY pyproject.toml uv.lock ./
COPY docker/metatrader5-stub ./docker/metatrader5-stub

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY contracts ./contracts

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

# --- Runtime Stage ---
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
    Q_TICK_CACHE_DIR=/data/tick_cache \
    PATH="/app/.venv/bin:${PATH}"

COPY --from=builder /app/.venv /app/.venv

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY contracts ./contracts
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh && mkdir -p /data/lake /data/tick_cache

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "q_backend.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
