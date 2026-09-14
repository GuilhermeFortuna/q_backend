#!/usr/bin/env bash
set -euo pipefail

# Determine repository root
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "=========================================="
echo " Starting q_backend CI Pipeline"
echo "=========================================="

# 1. Ensure required services are accessible (PostgreSQL & Redis)
if [ -z "${CI:-}" ]; then
  echo "==> Checking local Postgres (5434) and Redis (6380)..."
  SERVICES_READY=true
  if ! (echo > /dev/tcp/localhost/5434) >/dev/null 2>&1; then
    SERVICES_READY=false
  fi
  if ! (echo > /dev/tcp/localhost/6380) >/dev/null 2>&1; then
    SERVICES_READY=false
  fi

  if [ "$SERVICES_READY" = false ]; then
    UNITS_INSTALLED=false
    if command -v systemctl >/dev/null 2>&1; then
      if systemctl --user cat q-postgres.service >/dev/null 2>&1 && systemctl --user cat q-redis.service >/dev/null 2>&1; then
        UNITS_INSTALLED=true
      fi
    fi

    if [ "$UNITS_INSTALLED" = true ]; then
      echo "Services not running. Starting systemd user units (q-postgres, q-redis)..."
      systemctl --user start q-postgres.service q-redis.service
      echo "Waiting for PostgreSQL to be ready..."
      for i in {1..30}; do
        if (echo > /dev/tcp/localhost/5434) >/dev/null 2>&1; then
          echo "PostgreSQL is ready."
          break
        fi
        sleep 1
      done
    else
      echo "Services not running. Starting Docker services (postgres, redis)..."
      docker compose up -d postgres redis
      echo "Waiting for PostgreSQL to be ready..."
      for i in {1..30}; do
        if docker compose exec -T postgres pg_isready -U postgres >/dev/null 2>&1; then
          echo "PostgreSQL is ready."
          break
        fi
        sleep 1
      done
    fi
  else
    echo "Local services are reachable."
  fi
fi

# 2. Vendored contract drift
# Runs here rather than only in the GitHub workflow, so that a pre-push hook
# catches drift instead of leaving it for CI to find. Needs to reach the
# contracts repository; point CONTRACTS_REPO at a local clone when offline.
echo "==> Checking vendored contracts (make contracts-check)..."
make contracts-check

# 3. Database Migrations
echo "==> Applying database migrations (alembic upgrade head)..."
uv run alembic upgrade head

# 4. Linting
echo "==> Running linter (ruff check .)..."
uv run ruff check .

# 5. Code Formatting Check
echo "==> Checking code formatting (black --check .)..."
uv run black --check .

# 6. Automated Tests
# Unit tests run in parallel under pytest-xdist; integration tests share the live
# Postgres/Redis (they flushdb/flushall), so they run serially afterwards.
# Default to half the logical CPUs (capped at 12) so the desktop stays responsive;
# override with PYTEST_WORKERS=N. Numeric libraries get one thread per worker —
# parallelism comes from the xdist processes, not BLAS/OpenMP/numba threads.
CPUS="$(nproc 2>/dev/null || echo 2)"
DEFAULT_WORKERS=$(( CPUS / 2 ))
(( DEFAULT_WORKERS > 12 )) && DEFAULT_WORKERS=12
(( DEFAULT_WORKERS < 1 )) && DEFAULT_WORKERS=1
PYTEST_WORKERS="${PYTEST_WORKERS:-$DEFAULT_WORKERS}"
for var in OMP_NUM_THREADS OPENBLAS_NUM_THREADS MKL_NUM_THREADS NUMEXPR_NUM_THREADS NUMBA_NUM_THREADS; do
  export "$var=${!var:-1}"
done
# Lower CPU/IO priority locally so tests yield to interactive work.
NICE=()
if [ -z "${CI:-}" ] && command -v nice >/dev/null 2>&1; then
  NICE=(nice -n 10)
  command -v ionice >/dev/null 2>&1 && NICE=(ionice -c 3 "${NICE[@]}")
fi

echo "==> Running unit tests (pytest, ${PYTEST_WORKERS} workers)..."
"${NICE[@]}" uv run pytest -n "$PYTEST_WORKERS" --dist loadfile -m "not integration"

echo "==> Running integration tests (pytest, serial)..."
"${NICE[@]}" uv run pytest -m integration

echo "=========================================="
echo " All q_backend CI checks passed successfully! "
echo "=========================================="
