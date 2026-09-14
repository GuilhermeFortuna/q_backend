FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

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

COPY pyproject.toml uv.lock ./
COPY src ./src
COPY contracts ./contracts
COPY alembic ./alembic
COPY alembic.ini ./
COPY docker/metatrader5-stub ./docker/metatrader5-stub
COPY docker/entrypoint.sh /entrypoint.sh

RUN chmod +x /entrypoint.sh \
    && uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

RUN mkdir -p /data/lake /data/tick_cache

EXPOSE 8000

ENTRYPOINT ["/entrypoint.sh"]
CMD ["uvicorn", "q_backend.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
